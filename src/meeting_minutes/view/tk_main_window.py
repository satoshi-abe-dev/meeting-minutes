"""View（Tkinter 実装層）— メインウィンドウ

現行 gui.py の ``_build_ui`` 以下（ウィジェット構築・イベントバインド・
ファイルダイアログ・ウィンドウ配置）をそのまま担う。ビジネスロジックは持たず、
操作は名前付きコールバックとして Presenter へ渡し、表示更新は MainView の
メソッドとして受ける。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from meeting_minutes.view.contract import MainView

_WINDOW_WIDTH = 760
# 議事録フォーマットのラジオ 3 択ぶんの高さを見込む（内容の必要高に合わせる）。
_WINDOW_HEIGHT = 590


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


class TkMainWindow(MainView):
    def __init__(self) -> None:
        self._callbacks: dict[str, Callable[[], None]] = {}

        self.root = tk.Tk()
        self.root.title("議事録生成AI（ローカル処理）")
        self.root.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_HEIGHT}")
        self.root.minsize(640, 480)

        # 議事録フォーマット。初期値は Presenter が set_format_mode() で上書きする。
        self._fmt_mode = tk.StringVar(value="builtin")
        self.reuse_var = tk.BooleanVar(value=True)

        self._build_ui()

    def _fire(self, name: str) -> None:
        handler = self._callbacks.get(name)
        if handler is not None:
            handler()

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
        ttk.Button(
            video_row, text="選択...", command=lambda: self._fire("choose_video")
        ).pack(side="left")
        self.file_label = ttk.Label(video_row, text="未選択", foreground="#666")
        self.file_label.pack(side="left", padx=(8, 0))

        # 行1〜3: 議事録フォーマット（見出し・構成）の選択。ラジオ 3 択。
        # ラベルは「内蔵（既定）」と同じ行（row=1）に置く。row=2（おまかせ）と
        # row=3（ファイルを選択）の列0 は空欄のまま。
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
        file_row.grid(row=3, column=1, sticky="w", padx=(6, 0), pady=(2, 0))
        ttk.Radiobutton(
            file_row, text="ファイルを選択", value="file",
            variable=self._fmt_mode, command=self._sync_fmt_widgets,
        ).pack(side="left")
        self._tpl_pick_btn = ttk.Button(
            file_row, text="選択...", command=lambda: self._fire("pick_template")
        )
        self._tpl_pick_btn.pack(side="left", padx=(8, 0))
        self._tpl_name_label = ttk.Label(file_row, text="未選択", foreground="#666")
        self._tpl_name_label.pack(side="left", padx=(8, 0))

        auto_radio = ttk.Radiobutton(
            head, text="おまかせ（動画に合わせて自動生成）", value="auto",
            variable=self._fmt_mode, command=self._sync_fmt_widgets,
        )
        auto_radio.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(2, 0))
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

        # ttk の relief 枠線（groove/solid いずれも）は macOS(aqua) テーマで薄すぎ／
        # 描画されず、実画面で見えなかった（PR #33/#40/#41）。テーマ非依存で Tk コアが
        # 直接描く tk.Frame の highlightthickness（1px の枠）に切り替える。
        # 見出しラベルは text= の小フォント問題を避けるため引き続き枠の上に別置き。
        # 色は明るい背景（systemWindowBackgroundColor ≒ 白〜淡灰）に対して WCAG 非テキスト
        # UI 基準 3:1 を満たす #808080（対白 約 4.0:1 / 対 #ECECEC 約 3.3:1）。
        cfg_label = ttk.Label(self.root, text="設定（config.toml で変更）")
        cfg_label.pack(anchor="w", padx=10, pady=(6, 2))
        cfg = tk.Frame(
            self.root, highlightbackground="#808080", highlightthickness=1, bd=0
        )
        cfg.pack(fill="x", padx=10, pady=(0, 6))
        # 本文は Presenter が set_config_summary() で流し込む（config + resolve_backend）。
        self._cfg_summary_label = ttk.Label(cfg, text="", justify="left")
        self._cfg_summary_label.pack(anchor="w", padx=8, pady=6)

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
            run_bar, text="議事録を作成", command=lambda: self._fire("start"),
            state="disabled",
        )
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(
            run_bar, text="中断", command=lambda: self._fire("stop"), state="disabled"
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
            command=lambda: self._fire("open_minutes"),
            state="disabled",
        )
        self.open_minutes_btn.pack(side="left")
        self.open_folder_btn = ttk.Button(
            self.done_bar,
            text="出力フォルダーを開く",
            command=lambda: self._fire("open_folder"),
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

    def _sync_fmt_widgets(self) -> None:
        """ラジオの状態に合わせて「選択...」ボタンとファイル名表示の有効／無効を切り替える。"""
        file_mode = self._fmt_mode.get() == "file"
        state = ["!disabled"] if file_mode else ["disabled"]
        self._tpl_pick_btn.state(state)
        self._tpl_name_label.state(state)

    # --- MainView 実装: ハンドラ登録 --------------------------------
    def set_on_choose_video(self, handler: Callable[[], None]) -> None:
        self._callbacks["choose_video"] = handler

    def set_on_pick_template(self, handler: Callable[[], None]) -> None:
        self._callbacks["pick_template"] = handler

    def set_on_start(self, handler: Callable[[], None]) -> None:
        self._callbacks["start"] = handler

    def set_on_stop(self, handler: Callable[[], None]) -> None:
        self._callbacks["stop"] = handler

    def set_on_open_minutes(self, handler: Callable[[], None]) -> None:
        self._callbacks["open_minutes"] = handler

    def set_on_open_folder(self, handler: Callable[[], None]) -> None:
        self._callbacks["open_folder"] = handler

    # --- MainView 実装: 入力状態 -----------------------------------
    def get_format_mode(self) -> str:
        return self._fmt_mode.get()

    def get_reuse(self) -> bool:
        return self.reuse_var.get()

    # --- MainView 実装: 画面更新 ---------------------------------
    def set_format_mode(self, mode: str) -> None:
        self._fmt_mode.set(mode)
        self._sync_fmt_widgets()

    def set_config_summary(self, text: str) -> None:
        self._cfg_summary_label.configure(text=text)

    def set_video_name(self, name: str) -> None:
        self.file_label.configure(text=name, foreground="#000")

    def set_template_name(self, name: str) -> None:
        self._tpl_name_label.configure(text=name)

    def set_start_enabled(self, enabled: bool) -> None:
        self.run_btn.configure(state="normal" if enabled else "disabled")

    def set_stop_enabled(self, enabled: bool) -> None:
        self.stop_btn.configure(state="normal" if enabled else "disabled")

    def set_open_minutes_enabled(self, enabled: bool) -> None:
        self.open_minutes_btn.configure(state="normal" if enabled else "disabled")

    def set_open_folder_enabled(self, enabled: bool) -> None:
        self.open_folder_btn.configure(state="normal" if enabled else "disabled")

    def set_progress(self, value: int) -> None:
        self.progress.configure(value=value)

    def set_stage_text(self, text: str) -> None:
        self.stage_label.configure(text=text)

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message)

    # --- MainView 実装: ダイアログ / OS 連携 --------------------
    def ask_video_path(self) -> str | None:
        path = filedialog.askopenfilename(
            title="打ち合わせ動画を選択",
            filetypes=[
                ("動画ファイル", "*.mp4 *.mov *.m4v *.mkv *.webm *.avi"),
                ("すべてのファイル", "*.*"),
            ],
        )
        return path or None

    def ask_template_path(self) -> str | None:
        path = filedialog.askopenfilename(
            title="議事録テンプレートを選択",
            filetypes=[("テキスト", "*.txt *.md"), ("すべてのファイル", "*.*")],
        )
        return path or None

    def open_in_file_manager(self, path: Path) -> None:
        _open_in_finder(path)

    # --- MainView 実装: イベントループ -------------------------
    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self.root.after(delay_ms, callback)

    def run(self) -> None:
        self.root.mainloop()
