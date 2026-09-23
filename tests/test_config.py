"""Tests for config.load_config (the priority of defaults / TOML / environment variables)."""

from __future__ import annotations

import textwrap

from meeting_minutes.model.config import Config, load_config


def test_defaults_when_no_file(tmp_path, monkeypatch):
    # point the default search at an empty directory to simulate "no file"
    monkeypatch.setattr("meeting_minutes.model.config.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "meeting_minutes.model.config.default_config_path", lambda: None
    )
    cfg = load_config(None)
    assert isinstance(cfg, Config)
    assert cfg.ai.base_url == "http://localhost:1234/v1"
    assert cfg.transcribe.backend == "auto"
    assert cfg.transcribe.model == "large-v3-turbo"
    assert cfg.frames.max_frames == 60


def test_load_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            """
            [ai]
            base_url = "http://localhost:9999/v1"
            llm_model = "my-llm"
            max_tokens = 111

            [transcribe]
            backend = "mlx"
            model = "small"

            [frames]
            interval_sec = 30
            unknown_key = "ignored"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.ai.base_url == "http://localhost:9999/v1"
    assert cfg.ai.llm_model == "my-llm"
    assert cfg.ai.max_tokens == 111
    assert cfg.transcribe.backend == "mlx"
    assert cfg.transcribe.model == "small"
    assert cfg.frames.interval_sec == 30.0
    # unknown keys are ignored, and the other defaults are kept
    assert cfg.ai.vlm_model == "qwen2-vl-7b-instruct"


def test_env_overrides_toml(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[ai]\nllm_model = "from-toml"\n\n[transcribe]\nbackend = "mlx"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MM_AI_LLM_MODEL", "from-env")
    monkeypatch.setenv("MM_FRAMES_MAX_FRAMES", "5")
    monkeypatch.setenv("MM_TRANSCRIBE_BACKEND", "faster-whisper")
    cfg = load_config(p)
    assert cfg.ai.llm_model == "from-env"
    assert cfg.frames.max_frames == 5
    assert cfg.transcribe.backend == "faster-whisper"


def test_output_root_relative_to_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("meeting_minutes.model.config.REPO_ROOT", tmp_path)
    p = tmp_path / "config.toml"
    p.write_text('[output]\ndir = "out"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.output_root == tmp_path / "out"


def test_output_template_path_default_and_toml_and_env(tmp_path, monkeypatch):
    # default is an empty string (the built-in template)
    monkeypatch.setattr("meeting_minutes.model.config.default_config_path", lambda: None)
    assert load_config(None).output.template_path == ""

    p = tmp_path / "config.toml"
    p.write_text('[output]\ntemplate_path = "tpl/顧客A.txt"\n', encoding="utf-8")
    assert load_config(p).output.template_path == "tpl/顧客A.txt"

    monkeypatch.setenv("MM_OUTPUT_TEMPLATE_PATH", "tpl/env.txt")
    assert load_config(p).output.template_path == "tpl/env.txt"


def test_gui_language_default_toml_env_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("meeting_minutes.model.config.default_config_path", lambda: None)
    # the default is "en"
    assert load_config(None).gui.language == "en"

    p = tmp_path / "config.toml"
    p.write_text('[gui]\nlanguage = "ja"\n', encoding="utf-8")
    assert load_config(p).gui.language == "ja"

    # environment variable overrides TOML
    monkeypatch.setenv("MM_GUI_LANGUAGE", "en")
    assert load_config(p).gui.language == "en"
    monkeypatch.delenv("MM_GUI_LANGUAGE")

    # an unsupported value falls back to "en" (via TOML)
    p.write_text('[gui]\nlanguage = "fr"\n', encoding="utf-8")
    assert load_config(p).gui.language == "en"

    # an unsupported value falls back to "en" (via environment variable)
    p.write_text('[gui]\nlanguage = "ja"\n', encoding="utf-8")
    monkeypatch.setenv("MM_GUI_LANGUAGE", "de")
    assert load_config(p).gui.language == "en"
