"""Unit tests for MainPresenter (never starts tkinter).

Injects FakeMainView (a fake implementation of the MainView abstract class)
and verifies resolving the 3-way format choice, guards on start, the progress
fraction calculation, and button state transitions on success/error/cancellation.
View's Tkinter implementation (view/tk_main_window.py) is never loaded, so tkinter isn't required.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from meeting_minutes.model.config import Config
from meeting_minutes.presenter.main import (
    _STAGE_ORDER,
    _STAGE_WEIGHT,
    MainPresenter,
)
from meeting_minutes.view import MainView


class FakeMainView(MainView):
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[], None]] = {}
        self._format_mode = "builtin"
        self._reuse = True
        self.video_name: str | None = None
        self.template_name: str | None = None
        self.config_summary: str | None = None
        self.progress = 0
        self.stage_text = "準備完了"
        self.log: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.opened: list[Path] = []
        self.enabled = {
            "start": False, "stop": False, "open_minutes": False, "open_folder": False,
        }
        self.scheduled: list[tuple[int, Callable[[], None]]] = []
        self.next_video_path: str | None = None
        self.next_template_path: str | None = None

    # -- handler registration --
    def set_on_choose_video(self, h): self.handlers["choose_video"] = h
    def set_on_pick_template(self, h): self.handlers["pick_template"] = h
    def set_on_start(self, h): self.handlers["start"] = h
    def set_on_stop(self, h): self.handlers["stop"] = h
    def set_on_open_minutes(self, h): self.handlers["open_minutes"] = h
    def set_on_open_folder(self, h): self.handlers["open_folder"] = h

    # -- input state --
    def get_format_mode(self) -> str: return self._format_mode
    def get_reuse(self) -> bool: return self._reuse

    # -- screen updates --
    def set_format_mode(self, mode): self._format_mode = mode
    def set_config_summary(self, text): self.config_summary = text
    def set_video_name(self, name): self.video_name = name
    def set_template_name(self, name): self.template_name = name
    def set_start_enabled(self, e): self.enabled["start"] = e
    def set_stop_enabled(self, e): self.enabled["stop"] = e
    def set_open_minutes_enabled(self, e): self.enabled["open_minutes"] = e
    def set_open_folder_enabled(self, e): self.enabled["open_folder"] = e
    def set_progress(self, v): self.progress = v
    def set_stage_text(self, t): self.stage_text = t
    def append_log(self, t): self.log.append(t)
    def show_error(self, title, message): self.errors.append((title, message))

    # -- dialogs / OS --
    def ask_video_path(self): return self.next_video_path
    def ask_template_path(self): return self.next_template_path
    def open_in_file_manager(self, path): self.opened.append(Path(path))

    # -- event loop --
    def schedule(self, delay_ms, callback): self.scheduled.append((delay_ms, callback))
    def run(self): raise AssertionError("run() は単体テストでは呼ばれない想定")


class _FakeResult:
    def __init__(self, *, warnings=()):
        self.out_dir = "/tmp/out/会議"
        self.minutes_path = "/tmp/out/会議/minutes.md"
        self.minutes_docx_path = "/tmp/out/会議/minutes.docx"
        self.n_segments = 42
        self.n_frames = 7
        self.warnings = list(warnings)


def _config(*, auto_structure=False, template_path="") -> Config:
    cfg = Config()
    cfg.output.auto_structure = auto_structure
    cfg.output.template_path = template_path
    return cfg


def _make(cfg=None, run_pipeline=None, language="ja"):
    view = FakeMainView()
    presenter = MainPresenter(
        view, cfg or _config(),
        run_pipeline=run_pipeline or (lambda *a, **k: _FakeResult()),
        language=language,
    )
    return view, presenter


@pytest.fixture(autouse=True)
def _redirect_output(tmp_path, monkeypatch):
    """`_start()` creates output/<video name>/logs/ and writes gui.log. To avoid
    polluting the real repo's output/, redirect the output root to each test's tmp_path."""
    monkeypatch.setattr("meeting_minutes.model.config.REPO_ROOT", tmp_path)
    return tmp_path


# --- Initialization ---------------------------------------------------------

def test_init_registers_handlers_and_pushes_initial_state():
    view, _ = _make()
    assert set(view.handlers) == {
        "choose_video", "pick_template", "start", "stop",
        "open_minutes", "open_folder",
    }
    assert view._format_mode == "builtin"
    assert view.template_name == "未選択"
    assert "文字起こし:" in view.config_summary
    assert view.scheduled and view.scheduled[0][0] == 100  # polling started


def test_config_summary_backend_note_auto_is_readable(monkeypatch):
    # backend=auto gets resolved to the real value. Worded so it's clear "auto was resolved to mlx."
    monkeypatch.setattr(
        "meeting_minutes.presenter.main.resolve_backend", lambda tr: "mlx"
    )
    view, _ = _make()
    assert "backend=auto（自動選択: mlx）" in view.config_summary
    assert "→" not in view.config_summary


def test_config_summary_backend_note_explicit_is_plain(monkeypatch):
    monkeypatch.setattr(
        "meeting_minutes.presenter.main.resolve_backend", lambda tr: "faster-whisper"
    )
    cfg = _config()
    cfg.transcribe.backend = "faster-whisper"
    view, _ = _make(cfg)
    assert "backend=faster-whisper" in view.config_summary
    assert "自動選択" not in view.config_summary


@pytest.mark.parametrize(
    "cfg, expected_mode",
    [
        (_config(), "builtin"),
        (_config(template_path="/x/tpl.txt"), "file"),
        (_config(auto_structure=True), "auto"),
        (_config(auto_structure=True, template_path="/x/tpl.txt"), "auto"),
    ],
)
def test_initial_format_mode_follows_config_priority(cfg, expected_mode):
    view, _ = _make(cfg)
    assert view._format_mode == expected_mode


# --- Choosing a video ------------------------------------------------------

def test_choose_video_sets_name_and_enables_start():
    view, presenter = _make()
    view.next_video_path = "/videos/打ち合わせ.mp4"
    view.handlers["choose_video"]()
    assert view.video_name == "打ち合わせ.mp4"
    assert view.enabled["start"] is True
    assert presenter.video_path == Path("/videos/打ち合わせ.mp4")


def test_choose_video_cancelled_does_nothing():
    view, presenter = _make()
    view.next_video_path = None
    view.handlers["choose_video"]()
    assert view.video_name is None
    assert view.enabled["start"] is False
    assert presenter.video_path is None


# --- Resolving the 3-way format choice -> reflecting it into config ----------------------

def _start_with_video(view, presenter, mode):
    view.next_video_path = "/v/m.mp4"
    view.handlers["choose_video"]()
    view._format_mode = mode
    view.handlers["start"]()
    if presenter._worker:
        presenter._worker.join(timeout=2)


def test_start_builtin_sets_config_flags():
    view, presenter = _make()
    _start_with_video(view, presenter, "builtin")
    assert presenter.config_obj.output.auto_structure is False
    assert presenter.config_obj.output.template_path == ""
    assert "議事録フォーマット: 内蔵（既定）" in view.log


def test_start_auto_sets_config_flags():
    view, presenter = _make(_config(template_path="/pre/set.txt"))  # auto takes priority
    _start_with_video(view, presenter, "auto")
    assert presenter.config_obj.output.auto_structure is True
    assert presenter.config_obj.output.template_path == ""
    assert "議事録フォーマット: おまかせ（動画に合わせて自動生成）" in view.log


def test_start_file_with_template_sets_config_flags():
    view, presenter = _make()
    view.next_template_path = "/tpl/客先.txt"
    view.handlers["pick_template"]()  # -> mode="file", _template_path is set
    assert view._format_mode == "file"
    assert view.template_name == "客先.txt"
    _start_with_video(view, presenter, "file")
    assert presenter.config_obj.output.auto_structure is False
    assert presenter.config_obj.output.template_path == "/tpl/客先.txt"
    assert "議事録フォーマット: /tpl/客先.txt" in view.log


def test_start_file_without_template_is_blocked():
    view, presenter = _make()
    view.next_video_path = "/v/m.mp4"
    view.handlers["choose_video"]()
    view._format_mode = "file"  # left with no file selected
    view.handlers["start"]()
    assert view.errors and view.errors[0][0] == "議事録フォーマット"
    assert presenter._worker is None  # the worker did not start
    assert view.enabled["start"] is True  # the button stays enabled


def test_start_without_video_does_nothing():
    view, presenter = _make()
    view.handlers["start"]()
    assert presenter._worker is None
    assert view.log == []


# --- Writing to gui.log (Issue #98) --------------------------------

def test_start_computes_log_path_and_writes_file(tmp_path):
    view, presenter = _make()
    _start_with_video(view, presenter, "builtin")

    expected = tmp_path / "output" / "m" / "logs" / "gui.log"
    assert presenter._log_path == expected
    assert expected.is_file()
    # the file has the same content as the on-screen log area
    written = expected.read_text(encoding="utf-8")
    for line in view.log:
        assert line in written


def test_gui_log_is_appended_not_overwritten(tmp_path):
    view, presenter = _make()
    _start_with_video(view, presenter, "builtin")
    log_path = presenter._log_path
    presenter._poll_events()  # process the worker-completion event, resetting _worker to None
    first = log_path.read_text(encoding="utf-8")

    # running the same video again leaves the earlier content in place
    view.handlers["start"]()
    if presenter._worker:
        presenter._worker.join(timeout=2)
    second = log_path.read_text(encoding="utf-8")

    assert second.startswith(first)
    assert len(second) > len(first)
    assert second.count("開始: m.mp4") == 2


def test_log_write_failure_does_not_break_gui(tmp_path):
    view, presenter = _make()
    # setting log_path to a directory makes open(..., "a") raise IsADirectoryError (an OSError)
    presenter._log_path = tmp_path
    presenter._log("画面には出る")
    assert "画面には出る" in view.log  # the exception is swallowed and the display still happens


def test_log_path_uses_resolved_video_stem_for_symlink(tmp_path):
    """When a symlink is chosen, gui.log lands in the same "real name" folder
    as the pipeline (a Codex finding: using the link name would split it into a different folder)."""
    real = tmp_path / "real_video.mp4"
    real.write_bytes(b"x")
    link = tmp_path / "shortcut.mp4"
    link.symlink_to(real)

    view, presenter = _make()
    view.next_video_path = str(link)
    view.handlers["choose_video"]()
    view.handlers["start"]()
    if presenter._worker:
        presenter._worker.join(timeout=2)

    assert presenter._log_path == tmp_path / "output" / "real_video" / "logs" / "gui.log"


# --- Progress fraction calculation (_STAGE_ORDER / _STAGE_WEIGHT) --------------------

def test_update_progress_matches_stage_weight_formula():
    view, presenter = _make()
    # the 50% point of the transcribe stage
    presenter._update_progress("transcribe", 1, 2, "認識中")
    base = _STAGE_WEIGHT["preflight"] + _STAGE_WEIGHT["audio"]
    expected = int((base + _STAGE_WEIGHT["transcribe"] * 0.5) * 1000)
    assert view.progress == expected
    assert view.stage_text == "文字起こし （1/2）"
    assert view.log == ["[文字起こし] 認識中"]


def test_update_progress_total_zero_no_counter():
    view, presenter = _make()
    presenter._update_progress("vision", 0, 0, "")
    base = sum(_STAGE_WEIGHT[s] for s in _STAGE_ORDER[: _STAGE_ORDER.index("vision")])
    assert view.progress == int(base * 1000)
    assert view.stage_text == "フレーム解析 "
    assert view.log == []  # no log entry is added when message is empty


def test_update_progress_done_sets_full_bar():
    view, presenter = _make()
    presenter._update_progress("done", 1, 1, "")
    assert view.progress == 1000
    assert view.stage_text == "完了"


# --- Button state transitions on success / error / cancellation ----------------------

def test_on_success_button_states_and_log():
    view, presenter = _make()
    presenter._on_success(_FakeResult(warnings=["テンプレ大きすぎ"]))
    assert view.progress == 1000
    assert view.stage_text == "完了"
    assert view.enabled == {
        "start": True, "stop": False, "open_minutes": True, "open_folder": True,
    }
    assert presenter._cancel_event is None
    assert any("議事録: /tmp/out/会議/minutes.md" in line for line in view.log)
    assert any("警告: テンプレ大きすぎ" in line for line in view.log)


def test_on_error_button_states_and_dialog():
    view, presenter = _make()
    presenter._on_error(RuntimeError("LLM 落ちた"), "Traceback (most recent call last): ...")
    assert view.stage_text == "エラー"
    assert view.enabled["start"] is True
    assert view.enabled["stop"] is False
    assert presenter._cancel_event is None
    assert view.errors == [("エラー", "LLM 落ちた")]
    assert any("失敗: LLM 落ちた" in line for line in view.log)
    assert view.log[-1].startswith("Traceback")


def test_on_cancelled_button_states():
    view, presenter = _make()
    presenter._cancel_event = object()  # type: ignore[assignment]
    presenter._on_cancelled()
    assert view.stage_text == "中断しました"
    assert view.enabled["start"] is True
    assert view.enabled["stop"] is False
    assert presenter._cancel_event is None
    assert any("中断しました" in line for line in view.log)


def test_stop_sets_cancel_and_disables_stop():
    view, presenter = _make()
    import threading

    presenter._cancel_event = threading.Event()
    presenter._stop()
    assert presenter._cancel_event.is_set()
    assert view.enabled["stop"] is False
    assert any("中断を要求しました" in line for line in view.log)


def test_stop_without_active_run_is_noop():
    view, presenter = _make()
    presenter._stop()
    assert view.log == []


# --- Opening things externally --------------------------------------------------

def test_open_minutes_prefers_docx_then_md_then_folder(tmp_path):
    view, presenter = _make()
    md = tmp_path / "minutes.md"
    docx = tmp_path / "minutes.docx"
    md.write_text("x", encoding="utf-8")
    docx.write_text("x", encoding="utf-8")

    # if docx is a real file, open the docx
    presenter.minutes_docx_path = str(docx)
    presenter.minutes_path = str(md)
    presenter.result_dir = str(tmp_path)
    view.handlers["open_minutes"]()
    assert view.opened == [docx]

    # falls back to .md if docx is None
    view.opened.clear()
    presenter.minutes_docx_path = None
    view.handlers["open_minutes"]()
    assert view.opened == [md]

    # falls back to the output folder if neither docx nor .md exists
    view.opened.clear()
    presenter.minutes_docx_path = str(tmp_path / "missing.docx")
    presenter.minutes_path = str(tmp_path / "missing.md")
    view.handlers["open_minutes"]()
    assert view.opened == [tmp_path]

    # opens nothing if none of them exist
    view.opened.clear()
    presenter.result_dir = str(tmp_path / "missing_dir")
    view.handlers["open_minutes"]()
    assert view.opened == []


def test_open_folder_only_when_dir_exists(tmp_path):
    view, presenter = _make()
    presenter.result_dir = str(tmp_path / "missing")
    view.handlers["open_folder"]()
    assert view.opened == []

    presenter.result_dir = str(tmp_path)
    view.handlers["open_folder"]()
    assert view.opened == [tmp_path]


# --- Display language en (Issue #55) ------------------------------------------

def test_english_initial_state():
    view, _ = _make(language="en")
    assert view.template_name == "Not selected"
    assert "Transcription:" in view.config_summary


def test_english_update_progress_stage_text_and_log():
    view, presenter = _make(language="en")
    presenter._update_progress("transcribe", 1, 2, "recognizing")
    assert view.stage_text == "Transcription (1/2)"
    assert view.log == ["[Transcription] recognizing"]
    presenter._update_progress("done", 1, 1, "")
    assert view.stage_text == "Done"


def test_english_on_error_dialog_and_log():
    view, presenter = _make(language="en")
    presenter._on_error(RuntimeError("LLM down"), "Traceback ...")
    assert view.stage_text == "Error"
    assert view.errors == [("Error", "LLM down")]
    assert any("Failed: LLM down" in line for line in view.log)


def test_english_on_cancelled_and_success_logs():
    view, presenter = _make(language="en")
    presenter._on_cancelled()
    assert view.stage_text == "Cancelled"
    assert any("Cancelled." in line for line in view.log)

    view2, presenter2 = _make(language="en")
    presenter2._on_success(_FakeResult(warnings=["template too big"]))
    assert any("Minutes: /tmp/out/会議/minutes.md" in line for line in view2.log)
    assert any(
        "Transcript: 42 segments / Frames: 7" in line for line in view2.log
    )
    assert any("Warning: template too big" in line for line in view2.log)


def test_english_start_log_and_format_error():
    view, presenter = _make(language="en")
    view.next_video_path = "/v/m.mp4"
    view.handlers["choose_video"]()
    view._format_mode = "builtin"
    view.handlers["start"]()
    if presenter._worker:
        presenter._worker.join(timeout=2)
    assert any(
        line.startswith("Started: m.mp4") for line in view.log
    )
    assert "Minutes format: Built-in (default)" in view.log

    view2, _ = _make(language="en")
    view2.next_video_path = "/v/m.mp4"
    view2.handlers["choose_video"]()
    view2._format_mode = "file"
    view2.handlers["start"]()
    assert view2.errors and view2.errors[0][0] == "Minutes format"
