"""Presenter — メイン画面

現行 gui.py の ``App`` に混在していた画面ロジック一式:
    - 議事録フォーマット 3 択の解決（config.output への反映）
    - 「議事録を作成」開始時のガード・ボタン状態遷移・ワーカースレッド起動
    - 進捗イベントの受信（queue）と進捗率計算・ログ整形
    - 成功／エラー／中断時の状態遷移
View の契約（MainView）と Model（config / pipeline.run）にだけ依存し、tkinter は
一切 import しない。
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, normalize_language, t
from meeting_minutes.model.cancel import PipelineCancelled
from meeting_minutes.model.config import Config
from meeting_minutes.model.pipeline import run as _default_run
from meeting_minutes.model.transcribe import resolve_backend
from meeting_minutes.view import MainView

# 工程ラベルは i18n カタログの "stage.<name>" キーへ移した（旧 _STAGE_LABEL）。
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


class MainPresenter:
    def __init__(
        self,
        view: MainView,
        config: Config,
        *,
        run_pipeline: Callable = _default_run,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self.view = view
        self.config_obj = config
        self._run_pipeline = run_pipeline
        self.language = normalize_language(language)

        self.video_path: Path | None = None
        self.result_dir = None
        self.minutes_path = None
        # 議事録フォーマット。config.toml の設定を初期値として尊重し、GUI の
        # ラジオ／ファイル選択はその回だけの上書き（config.toml は書き換えない）。
        _cfg_tpl = self.config_obj.output.template_path
        self._template_path: str | None = _cfg_tpl or None  # 「ファイルを選択」側の対象

        self._events: queue.Queue[tuple] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._reuse: bool = True
        self._cancel_event: threading.Event | None = None
        # 実行開始時に output/<動画名>/gui.log を指す。画面のログ欄と同じ内容を追記する。
        self._log_path: Path | None = None

        self.view.set_on_choose_video(self._choose_file)
        self.view.set_on_pick_template(self._pick_template_file)
        self.view.set_on_start(self._start)
        self.view.set_on_stop(self._stop)
        self.view.set_on_open_minutes(self._open_minutes)
        self.view.set_on_open_folder(self._open_folder)

        # 初期選択は config の優先順位（auto > file > builtin）に合わせる。
        if self.config_obj.output.auto_structure:
            init_fmt = "auto"
        elif _cfg_tpl:
            init_fmt = "file"
        else:
            init_fmt = "builtin"
        self.view.set_format_mode(init_fmt)
        self.view.set_template_name(
            Path(self._template_path).name
            if self._template_path
            else self._t("label.unselected")
        )
        self.view.set_config_summary(self._config_summary())

        self._poll_events()

    def _t(self, key: str, *, default: str | None = None, **kwargs: object) -> str:
        return t(key, self.language, default=default, **kwargs)

    def _log(self, text: str) -> None:
        """画面のログ欄に出しつつ、実行中なら output/<動画名>/gui.log にも追記する。
        View（画面表示）は変更しない。ファイル書き込みの失敗は GUI 動作に影響させない。"""
        self.view.append_log(text)
        if self._log_path is not None:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except OSError:
                pass  # ログファイル書き込み失敗は握りつぶす（画面表示は済んでいる）

    # --- 設定表示 -------------------------------------------------
    def _config_summary(self) -> str:
        llm = self.config_obj.llm
        tr = self.config_obj.transcribe
        backend = resolve_backend(tr)
        backend_note = f"backend={tr.backend}" + (
            f" → {backend}" if tr.backend != backend else ""
        )
        # 実際に使う順番（文字起こし → VLM → LLM）で縦に並べる。
        return "\n".join(
            (
                self._t("cfg.transcribe", model=tr.model, note=backend_note),
                self._t("cfg.vlm", model=llm.vlm_model),
                self._t("cfg.llm", model=llm.model),
                self._t("cfg.endpoint", url=llm.base_url),
            )
        )

    # --- 操作 -------------------------------------------------------
    def _choose_file(self) -> None:
        path = self.view.ask_video_path()
        if not path:
            return
        self.video_path = Path(path)
        self.view.set_video_name(self.video_path.name)
        self.view.set_start_enabled(True)

    # --- 議事録フォーマットの選択 ---------------------------------------
    def _pick_template_file(self) -> None:
        path = self.view.ask_template_path()
        if not path:
            return
        self._template_path = path
        self.view.set_format_mode("file")
        self.view.set_template_name(Path(path).name)

    def _selected_template_path(self) -> str:
        """この回で使うテンプレートのパス（内蔵なら空文字）。"""
        if self.view.get_format_mode() == "file" and self._template_path:
            return self._template_path
        return ""

    def _start(self) -> None:
        if self.video_path is None or self._worker is not None:
            return
        # 「ファイルを選択」なのに未選択のまま開始 → 無警告で内蔵にフォールバックさせず、
        # ここで止めて気づかせる。
        if self.view.get_format_mode() == "file" and not self._template_path:
            self.view.show_error(
                self._t("dialog.format_error.title"),
                self._t("dialog.format_error.message"),
            )
            return

        # この回のログ書き出し先。pipeline.py の out_dir 算出と同じ式。
        out_dir = self.config_obj.output_root / self.video_path.stem
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = out_dir / "gui.log"
        except OSError:
            self._log_path = None  # 作れなくても GUI は動かす

        self.view.set_start_enabled(False)
        self.view.set_stop_enabled(True)
        self.view.set_open_minutes_enabled(False)
        self.view.set_open_folder_enabled(False)
        self.view.set_progress(0)
        self._reuse = self.view.get_reuse()  # UI スレッドで読んでおく
        self._cancel_event = threading.Event()
        # この回で使う議事録フォーマット（GUI の選択で config.toml をその回だけ上書き）。
        # 3 つのラジオが排他的に 1 状態を表す。config.toml で auto_structure=true でも
        # GUI で「内蔵」「ファイルを選択」を選んだらそちらが勝つよう、両フラグを毎回セット。
        fmt_mode = self.view.get_format_mode()
        tpl = self._selected_template_path()
        self.config_obj.output.auto_structure = fmt_mode == "auto"
        self.config_obj.output.template_path = tpl
        if fmt_mode == "auto":
            fmt_desc = self._t("log.format_desc.auto")
        elif tpl:
            fmt_desc = tpl
        else:
            fmt_desc = self._t("log.format_desc.builtin")
        suffix = "" if self._reuse else self._t("log.start_suffix_fresh")
        self._log(
            self._t("log.start", name=self.video_path.name, suffix=suffix)
        )
        self._log(self._t("log.format", desc=fmt_desc))

        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _stop(self) -> None:
        """中断ボタン。フラグを立てるだけ（協調的キャンセル）。"""
        if self._cancel_event is None:
            return
        self._cancel_event.set()
        self.view.set_stop_enabled(False)  # 二重クリック防止
        self._log(self._t("log.stop_requested"))

    def _work(self) -> None:
        def on_progress(stage: str, current: int, total: int, message: str) -> None:
            self._events.put(("progress", stage, current, total, message))

        try:
            result = self._run_pipeline(
                self.video_path,
                self.config_obj,
                on_progress=on_progress,
                reuse=self._reuse,
                cancel_event=self._cancel_event,
            )
            self._events.put(("result", result))
        except PipelineCancelled:
            self._events.put(("cancelled",))
        except Exception as exc:
            self._events.put(("error", exc, traceback.format_exc()))

    # --- queue 消化（UI スレッド） --------------------------------
    def _poll_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.view.schedule(100, self._poll_events)

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

    def _update_progress(
        self, stage: str, current: int, total: int, message: str
    ) -> None:
        label = self._t(f"stage.{stage}", default=stage)
        if stage == "done":
            self.view.set_progress(1000)
            self.view.set_stage_text(self._t("stage_text.done"))
            return
        # 直前工程までの重み合計 + 現工程内の進捗割合
        if stage in _STAGE_ORDER:
            prior = _STAGE_ORDER[: _STAGE_ORDER.index(stage)]
            base = sum(_STAGE_WEIGHT[s] for s in prior)
        else:
            base = 0.0
        frac = (current / total) if total else 0.0
        weight = _STAGE_WEIGHT.get(stage, 0.0)
        self.view.set_progress(int((base + weight * frac) * 1000))
        counter = (
            self._t("stage_text.counter", current=current, total=total)
            if total
            else ""
        )
        self.view.set_stage_text(f"{label} {counter}")
        if message:
            self._log(f"[{label}] {message}")

    def _on_success(self, result) -> None:
        self._worker = None
        self.result_dir = result.out_dir
        self.minutes_path = result.minutes_path
        self.view.set_progress(1000)
        self.view.set_stage_text(self._t("stage_text.done"))
        self._log("")
        self._log(self._t("log.minutes_path", path=result.minutes_path))
        self._log(
            self._t(
                "log.counts", segments=result.n_segments, frames=result.n_frames
            )
        )
        for w in result.warnings:
            self._log(self._t("log.warning", msg=w))
        self.view.set_open_minutes_enabled(True)
        self.view.set_open_folder_enabled(True)
        self.view.set_start_enabled(True)
        self.view.set_stop_enabled(False)
        self._cancel_event = None

    def _on_error(self, exc: Exception, tb: str) -> None:
        self._worker = None
        self.view.set_stage_text(self._t("stage_text.error"))
        self._log("")
        self._log(self._t("log.failure", exc=exc))
        self.view.set_start_enabled(True)
        self.view.set_stop_enabled(False)
        self._cancel_event = None
        self.view.show_error(self._t("dialog.error.title"), str(exc))
        # 詳細はログにだけ残す
        self._log(tb)

    def _on_cancelled(self) -> None:
        self._worker = None
        self.view.set_stage_text(self._t("stage_text.cancelled"))
        self._log(self._t("log.cancelled"))
        self.view.set_start_enabled(True)
        self.view.set_stop_enabled(False)
        self._cancel_event = None

    # --- 外部を開く ---------------------------------------------
    def _open_minutes(self) -> None:
        if self.minutes_path and Path(self.minutes_path).is_file():
            self.view.open_in_file_manager(Path(self.minutes_path))

    def _open_folder(self) -> None:
        if self.result_dir and Path(self.result_dir).is_dir():
            self.view.open_in_file_manager(Path(self.result_dir))
