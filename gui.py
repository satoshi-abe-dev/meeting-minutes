#!/usr/bin/env python3
"""打ち合わせ動画 → 議事録AI の GUI。

    python gui.py

動画ファイルを選んで実行すると、文字起こし → フレーム抽出 → フレーム解析 → 議事録生成
を順に行い、進捗バーとログを表示する。処理はワーカースレッドで走らせ、進捗は
queue 経由で UI スレッドへ渡す（tkinter はスレッドセーフでないため）。

このファイルは画面まわりだけを持ち、実処理は meeting_minutes.pipeline.run に委譲する。
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

from meeting_minutes.cancel import PipelineCancelled  # noqa: E402
from meeting_minutes.config import Config, load_config  # noqa: E402
from meeting_minutes.pipeline import run  # noqa: E402
from meeting_minutes.transcribe import resolve_backend  # noqa: E402

_WINDOW_WIDTH = 760
# 議事録フォーマットのラジオ 3 択ぶんの高さを見込む（内容の必要高に合わせる）。
_WINDOW_HEIGHT = 590

_STAGE_LABEL = {
    "preflight": "サーバー確認",
    "audio": "音声抽出",
    "transcribe": "文字起こし",
    "frames": "フレーム抽出",
    "vision": "フレーム解析",
    "minutes": "議事録生成",
    "done": "完了",
}
# 工程ごとの全体に対する重み（進捗バーをそれっぽく動かすための目安）
_STAGE_WEIGHT = {
    "preflight": 0.03,
    "audio": 0.05,
    "transcribe": 0.42,
    "frames": 0.10,
    "vision": 0.25,
    "minutes": 0.15,
}
_STAGE_ORDER = ["preflight", "audio", "transcribe", "frames", "vision", "minutes"]


class _Tooltip:
    """ttk ウィジェットにマウスオーバーで出す簡易ツールチップ。

    tkinter/ttk には標準のツールチップが無いため、枠なしの Toplevel を
    ウィジェットの直下に出す最小実装にしている。
    """

    def __init__(self, widget: tk.Widget, text: str, *, delay_ms: int = 400):
        self._widget = widget
        self._text = text
        self._delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _event: object = None) -> None:
        self._cancel_schedule()
        self._after_id = self._widget.after(self._delay_ms, self._show)

    def _cancel_schedule(self) -> None:
        if self._after_id is not None:
            self._widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self._widget.winfo_rootx() + 4
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)  # ウィンドウ枠・タイトルバーを出さない
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip,
            text=self._text,
            justify="left",
            wraplength=340,  # 長文ツールチップを自動折り返し（全ツールチップ共通）
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
        ).pack()

    def _hide(self, _event: object = None) -> None:
        self._cancel_schedule()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def _open_in_finder(path: Path) -> None:
    """macOS の Finder / 既定アプリで開く。他 OS でも一応フォールバック。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:  # noqa: BLE001 - 開けなくても致命的でない
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("打ち合わせ動画 → 議事録AI（ローカル処理）")
        self.root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}")
        self.root.minsize(640, 480)

        self.config_obj: Config = load_config(None)
        self.video_path: Path | None = None
        self.result_dir: Path | None = None
        self.minutes_path: Path | None = None
        # 議事録フォーマット。config.toml の設定を初期値として尊重し、GUI の
        # ラジオ／ファイル選択はその回だけの上書き（config.toml は書き換えない）。
        _cfg_tpl = self.config_obj.output.template_path
        self._template_path: str | None = _cfg_tpl or None  # 「ファイルを選択」側の対象
        # 初期選択は config の優先順位（auto > file > builtin）に合わせる。
        if self.config_obj.output.auto_structure:
            _init_fmt = "auto"
        elif _cfg_tpl:
            _init_fmt = "file"
        else:
            _init_fmt = "builtin"
        self._fmt_mode = tk.StringVar(value=_init_fmt)

        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._reuse: bool = True
        self._cancel_event: threading.Event | None = None

        self._build_ui()
        self._poll_events()

    # --- UI 構築 -------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        # 各行を「列0＝説明ラベル、列1＝操作」で統一する。列0 の幅は grid 自動
        # （行内で広い方＝「議事録フォーマット:」に合う）に任せ、列1 の開始位置は
        # 共有列なので全行で自動的に揃う。
        head = ttk.Frame(self.root)
        head.pack(fill="x", **pad)
        head.columnconfigure(1, weight=1)

        # 行0: 動画ファイル
        ttk.Label(head, text="動画ファイル:").grid(row=0, column=0, sticky="w")
        video_row = ttk.Frame(head)
        video_row.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(video_row, text="選択...", command=self._choose_file).pack(side="left")
        self.file_label = ttk.Label(video_row, text="未選択", foreground="#666")
        self.file_label.pack(side="left", padx=(8, 0))

        # 行1〜3: 議事録フォーマット（見出し・構成）の選択。ラジオ 3 択。
        # ラベルは「内蔵（既定）」と同じ行（row=1）に置く。row=2（ファイルを選択）と
        # row=3（おまかせ）の列0 は空欄のまま。
        ttk.Label(head, text="議事録フォーマット:").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        builtin_radio = ttk.Radiobutton(
            head, text="内蔵（既定）", value="builtin",
            variable=self._fmt_mode, command=self._sync_fmt_widgets,
        )
        builtin_radio.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        _Tooltip(
            builtin_radio,
            "「内蔵」は、会議の内容に関わらず常に同じ見出し・構成（決定事項・宿題・"
            "議事の要点など）を使う既定のフォーマットです。内容に応じて動的に変わる"
            "ことはありません。お客様ごとの様式に合わせたい場合は、テンプレートファイルを"
            "用意してこのメニューから選んでください。",
        )

        file_row = ttk.Frame(head)
        file_row.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(2, 0))
        ttk.Radiobutton(
            file_row, text="ファイルを選択", value="file",
            variable=self._fmt_mode, command=self._sync_fmt_widgets,
        ).pack(side="left")
        self._tpl_pick_btn = ttk.Button(
            file_row, text="選択...", command=self._pick_template_file
        )
        self._tpl_pick_btn.pack(side="left", padx=(8, 0))
        self._tpl_name_label = ttk.Label(
            file_row,
            text=Path(self._template_path).name if self._template_path else "未選択",
            foreground="#666",
        )
        self._tpl_name_label.pack(side="left", padx=(8, 0))

        auto_radio = ttk.Radiobutton(
            head, text="おまかせ（動画に合わせて自動生成）", value="auto",
            variable=self._fmt_mode, command=self._sync_fmt_widgets,
        )
        auto_radio.grid(row=3, column=1, sticky="w", padx=(6, 0), pady=(2, 0))
        _Tooltip(
            auto_radio,
            "「おまかせ」は、動画の内容に合わせて議事録の見出し構成を毎回 AI に"
            "提案させます（例: 団体旅行の説明会なら「スケジュール」「持ち物」"
            "「注意事項」など）。生成された構成は出力フォルダーの structure_used.txt に"
            "保存され、気に入ればテンプレートファイルとして保存して、以後は"
            "「ファイルを選択」で固定できます。生成に失敗した場合は自動的に"
            "「内蔵」で作成します。",
        )
        self._sync_fmt_widgets()

        # ttk.LabelFrame のタイトルは標準で小さいフォントになるため、通常サイズの
        # ラベルを見出しに置き、中身は枠線付きの素の Frame で囲う。
        ttk.Label(self.root, text="設定（config.toml で変更）").pack(
            anchor="w", padx=10, pady=(6, 0)
        )
        cfg = ttk.Frame(self.root, relief="groove", borderwidth=1)
        cfg.pack(fill="x", padx=10, pady=(2, 6))
        llm = self.config_obj.llm
        tr = self.config_obj.transcribe
        backend = resolve_backend(tr)
        backend_note = f"backend={tr.backend}" + (
            f" → {backend}" if tr.backend != backend else ""
        )
        # 実際に使う順番（文字起こし → VLM → LLM）で縦に並べる。
        ttk.Label(
            cfg,
            text=(
                f"文字起こし: {tr.model}（{backend_note}）\n"
                f"VLM: {llm.vlm_model}\n"
                f"LLM: {llm.model}\n"
                f"LLM・VLM 接続先: {llm.base_url}"
            ),
            justify="left",
        ).pack(anchor="w", padx=8, pady=6)

        self.reuse_var = tk.BooleanVar(value=True)
        reuse_check = ttk.Checkbutton(
            self.root,
            text="作成済みデータを利用する",
            variable=self.reuse_var,
        )
        reuse_check.pack(anchor="w", padx=10)
        _Tooltip(
            reuse_check,
            "チェックを入れると、前回までの中間ファイルを再利用して、"
            "処理を早く終えられます。",
        )

        info = ttk.Label(
            self.root,
            text=(
                "※ 音声・映像・文字起こし・要約はすべてこの PC 内で処理します。"
                "外部サービスへは送信しません。\n"
                "（LLM・VLM はローカルサーバーのものを使用）"
            ),
            foreground="#337",
            wraplength=720,
            justify="left",
        )
        info.pack(fill="x", padx=10)

        run_bar = ttk.Frame(self.root)
        run_bar.pack(**pad)
        self.run_btn = ttk.Button(
            run_bar, text="議事録を作成", command=self._start, state="disabled"
        )
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(
            run_bar, text="中断", command=self._stop, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.stage_label = ttk.Label(self.root, text="待機中")
        self.stage_label.pack(fill="x", padx=10)
        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", padx=10, pady=(0, 6))

        logframe = ttk.Frame(self.root)
        logframe.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logframe, height=12, state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logframe, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

        self.done_bar = ttk.Frame(self.root)
        self.done_bar.pack(fill="x", **pad)
        self.open_minutes_btn = ttk.Button(
            self.done_bar,
            text="議事録を開く",
            command=self._open_minutes,
            state="disabled",
        )
        self.open_minutes_btn.pack(side="left")
        self.open_folder_btn = ttk.Button(
            self.done_bar,
            text="出力フォルダーを開く",
            command=self._open_folder,
            state="disabled",
        )
        self.open_folder_btn.pack(side="left", padx=8)

        self._center_on_screen(_WINDOW_WIDTH, _WINDOW_HEIGHT)

    def _center_on_screen(self, width: int, height: int) -> None:
        """ウィンドウを画面中央に配置する。

        macOS では、ウィンドウが実体化する前に座標付き geometry() を渡しても
        初回表示時にウィンドウマネージャの既定位置（左下寄り）で上書きされる。
        全ウィジェットを組んだ後 update_idletasks() で一度実体化させてから
        "WxH+X+Y" 形式で座標を明示する。
        """
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    # --- 操作 -------------------------------------------------------
    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="打ち合わせ動画を選択",
            filetypes=[
                ("動画ファイル", "*.mp4 *.mov *.m4v *.mkv *.webm *.avi"),
                ("すべてのファイル", "*.*"),
            ],
        )
        if not path:
            return
        self.video_path = Path(path)
        self.file_label.configure(text=self.video_path.name, foreground="#000")
        self.run_btn.configure(state="normal")

    # --- 議事録フォーマットの選択 ---------------------------------------
    def _sync_fmt_widgets(self) -> None:
        """ラジオの状態に合わせて「選択...」ボタンとファイル名表示の有効／無効を切り替える。"""
        file_mode = self._fmt_mode.get() == "file"
        state = ["!disabled"] if file_mode else ["disabled"]
        self._tpl_pick_btn.state(state)
        self._tpl_name_label.state(state)

    def _pick_template_file(self) -> None:
        path = filedialog.askopenfilename(
            title="議事録テンプレートを選択",
            filetypes=[("テキスト", "*.txt *.md"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        self._template_path = path
        self._fmt_mode.set("file")
        self._tpl_name_label.configure(text=Path(path).name)
        self._sync_fmt_widgets()

    def _selected_template_path(self) -> str:
        """この回で使うテンプレートのパス（内蔵なら空文字）。"""
        if self._fmt_mode.get() == "file" and self._template_path:
            return self._template_path
        return ""

    def _start(self) -> None:
        if self.video_path is None or self._worker is not None:
            return
        # 「ファイルを選択」なのに未選択のまま開始 → 無警告で内蔵にフォールバックさせず、
        # ここで止めて気づかせる。
        if self._fmt_mode.get() == "file" and not self._template_path:
            messagebox.showerror(
                "議事録フォーマット",
                "「ファイルを選択」が選ばれていますが、テンプレートファイルが"
                "選択されていません。\n「選択...」からファイルを選ぶか、"
                "「内蔵（既定）」を選んでください。",
            )
            return
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_minutes_btn.configure(state="disabled")
        self.open_folder_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self._reuse = self.reuse_var.get()  # UI スレッドで読んでおく
        self._cancel_event = threading.Event()
        # この回で使う議事録フォーマット（GUI の選択で config.toml をその回だけ上書き）。
        # 3 つのラジオが排他的に 1 状態を表す。config.toml で auto_structure=true でも
        # GUI で「内蔵」「ファイルを選択」を選んだらそちらが勝つよう、両フラグを毎回セット。
        fmt_mode = self._fmt_mode.get()
        tpl = self._selected_template_path()
        self.config_obj.output.auto_structure = fmt_mode == "auto"
        self.config_obj.output.template_path = tpl
        if fmt_mode == "auto":
            fmt_desc = "おまかせ（動画に合わせて自動生成）"
        elif tpl:
            fmt_desc = tpl
        else:
            fmt_desc = "内蔵（既定）"
        self._append_log(
            f"開始: {self.video_path.name}"
            + ("" if self._reuse else "（最初からやり直す）")
        )
        self._append_log("議事録フォーマット: " + fmt_desc)

        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _stop(self) -> None:
        """中断ボタン。フラグを立てるだけ（協調的キャンセル）。"""
        if self._cancel_event is None:
            return
        self._cancel_event.set()
        self.stop_btn.configure(state="disabled")  # 二重クリック防止
        self._append_log(
            "中断を要求しました…現在の工程の区切りまで少し待ちます"
            "（mlx-whisper の文字起こし中や ffmpeg 実行中は即座には止まりません）"
        )

    def _work(self) -> None:
        def on_progress(stage: str, current: int, total: int, message: str) -> None:
            self._events.put(("progress", stage, current, total, message))

        try:
            result = run(
                self.video_path,
                self.config_obj,
                on_progress=on_progress,
                reuse=self._reuse,
                cancel_event=self._cancel_event,
            )
            self._events.put(("result", result))
        except PipelineCancelled:
            self._events.put(("cancelled",))
        except Exception as exc:  # noqa: BLE001 - UI に見せるため全捕捉
            self._events.put(("error", exc, traceback.format_exc()))

    # --- queue 消化（UI スレッド） --------------------------------
    def _poll_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "progress":
            _, stage, current, total, message = event
            self._update_progress(stage, current, total, message)
        elif kind == "result":
            self._on_success(event[1])
        elif kind == "error":
            self._on_error(event[1], event[2])
        elif kind == "cancelled":
            self._on_cancelled()

    def _update_progress(self, stage: str, current: int, total: int, message: str) -> None:
        label = _STAGE_LABEL.get(stage, stage)
        if stage == "done":
            self.progress.configure(value=1000)
            self.stage_label.configure(text="完了")
            return
        # 直前工程までの重み合計 + 現工程内の進捗割合
        if stage in _STAGE_ORDER:
            prior = _STAGE_ORDER[: _STAGE_ORDER.index(stage)]
            base = sum(_STAGE_WEIGHT[s] for s in prior)
        else:
            base = 0.0
        frac = (current / total) if total else 0.0
        weight = _STAGE_WEIGHT.get(stage, 0.0)
        self.progress.configure(value=int((base + weight * frac) * 1000))
        counter = f"（{current}/{total}）" if total else ""
        self.stage_label.configure(text=f"{label} {counter}")
        if message:
            self._append_log(f"[{label}] {message}")

    def _on_success(self, result) -> None:
        self._worker = None
        self.result_dir = result.out_dir
        self.minutes_path = result.minutes_path
        self.progress.configure(value=1000)
        self.stage_label.configure(text="完了")
        self._append_log("")
        self._append_log(f"議事録: {result.minutes_path}")
        self._append_log(
            f"文字起こし {result.n_segments} 区間 / フレーム {result.n_frames} 枚"
        )
        for w in result.warnings:
            self._append_log(f"警告: {w}")
        self.open_minutes_btn.configure(state="normal")
        self.open_folder_btn.configure(state="normal")
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._cancel_event = None

    def _on_error(self, exc: Exception, tb: str) -> None:
        self._worker = None
        self.stage_label.configure(text="エラー")
        self._append_log("")
        self._append_log(f"失敗: {exc}")
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._cancel_event = None
        messagebox.showerror("エラー", str(exc))
        # 詳細はログにだけ残す
        self._append_log(tb)

    def _on_cancelled(self) -> None:
        self._worker = None
        self.stage_label.configure(text="中断しました")
        self._append_log(
            "中断しました。ここまでの文字起こし・フレームは output に残っており、"
            "次回の実行（「作成済みデータを利用する」にチェックした状態）"
            "で再利用されます。"
        )
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._cancel_event = None

    # --- ログ / 外部を開く ---------------------------------------
    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_minutes(self) -> None:
        if self.minutes_path and Path(self.minutes_path).is_file():
            _open_in_finder(Path(self.minutes_path))

    def _open_folder(self) -> None:
        if self.result_dir and Path(self.result_dir).is_dir():
            _open_in_finder(Path(self.result_dir))


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
