"""Tests for transcribe's backend selection, model-name conversion, and each implementation.

The real mlx-whisper / faster-whisper are replaced by injecting fakes into
sys.modules. Neither network access nor a real model is needed.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from meeting_minutes.model import transcribe
from meeting_minutes.model.cancel import PipelineCancelled
from meeting_minutes.model.config import TranscribeConfig
from meeting_minutes.model.transcribe import (
    ModelNotAvailableError,
    Segment,
    _mlx_model_repo,
    _transcribe_faster_whisper,
    _transcribe_mlx,
    load_transcript,
    resolve_backend,
    save_transcript,
)

# The real thing, before the fixture stubs it (for tests that want the
# local-directory check to actually take effect)
_REAL_MLX_MODEL_CACHED = transcribe._mlx_model_cached
_REAL_FW_MODEL_CACHED = transcribe._faster_whisper_model_cached


@pytest.fixture(autouse=True)
def _models_available(monkeypatch):
    """By default, treats the model as "already fetched." Tests that examine
    the not-yet-fetched behavior override this to False individually. Never
    touches the real HF cache or network."""
    monkeypatch.setattr(transcribe, "_mlx_model_cached", lambda repo: True)
    monkeypatch.setattr(
        transcribe, "_faster_whisper_model_cached", lambda config: True
    )

# --- _mlx_model_repo ---------------------------------------------------

def test_mlx_model_repo_known_sizes():
    assert _mlx_model_repo("large-v3") == "mlx-community/whisper-large-v3-mlx"
    assert _mlx_model_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    assert _mlx_model_repo("MEDIUM") == "mlx-community/whisper-medium-mlx"


def test_mlx_model_repo_passthrough():
    # a full HF repo name is passed through unchanged
    assert _mlx_model_repo("mlx-community/whisper-foo") == "mlx-community/whisper-foo"
    # an unknown size name is also passed through unchanged (respecting the user's choice)
    assert _mlx_model_repo("distil-large-v3") == "distil-large-v3"


# --- resolve_backend ------------------------------------------------

@pytest.mark.parametrize("explicit", ["mlx", "faster-whisper"])
def test_resolve_backend_explicit_wins(explicit):
    cfg = TranscribeConfig(backend=explicit)
    assert resolve_backend(cfg) == explicit


def test_resolve_backend_auto_on_apple_silicon_with_mlx(monkeypatch):
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(transcribe, "_mlx_available", lambda: True)
    assert resolve_backend(TranscribeConfig(backend="auto")) == "mlx"


def test_resolve_backend_auto_falls_back_without_mlx(monkeypatch):
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(transcribe, "_mlx_available", lambda: False)
    assert resolve_backend(TranscribeConfig(backend="auto")) == "faster-whisper"


def test_resolve_backend_auto_non_apple(monkeypatch):
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: False)
    monkeypatch.setattr(transcribe, "_mlx_available", lambda: True)
    assert resolve_backend(TranscribeConfig(backend="auto")) == "faster-whisper"


def test_resolve_backend_unknown_value():
    assert resolve_backend(TranscribeConfig(backend="whatever")) == "faster-whisper"


# --- _transcribe_mlx (a fake mlx_whisper) ---------------------------

def _fake_mlx_module(captured: dict):
    mod = types.ModuleType("mlx_whisper")

    def transcribe_fn(
        path,
        *,
        path_or_hf_repo=None,
        language=None,
        word_timestamps=None,
        condition_on_previous_text=None,
    ):
        captured["path"] = path
        captured["repo"] = path_or_hf_repo
        captured["language"] = language
        captured["condition_on_previous_text"] = condition_on_previous_text
        return {
            "text": "こんにちは 本題です",
            "language": "ja",
            "segments": [
                {"start": 0.0, "end": 2.0, "text": " こんにちは"},
                {"start": 2.0, "end": 5.0, "text": " 本題です"},
            ],
        }

    mod.transcribe = transcribe_fn
    return mod


def test_transcribe_mlx_parses_segments_and_reports(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "mlx_whisper", _fake_mlx_module(captured))

    progress: list[tuple] = []
    cfg = TranscribeConfig(backend="mlx", model="large-v3", language="ja")
    segs = _transcribe_mlx(
        "/tmp/a.wav",
        cfg,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
        total_hint=10,
    )

    assert segs == [
        Segment(0.0, 2.0, "こんにちは"),
        Segment(2.0, 5.0, "本題です"),
    ]
    # the size name is converted to the repo before being passed
    assert captured["repo"] == "mlx-community/whisper-large-v3-mlx"
    assert captured["language"] == "ja"
    # the repetition-loop hallucination guard (see DESIGN.md)
    assert captured["condition_on_previous_text"] is False
    # the explanation at the start + a notification per segment
    assert progress[0][0] == 0
    assert [p[0] for p in progress[1:]] == [1, 2]


def test_transcribe_wav_dispatches_to_mlx(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "mlx_whisper", _fake_mlx_module(captured))
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(transcribe, "_mlx_available", lambda: True)

    segs = transcribe.transcribe_wav("/tmp/a.wav", TranscribeConfig(backend="auto"))
    assert len(segs) == 2
    assert captured["repo"].startswith("mlx-community/")


# --- _transcribe_faster_whisper (a fake faster_whisper) ------------

class _FWSeg:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def _fake_faster_whisper_module(captured: dict):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(
            self, model, device=None, compute_type=None, local_files_only=None
        ):
            captured["model"] = model
            captured["device"] = device
            captured["compute_type"] = compute_type
            captured["local_files_only"] = local_files_only

        def transcribe(
            self, path, language=None, vad_filter=None, condition_on_previous_text=None
        ):
            captured["language"] = language
            captured["vad_filter"] = vad_filter
            captured["condition_on_previous_text"] = condition_on_previous_text
            segs = iter([_FWSeg(0.0, 1.0, " a"), _FWSeg(1.0, 2.0, " b"), _FWSeg(2.0, 3.0, " c")])
            return segs, {"language": "ja"}

    mod.WhisperModel = WhisperModel
    return mod


def test_transcribe_faster_whisper_streams_segments(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper_module(captured)
    )

    progress: list[tuple] = []
    cfg = TranscribeConfig(backend="faster-whisper", model="small", compute_type="int8")
    segs = _transcribe_faster_whisper(
        "/tmp/a.wav",
        cfg,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
        total_hint=3,
    )

    assert [s.text for s in segs] == ["a", "b", "c"]
    assert captured["model"] == "small"
    assert captured["vad_filter"] is True
    assert captured["condition_on_previous_text"] is False
    # a runtime guard that doesn't depend on the environment variable (no auto-download)
    assert captured["local_files_only"] is True
    # the first is the "model preparing" notification (current=0), followed by one per finalized segment
    assert progress[0][0] == 0
    assert [p[0] for p in progress[1:]] == [1, 2, 3]


def test_transcribe_faster_whisper_missing_model_raises(monkeypatch):
    # not fetched ahead of time -> stops with ModelNotAvailableError instead of auto-downloading
    monkeypatch.setattr(
        transcribe, "_faster_whisper_model_cached", lambda config: False
    )
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper_module(captured)
    )
    with pytest.raises(ModelNotAvailableError) as ei:
        _transcribe_faster_whisper("/tmp/a.wav", TranscribeConfig(model="small"))
    assert "small" in str(ei.value)
    assert "setup.sh" in str(ei.value)
    # never reaches constructing WhisperModel
    assert "model" not in captured


def test_transcribe_mlx_missing_model_raises(monkeypatch):
    monkeypatch.setattr(transcribe, "_mlx_model_cached", lambda repo: False)
    called = {"n": 0}

    def transcribe_fn(*a, **k):
        called["n"] += 1
        return {"segments": []}

    mod = types.ModuleType("mlx_whisper")
    mod.transcribe = transcribe_fn
    monkeypatch.setitem(sys.modules, "mlx_whisper", mod)

    with pytest.raises(ModelNotAvailableError) as ei:
        _transcribe_mlx("/tmp/a.wav", TranscribeConfig(model="large-v3"))
    # the size name is converted to the repo and included in the message
    assert "mlx-community/whisper-large-v3-mlx" in str(ei.value)
    assert called["n"] == 0  # transcription never runs


def test_faster_whisper_local_model_dir_reaches_body(monkeypatch, tmp_path):
    # when config.model is a local model directory, skips HF resolution and
    # reaches the body (the use case for air-gapped distribution with the model bundled).
    monkeypatch.setattr(transcribe, "_faster_whisper_model_cached", _REAL_FW_MODEL_CACHED)
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper_module(captured)
    )
    cfg = TranscribeConfig(backend="faster-whisper", model=str(tmp_path))
    segs = _transcribe_faster_whisper("/tmp/a.wav", cfg, total_hint=3)
    assert [s.text for s in segs] == ["a", "b", "c"]
    assert captured["model"] == str(tmp_path)
    assert captured["local_files_only"] is True


def test_mlx_local_model_dir_reaches_body(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_mlx_model_cached", _REAL_MLX_MODEL_CACHED)
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "mlx_whisper", _fake_mlx_module(captured))
    cfg = TranscribeConfig(backend="mlx", model=str(tmp_path), language="ja")
    segs = _transcribe_mlx("/tmp/a.wav", cfg, total_hint=10)
    assert [s.text for s in segs] == ["こんにちは", "本題です"]
    # the local path is passed through unchanged by _mlx_model_repo and reaches path_or_hf_repo as-is
    assert captured["repo"] == str(tmp_path)


def test_transcribe_faster_whisper_message_translated_when_language_en(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper_module(captured)
    )
    progress: list[tuple] = []
    cfg = TranscribeConfig(backend="faster-whisper", model="small", language="ja")
    _transcribe_faster_whisper(
        "/tmp/a.wav", cfg,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
        total_hint=3,
        language="en",
    )
    assert "Preparing the transcription model" in progress[0][2]
    assert "準備中" not in progress[0][2]
    # the transcribed language (config.language) is independent of the display language
    assert captured["language"] == "ja"


def test_transcribe_mlx_message_translated_when_language_en(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "mlx_whisper", _fake_mlx_module(captured))
    progress: list[tuple] = []
    cfg = TranscribeConfig(backend="mlx", model="large-v3", language="ja")
    _transcribe_mlx(
        "/tmp/a.wav", cfg,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
        total_hint=10,
        language="en",
    )
    assert "mlx-whisper" in progress[0][2]
    assert "文字起こし中" not in progress[0][2]
    assert captured["language"] == "ja"


def test_save_then_load_transcript_roundtrip(tmp_path):
    segs = [Segment(0.0, 2.5, "こんにちは"), Segment(2.5, 5.0, "本題です")]
    save_transcript(segs, tmp_path)
    loaded = load_transcript(tmp_path)
    assert loaded == segs


# --- Cancellation (cancel_event) ------------------------------------------------

def test_transcribe_mlx_cancel_before_call_skips_transcribe(monkeypatch):
    called = {"n": 0}

    def transcribe_fn(*a, **k):
        called["n"] += 1
        return {"segments": []}

    mod = types.ModuleType("mlx_whisper")
    mod.transcribe = transcribe_fn
    monkeypatch.setitem(sys.modules, "mlx_whisper", mod)

    cancel_event = threading.Event()
    cancel_event.set()
    with pytest.raises(PipelineCancelled):
        _transcribe_mlx(
            "/tmp/a.wav", TranscribeConfig(model="large-v3"), cancel_event=cancel_event
        )
    assert called["n"] == 0  # cancelled before it's called


def test_transcribe_faster_whisper_cancel_stops_after_first_segment(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper_module(captured)
    )

    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            # simulate the Stop button being pressed right after the first
            # segment finishes
            cancel_event.set()

    with pytest.raises(PipelineCancelled):
        _transcribe_faster_whisper(
            "/tmp/a.wav",
            TranscribeConfig(model="small"),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
