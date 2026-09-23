"""Transcription (fully local).

Has two backends:
    - "faster-whisper": the CTranslate2 implementation. Works on any OS, but CPU-only on a Mac.
    - "mlx": mlx-whisper, using Apple Silicon's GPU. Much faster on a Mac.

When `config.backend` is "auto," uses mlx if it's Apple Silicon and
mlx-whisper is installed; otherwise uses faster-whisper.

**Assumes the model was already fetched at setup time**, via
`scripts/setup.sh` (-> `meeting_minutes.download_transcribe_model`). At app
runtime, this module does not go fetch it from Hugging Face (`cli.py` /
`gui.py` set `HF_HUB_OFFLINE`, and this module also explicitly checks whether
the local cache has it). If it wasn't fetched, this does not auto-download —
it stops with `ModelNotAvailableError`.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, t

from .cancel import check_cancel
from .config import TranscribeConfig

# Progress callback: (completed segment count, approximate total, most recent text)
ProgressFn = Callable[[int, int, str], None]


class ModelNotAvailableError(Exception):
    """Raised when the transcription model isn't available locally and, per
    policy, isn't auto-downloaded at runtime.

    The message is already i18n'd (`pmsg.stt_model_missing`), so it can be
    shown as a single line as-is in the UI/log. Both the CLI (`cli.py`) and
    the GUI (`presenter/main.py`) catch the exception and display
    `str(exc)`, so no extra handling is needed.
    """


@dataclass
class Segment:
    """One span of transcription."""

    start: float  # seconds
    end: float  # seconds
    text: str


def _format_ts(seconds: float) -> str:
    """Format seconds as [HH:MM:SS]."""
    seconds = max(0, round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --- Choosing a backend --------------------------------------------------

def _mlx_available() -> bool:
    return importlib.util.find_spec("mlx_whisper") is not None


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def resolve_backend(config: TranscribeConfig) -> str:
    """Resolve config.backend to the actual backend name to use.

    Returns either "mlx" or "faster-whisper".
    """
    backend = (config.backend or "auto").strip().lower()
    if backend in ("mlx", "faster-whisper"):
        return backend
    if backend == "auto":
        if _is_apple_silicon() and _mlx_available():
            return "mlx"
        return "faster-whisper"
    return "faster-whisper"  # fall back for an unknown value


# mlx-whisper model names (size name -> HF repo)
_MLX_MODEL_MAP = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def _mlx_model_repo(name: str) -> str:
    """Convert a size name to the HF repo name for mlx-whisper. Anything
    containing "/" or unknown is passed through as-is."""
    if "/" in name:
        return name
    return _MLX_MODEL_MAP.get(name.strip().lower(), name)


def _mlx_model_cached(repo: str) -> bool:
    """Whether the mlx model is available (best-effort).

    If `repo` is an existing local directory, it's considered "present" (this
    matches how `_mlx_model_repo` passes through anything containing "/" or an
    unknown name unchanged — there's a use case for air-gapped distribution
    where the full model is bundled and its path is written into
    `config.model`). Otherwise, checks the HuggingFace shared cache.
    """
    if Path(repo).is_dir():
        return True
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(repo, local_files_only=True)
        return True
    except Exception:
        return False


def _faster_whisper_model_cached(config: TranscribeConfig) -> bool:
    """Whether the faster-whisper model is available (best-effort). The
    faster-whisper counterpart to `_mlx_model_cached`.

    If `config.model` is an existing local directory, it's considered
    "present" (`config.model` can be a local model path too, not just a size
    name — see the docs). Otherwise, resolves the cache via
    `download_model(..., local_files_only=True)` (doesn't load the model into
    RAM; raises if it hasn't been fetched).
    """
    if Path(config.model).is_dir():
        return True
    try:
        from faster_whisper import download_model

        download_model(config.model, local_files_only=True)
        return True
    except Exception:
        return False


# --- Public entry points ------------------------------------------------------

def transcribe_wav(
    wav_path: str | Path,
    config: TranscribeConfig,
    *,
    on_progress: ProgressFn | None = None,
    total_hint: int | None = None,
    cancel_event: threading.Event | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> tuple[list[Segment], str]:
    """Transcribe the wav and return (segments, detected_language).

    The backend (mlx / faster-whisper) is decided by `resolve_backend(config)`.
    on_progress: the progress callback. faster-whisper calls it as each
        segment is finalized; mlx calls it all at once after completion (see
        the comment on _transcribe_mlx below).
    total_hint: an estimated total segment count for the progress display
        (computed from things like audio length / average seconds per segment).
    cancel_event: interrupts if set. faster-whisper responds between segments,
        but mlx is a single blocking call, so it can only respond before the
        call starts (see _transcribe_mlx below).
    language: the language of the progress messages passed to on_progress
        ("ja" / "en"). The transcription language itself is config.language
        (a separate setting).
    detected_language (the second return value): the language Whisper
        actually decoded with — either config.language if it was set
        explicitly, or Whisper's own auto-detection result if config.language
        was left empty. Downstream, this drives the frame-analysis (VLM)
        step's output language, so on-screen content is described in the
        recording's own actual language rather than a fixed default (see
        vision.describe_frames's source_language parameter).
    """
    if resolve_backend(config) == "mlx":
        return _transcribe_mlx(
            wav_path,
            config,
            on_progress=on_progress,
            total_hint=total_hint,
            cancel_event=cancel_event,
            language=language,
        )
    return _transcribe_faster_whisper(
        wav_path,
        config,
        on_progress=on_progress,
        total_hint=total_hint,
        cancel_event=cancel_event,
        language=language,
    )


def _transcribe_faster_whisper(
    wav_path: str | Path,
    config: TranscribeConfig,
    *,
    on_progress: ProgressFn | None = None,
    total_hint: int | None = None,
    cancel_event: threading.Event | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> tuple[list[Segment], str]:
    # a heavy dependency, so imported inside the function (also makes it easier to stub in tests)
    from faster_whisper import WhisperModel

    if on_progress is not None:
        on_progress(
            0,
            total_hint or 0,
            t("pmsg.stt_preparing", language),
        )

    # If it wasn't fetched ahead of time (via scripts/setup.sh), stop here instead of going to HF.
    if not _faster_whisper_model_cached(config):
        raise ModelNotAvailableError(
            t("pmsg.stt_model_missing", language, repo=config.model)
        )

    device = config.device or "auto"
    model = WhisperModel(
        config.model,
        device=device,
        compute_type=config.compute_type,
        # Runtime guard independent of HF_HUB_OFFLINE — no auto-download even
        # if something slips past the preflight check above.
        local_files_only=True,
    )

    # the language being transcribed (separate from the display language)
    stt_lang = config.language.strip() or None
    raw_segments, info = model.transcribe(
        str(wav_path),
        language=stt_lang,
        vad_filter=True,  # drop silent stretches to improve accuracy and speed
        # Avoids feeding possibly-wrong prior output as context, which can
        # cause a "repetition loop" hallucination during singing/BGM/noise.
        condition_on_previous_text=False,
    )

    segments: list[Segment] = []
    approx_total = total_hint or 0
    for i, seg in enumerate(_iter_segments(raw_segments), start=1):
        # Segments are generated lazily, so stopping here also halts further generation.
        check_cancel(cancel_event)
        segments.append(seg)
        if approx_total and i > approx_total:
            approx_total = i
        if on_progress is not None:
            on_progress(i, approx_total or i, seg.text)
    detected_language = getattr(info, "language", None) or stt_lang or "ja"
    return segments, detected_language


def _transcribe_mlx(
    wav_path: str | Path,
    config: TranscribeConfig,
    *,
    on_progress: ProgressFn | None = None,
    total_hint: int | None = None,
    cancel_event: threading.Event | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> tuple[list[Segment], str]:
    # mlx-whisper returns everything at once (not a generator) — no
    # incremental progress possible. Explain once at the start; on_progress
    # then fires while converting segments after completion (bar jumps 0->100).
    import mlx_whisper

    repo = _mlx_model_repo(config.model)

    # Cancellation takes top priority — checked before the model check.
    check_cancel(cancel_event)

    # Stop here instead of going to HF if not pre-fetched (scripts/setup.sh)
    # — mlx_whisper.transcribe has no local_files_only equivalent.
    if not _mlx_model_cached(repo):
        raise ModelNotAvailableError(
            t("pmsg.stt_model_missing", language, repo=repo)
        )

    if on_progress is not None:
        on_progress(0, total_hint or 0, t("pmsg.stt_mlx_running", language))

    # mlx-whisper is a single blocking call, so it can't respond to
    # cancellation while it's running. Only checked before the call.
    check_cancel(cancel_event)

    # the language being transcribed (separate from the display language)
    stt_lang = config.language.strip() or None
    result = mlx_whisper.transcribe(
        str(wav_path),
        path_or_hf_repo=repo,
        language=stt_lang,
        word_timestamps=False,
        # Avoids feeding possibly-wrong prior output as context (repetition-
        # loop hallucination risk). More important here since mlx-whisper has
        # no vad_filter-equivalent silence removal.
        condition_on_previous_text=False,
    )

    raw = result.get("segments", []) if isinstance(result, dict) else []
    total = total_hint or len(raw)
    segments: list[Segment] = []
    for i, s in enumerate(raw, start=1):
        seg = Segment(
            start=float(s.get("start", 0.0)),
            end=float(s.get("end", 0.0)),
            text=(s.get("text") or "").strip(),
        )
        segments.append(seg)
        if on_progress is not None:
            on_progress(i, total or i, seg.text)
    detected_language = (
        (result.get("language") if isinstance(result, dict) else None)
        or stt_lang
        or "ja"
    )
    return segments, detected_language


def _iter_segments(raw: Iterable) -> Iterable[Segment]:
    for s in raw:
        yield Segment(start=float(s.start), end=float(s.end), text=(s.text or "").strip())


def save_transcript(
    segments: list[Segment], out_dir: str | Path, *, language: str = ""
) -> tuple[Path, Path]:
    """Save the transcript as both JSON and plain text.

    language: the detected/used transcription language (see transcribe_wav's
        second return value), persisted so a resumed run
        (pipeline.run(reuse=True)) can recover it via load_transcript
        without re-transcribing.
    Returns (json_path, txt_path).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "transcript.json"
    txt_path = out_dir / "transcript.txt"

    json_path.write_text(
        json.dumps(
            {"language": language, "segments": [asdict(s) for s in segments]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    txt_path.write_text(transcript_to_text(segments), encoding="utf-8")
    return json_path, txt_path


def transcript_to_text(segments: list[Segment]) -> str:
    """Format as readable text with a [HH:MM:SS] timestamp at the start of each line."""
    lines = [f"[{_format_ts(s.start)}] {s.text}" for s in segments if s.text]
    return "\n".join(lines) + ("\n" if lines else "")


def load_transcript(out_dir: str | Path) -> tuple[list[Segment], str]:
    """Read back the transcript.json written by save_transcript (for resuming).

    Returns (segments, detected_language). transcript.json files written
    before this field existed are a bare JSON array with no language key;
    detected_language comes back as "" in that case, and the caller falls
    back to config.transcribe.language or "ja".
    """
    path = Path(out_dir) / "transcript.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # pre-existing format, no language field
        raw_segments, language = data, ""
    else:
        raw_segments, language = data.get("segments", []), data.get("language", "")
    segments = [
        Segment(
            start=float(d["start"]), end=float(d["end"]), text=(d.get("text") or "")
        )
        for d in raw_segments
    ]
    return segments, language
