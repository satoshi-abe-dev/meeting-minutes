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
_WINDOW_HEIGHT = 560

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

# 議事録フォーマット選択ドロップダウンの「ファイルを選択」項目のラベル。
_TEMPLATE_PICK_LABEL = "ファイルを選択..."


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
        # 議事録フォーマット: config.toml の設定と、GUI からその回だけ差し替える上書き。
        self._config_template_path: str = self.config_obj.output.template_path
        self._template_override: str | None = None  # None = config.toml の設定を使う

        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._reuse: bool = True
        self._cancel_event: threading.Event | None = None

        self._build_ui()
        self._poll_events()

    # --- UI 構築 -------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Button(top, text="動画ファイルを選択…", command=self._choose_file).pack(
            side="left"
        )
        self.file_label = ttk.Label(top, text="未選択", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        # 議事録フォーマット（見出し・構成）の選択。動画選択ボタンの直下に置く。
        fmt_row = ttk.Frame(self.root)
        fmt_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(fmt_row, text="議事録フォーマット:").pack(side="left")
        self.template_combo = ttk.Combobox(fmt_row, state="readonly", width=44)
        self.template_combo["values"] = [self._config_choice_label(), _TEMPLATE_PICK_LABEL]
        self.template_combo.set(self._config_choice_label())
        self.template_combo.pack(side="left", padx=8)
        self.template_combo.bind("<<ComboboxSelected>>", self._on_template_selected)
        _Tooltip(
            self.template_combo,
            "「内蔵」は、会議の内容に関わらず常に同じ見出し・構成（決定事項・宿題・"
            "議事の要点など）を使う既定のフォーマットです。内容に応じて動的に変わる"
            "ことはありません。お客様ごとの様式に合わせたい場合は、テンプレートファイルを"
            "用意してこのメニューから選んでください。",
        )

        info = ttk.Label(
            self.root,
            text=(
                "音声・映像・文字起こし・要約はすべてこの PC 内で処理します。"
                "外部サービスへは送信しません。\n"
                "（LLM・VLM はローカルサーバーのものを使用）"
            ),
            foreground="#337",
            wraplength=720,
            justify="left",
        )
        info.pack(fill="x", padx=10)

        cfg = ttk.LabelFrame(self.root, text="設定（config.toml で変更）")
        cfg.pack(fill="x", **pad)
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
    def _config_choice_label(self) -> str:
        """ドロップダウン先頭の固定項目（config.toml の設定）のラベル。"""
        if self._config_template_path:
            return f"{Path(self._config_template_path).name}（既定）"
        return "内蔵（既定）"

    def _committed_template_label(self) -> str:
        """いま確定している選択のラベル（ダイアログをキャンセルしたときの戻り先）。"""
        if self._template_override is not None:
            return Path(self._template_override).name
        return self._config_choice_label()

    def _on_template_selected(self, _event: object = None) -> None:
        choice = self.template_combo.get()
        if choice == _TEMPLATE_PICK_LABEL:
            path = filedialog.askopenfilename(
                title="議事録テンプレートを選択",
                filetypes=[("テキスト", "*.txt *.md"), ("すべてのファイル", "*.*")],
            )
            if not path:
                self.template_combo.set(self._committed_template_label())  # キャンセル
                return
            self._template_override = path
            name = Path(path).name  # ファイル名だけを表示（先頭項目は「…（既定）」で区別）
            self.template_combo["values"] = [
                self._config_choice_label(), name, _TEMPLATE_PICK_LABEL
            ]
            self.template_combo.set(name)
        elif choice == self._config_choice_label():  # config.toml の設定 = 既定に戻す
            self._template_override = None
            self.template_combo["values"] = [
                self._config_choice_label(), _TEMPLATE_PICK_LABEL
            ]
            self.template_combo.set(self._config_choice_label())
        # それ以外はその回だけ上書き中のファイル名項目（_template_override は設定済み）

    def _start(self) -> None:
        if self.video_path is None or self._worker is not None:
            return
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_minutes_btn.configure(state="disabled")
        self.open_folder_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self._reuse = self.reuse_var.get()  # UI スレッドで読んでおく
        self._cancel_event = threading.Event()
        # この回で使う議事録フォーマット（今回だけの上書き優先、無ければ config.toml の設定）。
        self.config_obj.output.template_path = (
            self._template_override
            if self._template_override is not None
            else self._config_template_path
        )
        self._append_log(
            f"開始: {self.video_path.name}"
            + ("" if self._reuse else "（最初からやり直す）")
        )
        if self._template_override is not None:
            self._append_log(f"議事録フォーマット（今回だけ）: {self._template_override}")

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
