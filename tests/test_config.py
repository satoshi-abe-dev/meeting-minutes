"""config.load_config のテスト（デフォルト / TOML / 環境変数の優先順位）。"""

from __future__ import annotations

import textwrap

from meeting_minutes.model.config import Config, load_config


def test_defaults_when_no_file(tmp_path, monkeypatch):
    # デフォルト探索を空ディレクトリに向けて「ファイルなし」状態にする
    monkeypatch.setattr("meeting_minutes.model.config.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "meeting_minutes.model.config.default_config_path", lambda: None
    )
    cfg = load_config(None)
    assert isinstance(cfg, Config)
    assert cfg.llm.base_url == "http://localhost:1234/v1"
    assert cfg.transcribe.backend == "auto"
    assert cfg.transcribe.model == "large-v3-turbo"
    assert cfg.frames.max_frames == 60


def test_load_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            """
            [llm]
            base_url = "http://localhost:9999/v1"
            model = "my-llm"
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
    assert cfg.llm.base_url == "http://localhost:9999/v1"
    assert cfg.llm.model == "my-llm"
    assert cfg.llm.max_tokens == 111
    assert cfg.transcribe.backend == "mlx"
    assert cfg.transcribe.model == "small"
    assert cfg.frames.interval_sec == 30.0
    # 未知キーは無視され、他のデフォルトは維持される
    assert cfg.llm.vlm_model == "qwen2-vl-7b-instruct"


def test_env_overrides_toml(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        '[llm]\nmodel = "from-toml"\n\n[transcribe]\nbackend = "mlx"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MM_LLM_MODEL", "from-env")
    monkeypatch.setenv("MM_FRAMES_MAX_FRAMES", "5")
    monkeypatch.setenv("MM_TRANSCRIBE_BACKEND", "faster-whisper")
    cfg = load_config(p)
    assert cfg.llm.model == "from-env"
    assert cfg.frames.max_frames == 5
    assert cfg.transcribe.backend == "faster-whisper"


def test_output_root_relative_to_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("meeting_minutes.model.config.REPO_ROOT", tmp_path)
    p = tmp_path / "config.toml"
    p.write_text('[output]\ndir = "out"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.output_root == tmp_path / "out"


def test_output_template_path_default_and_toml_and_env(tmp_path, monkeypatch):
    # 既定は空文字（内蔵テンプレート）
    monkeypatch.setattr("meeting_minutes.model.config.default_config_path", lambda: None)
    assert load_config(None).output.template_path == ""

    p = tmp_path / "config.toml"
    p.write_text('[output]\ntemplate_path = "tpl/顧客A.txt"\n', encoding="utf-8")
    assert load_config(p).output.template_path == "tpl/顧客A.txt"

    monkeypatch.setenv("MM_OUTPUT_TEMPLATE_PATH", "tpl/env.txt")
    assert load_config(p).output.template_path == "tpl/env.txt"
