"""download_transcribe_model のテスト（実ダウンロードはせず、フェイクモジュールを注入）。"""

from __future__ import annotations

import sys
import types

from meeting_minutes.download_transcribe_model import download_transcribe_model, main
from meeting_minutes.model import transcribe
from meeting_minutes.model.config import Config


def _config(backend: str, model: str) -> Config:
    cfg = Config()
    cfg.transcribe.backend = backend
    cfg.transcribe.model = model
    return cfg


def _fake_hf(captured: dict):
    mod = types.ModuleType("huggingface_hub")

    def snapshot_download(repo, **kwargs):
        captured["repo"] = repo
        captured["kwargs"] = kwargs
        return f"/fake/cache/{repo}"

    mod.snapshot_download = snapshot_download
    return mod


def _fake_faster_whisper(captured: dict):
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model, device=None, compute_type=None):
            captured["model"] = model
            captured["device"] = device
            captured["compute_type"] = compute_type

    mod.WhisperModel = WhisperModel
    return mod


def test_download_mlx_downloads_mapped_repo(monkeypatch):
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(transcribe, "_mlx_available", lambda: True)
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "huggingface_hub", _fake_hf(captured))

    target = download_transcribe_model(_config("auto", "large-v3-turbo"))
    assert target == "mlx-community/whisper-large-v3-turbo"
    assert captured["repo"] == "mlx-community/whisper-large-v3-turbo"


def test_download_faster_whisper_instantiates_model(monkeypatch):
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: False)
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper(captured)
    )

    target = download_transcribe_model(_config("auto", "medium"))
    assert target == "medium"
    assert captured["model"] == "medium"


def test_download_explicit_faster_whisper_on_apple(monkeypatch):
    # Apple Silicon でも backend="faster-whisper" を明示したら faster-whisper 側を取得
    monkeypatch.setattr(transcribe, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(transcribe, "_mlx_available", lambda: True)
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper(captured)
    )

    target = download_transcribe_model(_config("faster-whisper", "large-v3"))
    assert target == "large-v3"
    assert captured["model"] == "large-v3"


def test_download_main_missing_config_returns_2():
    assert main(["--config", "/no/such/config.toml"]) == 2
