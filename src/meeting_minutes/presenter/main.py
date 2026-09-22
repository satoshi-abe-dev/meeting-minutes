"""Presenter — the main screen

The full set of screen logic that used to be mixed into gui.py's ``App``:
    - resolving the minutes-format 3-way choice (reflecting it into config.output)
    - guarding the start of "Create minutes," button state transitions, launching the worker thread
    - receiving progress events (via a queue), computing the progress fraction, and formatting the log
    - state transitions on success / error / cancellation
Depends only on the View's contract (MainView) and the Model (config /
pipeline.run); does not import tkinter at all.
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

# Stage labels have moved to the i18n catalog's "stage.<name>" keys (used to be _STAGE_LABEL).
# Each stage's weight relative to the whole (a rough guide for making the progress bar move plausibly)
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
        self.minutes_docx_path = None
        # The minutes format. Respects the config.toml setting as the initial
        # value; the GUI's radio/file selection is a one-time override for
        # that run (config.toml itself is not rewritten).
        _cfg_tpl = self.config_obj.output.template_path
        self._template_path: str | None = _cfg_tpl or None  # the target for "Choose a file"

        self._events: queue.Queue[tuple] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._reuse: bool = True
        self._cancel_event: threading.Event | None = None
        # Points at output/<video name>/logs/gui.log once a run starts. Appended with the same content as the on-screen log.
        self._log_path: Path | None = None

        self.view.set_on_choose_video(self._choose_file)
        self.view.set_on_pick_template(self._pick_template_file)
        self.view.set_on_start(self._start)
        self.view.set_on_stop(self._stop)
        self.view.set_on_open_minutes(self._open_minutes)
        self.view.set_on_open_folder(self._open_folder)

        # The initial selection matches config's priority order (auto > file > builtin).
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
        """Write to the on-screen log area, and while running, also append to output/<video name>/logs/gui.log.
        Does not change the View (the on-screen display). A file-write failure does not affect the GUI's operation."""
        self.view.append_log(text)
        if self._log_path is not None:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except OSError:
                pass  # swallow a log-file write failure (the on-screen display already succeeded)

    # --- Displaying the configuration -------------------------------------------------
    def _config_summary(self) -> str:
        ai = self.config_obj.ai
        tr = self.config_obj.transcribe
        backend = resolve_backend(tr)
        # backend=auto (or an unknown value) is resolved to the real value by
        # resolve_backend. Word it so "auto was resolved to mlx" comes across
        # (an explicitly-set value is shown plainly).
        if tr.backend == backend:
            backend_note = self._t("cfg.backend_explicit", backend=tr.backend)
        else:
            backend_note = self._t(
                "cfg.backend_resolved", configured=tr.backend, actual=backend
            )
        # Listed vertically in the order they're actually used (transcription -> VLM -> LLM).
        return "\n".join(
            (
                self._t("cfg.transcribe", model=tr.model, note=backend_note),
                self._t("cfg.vlm", model=ai.vlm_model),
                self._t("cfg.llm", model=ai.llm_model),
                self._t("cfg.endpoint", url=ai.base_url),
            )
        )

    # --- Actions -------------------------------------------------------
    def _choose_file(self) -> None:
        path = self.view.ask_video_path()
        if not path:
            return
        self.video_path = Path(path)
        self.view.set_video_name(self.video_path.name)
        self.view.set_start_enabled(True)

    # --- Choosing the minutes format ---------------------------------------
    def _pick_template_file(self) -> None:
        path = self.view.ask_template_path()
        if not path:
            return
        self._template_path = path
        self.view.set_format_mode("file")
        self.view.set_template_name(Path(path).name)

    def _selected_template_path(self) -> str:
        """The template path to use for this run (an empty string for the built-in template)."""
        if self.view.get_format_mode() == "file" and self._template_path:
            return self._template_path
        return ""

    def _start(self) -> None:
        if self.video_path is None or self._worker is not None:
            return
        # Starting with "Choose a file" selected but nothing actually chosen
        # -> rather than silently falling back to the built-in template, stop
        # here and make the user notice.
        if self.view.get_format_mode() == "file" and not self._template_path:
            self.view.show_error(
                self._t("dialog.format_error.title"),
                self._t("dialog.format_error.message"),
            )
            return

        # Where this run's log is written. pipeline.run() determines out_dir
        # after calling expanduser().resolve() on video_path, so apply the
        # same normalization here too (otherwise, if a symlink's name differs
        # from the real name, logs/gui.log could end up in a different folder
        # from transcript/ and minutes.md).
        resolved_video = self.video_path.expanduser().resolve()
        out_dir = self.config_obj.output_root / resolved_video.stem
        try:
            log_dir = out_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = log_dir / "gui.log"
        except OSError:
            self._log_path = None  # run the GUI anyway even if this can't be created

        self.view.set_start_enabled(False)
        self.view.set_stop_enabled(True)
        self.view.set_open_minutes_enabled(False)
        self.view.set_open_folder_enabled(False)
        self.view.set_progress(0)
        self._reuse = self.view.get_reuse()  # read it now, on the UI thread
        self._cancel_event = threading.Event()
        # The minutes format used for this run (the GUI's selection overrides
        # config.toml for just this run). The three radios exclusively
        # represent one state. Even if config.toml has auto_structure=true,
        # if the GUI has "Built-in" or "Choose a file" selected, that should
        # win — so both flags are set every time.
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
        """The Stop button. Just sets a flag (cooperative cancellation)."""
        if self._cancel_event is None:
            return
        self._cancel_event.set()
        self.view.set_stop_enabled(False)  # prevent a double click
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
                language=self.language,
            )
            self._events.put(("result", result))
        except PipelineCancelled:
            self._events.put(("cancelled",))
        except Exception as exc:
            self._events.put(("error", exc, traceback.format_exc()))

    # --- Draining the queue (on the UI thread) --------------------------------
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
        # sum of the weights of prior stages + progress fraction within the current stage
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
        self.minutes_docx_path = result.minutes_docx_path
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
        # keep the details in the log only
        self._log(tb)

    def _on_cancelled(self) -> None:
        self._worker = None
        self.view.set_stage_text(self._t("stage_text.cancelled"))
        self._log(self._t("log.cancelled"))
        self.view.set_start_enabled(True)
        self.view.set_stop_enabled(False)
        self._cancel_event = None

    # --- Opening things externally ---------------------------------------
    def _open_minutes(self) -> None:
        # Prefer the Word version. On a run where the docx conversion failed,
        # result.minutes_docx_path is None (the pipeline warns and continues).
        # As a safety net — e.g. if only the .md was manually deleted after a
        # past run — falls back to the output folder as a last resort.
        if self.minutes_docx_path and Path(self.minutes_docx_path).is_file():
            self.view.open_in_file_manager(Path(self.minutes_docx_path))
        elif self.minutes_path and Path(self.minutes_path).is_file():
            self.view.open_in_file_manager(Path(self.minutes_path))
        elif self.result_dir and Path(self.result_dir).is_dir():
            self.view.open_in_file_manager(Path(self.result_dir))

    def _open_folder(self) -> None:
        if self.result_dir and Path(self.result_dir).is_dir():
            self.view.open_in_file_manager(Path(self.result_dir))
