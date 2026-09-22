"""Loads configuration.

Priority (strongest first):
    1. Environment variables (the MM_ prefix)
    2. A TOML file (config.toml by default, or config.example.toml if that doesn't exist)
    3. Default values in the code

TOML is read with the standard library's tomllib (Python 3.11+). No extra dependency.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import cast

from meeting_minutes.i18n import normalize_language

# The repo root (this file is src/meeting_minutes/model/config.py, so 3 levels up)
REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts"


def _to_bool(raw: str) -> bool:
    """A boolean from an environment variable. Only "1"/"true"/"yes"/"on" (case-insensitive) are True."""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def load_prompt(name: str) -> str:
    """Read prompts/<name> and return it as a string."""
    path = PROMPTS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"プロンプトファイルが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


@dataclass
class AIConfig:
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "local-no-key"
    llm_model: str = "qwen2.5-7b-instruct"
    vlm_model: str = "qwen2-vl-7b-instruct"
    # A large local model can take minutes for a single request (the final
    # merge step of minutes generation especially, since it outputs many
    # tokens and tends to take a while), so this is set generously.
    timeout: float = 600.0
    # Reasoning models like the Qwen3 family spend tokens from this cap on
    # invisible "thinking" before writing the visible answer. If it's too
    # small, they can use it all up on thinking and return an empty body, so
    # this is set higher than for an ordinary model.
    max_tokens: int = 8192
    # The upper bound (in characters) on a transcript for "one-shot"
    # generation of the minutes. Beyond this, switches to the split mode of
    # chunk summarization -> merge. The default 40000 assumes the LLM is run
    # with a context of roughly 32k. If Context Length can't be raised on the
    # LM Studio side, lower this (e.g. 8000). -> docs/models_ja.md "Setting the context length"
    chunk_trigger_chars: int = 20000
    # Character count of one chunk in split mode.
    chunk_size_chars: int = 12000
    # The loaded model's real context length (in tokens). 0 means auto-detect
    # (via LM Studio's /api/v0/models). On a backend where it can't be
    # detected, and you're using something other than 32k, write the real
    # value here.
    context_tokens: int = 0


@dataclass
class TranscribeConfig:
    # Transcription engine: "auto" (mlx on Apple Silicon, faster-whisper
    # elsewhere) / "mlx" / "faster-whisper"
    backend: str = "auto"
    # The model's size name (large-v3-turbo / large-v3 / medium / small ...).
    # Converted per-backend into the actual thing to fetch (e.g. an HF repo
    # name). A string containing "/" is used as-is as a full repo name.
    # The default, large-v3-turbo, is nearly as accurate as large-v3 while
    # running faster, and works on either the faster-whisper or mlx backend.
    model: str = "large-v3-turbo"
    # The next two only take effect with faster-whisper (ignored by mlx)
    compute_type: str = "int8"
    device: str = "auto"
    language: str = "ja"  # empty string means auto-detect


@dataclass
class FramesConfig:
    interval_sec: float = 15.0
    scene_threshold: float = 0.3
    max_frames: int = 60
    min_gap_sec: float = 4.0


@dataclass
class OutputConfig:
    dir: str = "output"
    # Path to a custom template that replaces the minutes "structure." Empty
    # means the built-in template. Setting this makes it used automatically
    # every time (it can also be overridden for a single run from the GUI's
    # dropdown). If it doesn't exist, can't be read, or is empty, falls back
    # to the built-in template with a warning.
    template_path: str = ""
    # "Auto" mode: has the LLM auto-generate the minutes' heading structure to
    # match the video content. If True, takes priority over template_path
    # (priority order: auto > file > built-in). The generated structure is
    # saved to output/<video name>/work/structure_used.txt, and if you like
    # it, can be copied into templates/ to reuse as a fixed template. If
    # generation fails, falls back to the built-in template with a warning.
    auto_structure: bool = False


@dataclass
class GuiConfig:
    # The GUI's display language. "ja" / "en". Anything else is rounded to
    # "ja" by load_config.
    # Can be overridden per launch via gui.py's --lang (priority order:
    # --lang > config/env > default).
    # Only affects the GUI's on-screen text (the transcription language, LLM
    # prompts, and minutes content are separate).
    language: str = "ja"


@dataclass
class Config:
    ai: AIConfig = field(default_factory=AIConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    frames: FramesConfig = field(default_factory=FramesConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)

    @property
    def output_root(self) -> Path:
        """Return the output root as an absolute path (a relative one is resolved against the repo root)."""
        p = Path(self.output.dir).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)


# Environment variable -> (section, key, conversion function) mapping
_ENV_MAP: dict[str, tuple[str, str, Callable[[str], object]]] = {
    "MM_AI_BASE_URL": ("ai", "base_url", str),
    "MM_AI_API_KEY": ("ai", "api_key", str),
    "MM_AI_LLM_MODEL": ("ai", "llm_model", str),
    "MM_AI_VLM_MODEL": ("ai", "vlm_model", str),
    "MM_AI_TIMEOUT": ("ai", "timeout", float),
    "MM_AI_MAX_TOKENS": ("ai", "max_tokens", int),
    "MM_AI_CHUNK_TRIGGER_CHARS": ("ai", "chunk_trigger_chars", int),
    "MM_AI_CHUNK_SIZE_CHARS": ("ai", "chunk_size_chars", int),
    "MM_AI_CONTEXT_TOKENS": ("ai", "context_tokens", int),
    "MM_TRANSCRIBE_BACKEND": ("transcribe", "backend", str),
    "MM_TRANSCRIBE_MODEL": ("transcribe", "model", str),
    "MM_TRANSCRIBE_COMPUTE_TYPE": ("transcribe", "compute_type", str),
    "MM_TRANSCRIBE_DEVICE": ("transcribe", "device", str),
    "MM_TRANSCRIBE_LANGUAGE": ("transcribe", "language", str),
    "MM_FRAMES_INTERVAL_SEC": ("frames", "interval_sec", float),
    "MM_FRAMES_SCENE_THRESHOLD": ("frames", "scene_threshold", float),
    "MM_FRAMES_MAX_FRAMES": ("frames", "max_frames", int),
    "MM_FRAMES_MIN_GAP_SEC": ("frames", "min_gap_sec", float),
    "MM_OUTPUT_DIR": ("output", "dir", str),
    "MM_OUTPUT_TEMPLATE_PATH": ("output", "template_path", str),
    "MM_OUTPUT_AUTO_STRUCTURE": ("output", "auto_structure", _to_bool),
    "MM_GUI_LANGUAGE": ("gui", "language", str),
}

_SECTION_TYPES = {
    "ai": AIConfig,
    "transcribe": TranscribeConfig,
    "frames": FramesConfig,
    "output": OutputConfig,
    "gui": GuiConfig,
}


def default_config_path() -> Path | None:
    """Decide which TOML file to use. config.toml > config.example.toml > none."""
    for name in ("config.toml", "config.example.toml"):
        p = REPO_ROOT / name
        if p.is_file():
            return p
    return None


def _build_section(section_cls: type, raw: dict) -> object:
    """Build a dataclass section from a dict. Unknown keys are ignored; types are loosely coerced."""
    known = {f.name: f for f in fields(section_cls)}
    kwargs = {}
    for key, value in raw.items():
        if key not in known:
            continue  # silently drop unknown keys (forward compatibility)
        target_type = known[key].type
        try:
            if target_type in ("int", int):
                value = int(value)
            elif target_type in ("float", float):
                value = float(value)
            elif target_type in ("str", str):
                value = str(value)
        except (TypeError, ValueError):
            pass
        kwargs[key] = value
    return section_cls(**kwargs)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load the configuration.

    path: path to the TOML file. Uses default_config_path() if None.
    """
    toml_path: Path | None
    if path is not None:
        toml_path = Path(path)
        if not toml_path.is_file():
            raise FileNotFoundError(f"設定ファイルが見つかりません: {toml_path}")
    else:
        toml_path = default_config_path()

    data: dict = {}
    if toml_path is not None:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    sections: dict[str, object] = {}
    for name, cls in _SECTION_TYPES.items():
        sections[name] = _build_section(cls, data.get(name, {}) or {})

    # override with environment variables
    for env_name, (section, key, caster) in _ENV_MAP.items():
        if env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        try:
            casted = caster(raw)
        except (TypeError, ValueError):
            casted = raw
        setattr(sections[section], key, casted)

    # Round an unsupported GUI language value to the default (ja), whether it came via TOML or an environment variable.
    gui = cast(GuiConfig, sections["gui"])
    gui.language = normalize_language(getattr(gui, "language", None))

    return Config(**sections)  # type: ignore[arg-type]
