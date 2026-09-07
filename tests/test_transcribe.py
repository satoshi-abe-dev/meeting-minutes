"""transcribe のバックエンド選択・モデル名変換・各実装のテスト。

実際の mlx-whisper / faster-whisper は sys.modules にフェイクを注入して置き換える。
ネットワークもモデルも要らない。
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
    Segment,
    _mlx_model_repo,
    _transcribe_faster_whisper,
    _transcribe_mlx,
    load_transcript,
    resolve_backend,
    save_transcript,
)


# --- _mlx_model_repo ---------------------------------------------------

def test_mlx_model_repo_known_sizes():
    assert _mlx_model_repo("large-v3") == "mlx-community/whisper-large-v3-mlx"
    assert _mlx_model_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    assert _mlx_model_repo("MEDIUM") == "mlx-community/whisper-medium-mlx"


def test_mlx_model_repo_passthrough():
    # フル HF リポジトリ名はそのまま
    assert _mlx_model_repo("mlx-community/whisper-foo") == "mlx-community/whisper-foo"
    # 未知のサイズ名もそのまま（利用者の指定を尊重）
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


# --- _transcribe_mlx（フェイク mlx_whisper）---------------------------

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
    # サイズ名がリポジトリへ変換されて渡る
    assert captured["repo"] == "mlx-community/whisper-large-v3-mlx"
    assert captured["language"] == "ja"
    # repetition loop 幻覚対策（DESIGN.md 参照）
    assert captured["condition_on_previous_text"] is False
    # 開始時の説明 + セグメントごとの通知
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


# --- _transcribe_faster_whisper（フェイク faster_whisper）------------

class _FWSeg:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def _fake_faster_whisper_module(captured: dict):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model, device=None, compute_type=None):
            captured["model"] = model
            captured["device"] = device
            captured["compute_type"] = compute_type

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
    # 先頭は「モデル準備中」の通知（current=0）、その後セグメント確定ごとに逐次通知
    assert progress[0][0] == 0
    assert [p[0] for p in progress[1:]] == [1, 2, 3]


def test_save_then_load_transcript_roundtrip(tmp_path):
    segs = [Segment(0.0, 2.5, "こんにちは"), Segment(2.5, 5.0, "本題です")]
    save_transcript(segs, tmp_path)
    loaded = load_transcript(tmp_path)
    assert loaded == segs


# --- 中断（cancel_event）------------------------------------------------

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
    assert called["n"] == 0  # 呼ばれる前に中断される


def test_transcribe_faster_whisper_cancel_stops_after_first_segment(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper_module(captured)
    )

    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()  # 1 区間目が終わった直後に中断ボタンが押された想定

    with pytest.raises(PipelineCancelled):
        _transcribe_faster_whisper(
            "/tmp/a.wav",
            TranscribeConfig(model="small"),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
