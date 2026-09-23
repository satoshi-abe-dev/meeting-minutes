"""Orchestrates every stage.

    video -> audio extraction -> transcription -> frame extraction ->
    frame analysis (VLM) -> minutes generation

Each stage's dependency is swappable via Deps, and the GUI / CLI / tests all
call the same run(). Progress is reported via the on_progress callback.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, format_elapsed, normalize_language, t

from . import audio as _audio
from . import docx_export as _docx_export
from . import ffmpeg_utils as _ffmpeg_utils
from . import frames as _frames
from . import minutes as _minutes
from . import transcribe as _transcribe
from . import vision as _vision
from .cancel import PipelineCancelled, check_cancel  # noqa: F401 - re-exported for callers
from .config import Config
from .llm_client import LLMClient

# on_progress(stage, current, total, message)
#   stage: "preflight" | "audio" | "transcribe" | "frames" | "vision" | "minutes" | "done"
#   current/total: progress within that stage (total=0 means unknown)
ProgressFn = Callable[[str, int, int, str], None]

STAGES = ("preflight", "audio", "transcribe", "frames", "vision", "minutes")


def _default_make_client(cfg: Config) -> LLMClient:
    return LLMClient(cfg.ai)


@dataclass
class Deps:
    """Each stage's implementation. Tests swap these out.

    Because the __init__ the dataclass generates assigns each value to an
    instance attribute, plain functions can be used directly as defaults
    without turning into bound methods (descriptors).
    """

    extract_audio: Callable = _audio.extract_audio
    transcribe_wav: Callable = _transcribe.transcribe_wav
    save_transcript: Callable = _transcribe.save_transcript
    load_transcript: Callable = _transcribe.load_transcript
    extract_frames: Callable = _frames.extract_frames
    save_frame_index: Callable = _frames.save_frame_index
    load_frames: Callable = _frames.load_frames
    describe_frames: Callable = _vision.describe_frames
    save_frame_notes: Callable = _vision.save_frame_notes
    generate_minutes: Callable = _minutes.generate_minutes
    save_minutes: Callable = _minutes.save_minutes
    save_minutes_docx: Callable = _docx_export.save_minutes_docx
    make_client: Callable[[Config], LLMClient] = _default_make_client
    probe_duration: Callable = _ffmpeg_utils.probe_duration


@dataclass
class PipelineResult:
    video_path: Path
    out_dir: Path
    minutes_path: Path
    transcript_txt: Path
    transcript_json: Path
    frames_index: Path | None
    frame_notes: Path | None
    n_segments: int
    n_frames: int
    warnings: list[str] = field(default_factory=list)
    minutes_docx_path: Path | None = None


def _noop(stage: str, current: int, total: int, message: str) -> None:
    pass


def _format_duration(seconds: float) -> str:
    if not seconds:
        return "（不明）"
    seconds = round(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"約 {h} 時間 {m} 分"
    if m:
        return f"約 {m} 分 {s} 秒"
    return f"約 {s} 秒"


def run(
    video_path: str | Path,
    config: Config,
    on_progress: ProgressFn | None = None,
    *,
    deps: Deps | None = None,
    reuse: bool = True,
    cancel_event: threading.Event | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> PipelineResult:
    """Process one video and write out the minutes.

    reuse: if True, reuses the previous run's transcript/transcript.json /
        frames/frames.json under `output/<video name>/` if present, and
        doesn't redo transcription/frame extraction (speeds up a re-run after
        a failure at, say, the VLM stage). With False, always starts from
        scratch.
    cancel_event: if set, raises PipelineCancelled to interrupt. Responds
        before each stage starts, per frame during frame analysis, and per
        chunk during minutes generation. Cannot respond while an mlx-whisper
        call or an ffmpeg run is in progress (see docs/DESIGN_ja.md).
    language: the language of the progress messages / error hints passed to
        on_progress ("ja" / "en"). Defaults to "en". The GUI passes "ja" when
        run with --lang ja; the CLI doesn't pass it (so it's en). Does not
        affect the transcription language (config.transcribe.language) or the
        content of the minutes.
    """
    language = normalize_language(language)
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(t("pmsg.err_video_not_found", language, path=video_path))

    deps = deps or Deps()
    progress = on_progress or _noop
    warnings: list[str] = []

    out_dir = config.output_root / video_path.stem
    # Intermediate artifacts go into a subfolder per kind: frame images under
    # frames/, audio and transcript under transcript/, the minutes structure
    # and chunk summaries under work/ (gui.log is created under logs/ by the
    # GUI side).
    transcript_dir = out_dir / "transcript"
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    (out_dir / "work").mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    # Create the LLM server client once and use it throughout. Connection error hints also follow language.
    client = deps.make_client(config)
    with contextlib.suppress(AttributeError):
        client.language = language
    minutes_path: Path | None = None
    minutes_docx_path: Path | None = None
    frame_notes_path: Path | None = None
    try:
        # 0) Preflight check (verify the LLM server and models before heavy processing) --------
        check_cancel(cancel_event)
        progress(
            "preflight", 0, 1,
            t("pmsg.pre_wait", language,
              model=config.ai.llm_model, vlm=config.ai.vlm_model),
        )
        preflight = getattr(client, "preflight", None)
        if callable(preflight):
            preflight([config.ai.llm_model, config.ai.vlm_model])
        progress("preflight", 1, 1, t("pmsg.pre_ok", language))

        # 1) Audio extraction --------------------------------------------------------
        check_cancel(cancel_event)
        progress("audio", 0, 1, t("pmsg.audio_extracting", language))
        t0 = time.monotonic()
        wav_path = deps.extract_audio(video_path, transcript_dir / "audio.wav")
        audio_elapsed = time.monotonic() - t0
        duration = 0.0
        try:
            duration = float(deps.probe_duration(video_path))
        except Exception as exc:
            warnings.append(t("pmsg.warn_duration", language, exc=exc))
        progress(
            "audio", 1, 1,
            t("pmsg.audio_done", language,
              elapsed=format_elapsed(audio_elapsed, language)),
        )

        # 2) Transcription (reusable) --------------------------------------
        total_hint = int(duration / 4) if duration else 0  # assumes ~4 seconds per segment
        transcript_json = transcript_dir / "transcript.json"
        transcript_txt = transcript_dir / "transcript.txt"
        segments = None
        detected_language = ""
        if reuse and transcript_json.is_file():
            try:
                segments, detected_language = deps.load_transcript(transcript_dir)
                progress(
                    "transcribe", len(segments), len(segments),
                    t("pmsg.transcribe_reuse", language, n=len(segments)),
                )
            except Exception as exc:
                warnings.append(t("pmsg.warn_transcript_reuse", language, exc=exc))
                segments = None
                detected_language = ""
        if segments is None:
            check_cancel(cancel_event)

            def _tp(cur: int, tot: int, text: str) -> None:
                progress("transcribe", cur, tot, text.strip()[:60])

            backend = _transcribe.resolve_backend(config.transcribe)
            progress(
                "transcribe", 0, total_hint,
                t("pmsg.transcribe_start", language,
                  model=config.transcribe.model, backend=backend),
            )
            t0 = time.monotonic()
            segments, detected_language = deps.transcribe_wav(
                wav_path,
                config.transcribe,
                on_progress=_tp,
                total_hint=total_hint,
                cancel_event=cancel_event,
                language=language,
            )
            transcribe_elapsed = time.monotonic() - t0
            transcript_json, transcript_txt = deps.save_transcript(
                segments, transcript_dir, language=detected_language
            )
            progress(
                "transcribe", len(segments), len(segments),
                t("pmsg.transcribe_done", language, n=len(segments),
                  elapsed=format_elapsed(transcribe_elapsed, language)),
            )

        # The recording's own actual/detected transcription language, used to
        # drive frame-analysis (VLM) output language below — falls back to
        # config.transcribe.language (for pre-feature transcript.json files
        # with no persisted language) then "ja" if that's also empty.
        source_language = (
            detected_language or (config.transcribe.language or "").strip() or "ja"
        )
        # Only log this when config.transcribe.language was left empty AND we
        # actually have a real detection result (not the "ja" fallback used
        # when nothing is known — e.g. reusing a pre-feature transcript.json
        # with no persisted language).
        if not (config.transcribe.language or "").strip() and detected_language:
            progress(
                "transcribe", len(segments), len(segments),
                t("pmsg.language_detected", language, lang=detected_language),
            )

        # 3) Frame extraction (reusable) --------------------------------
        frames_index: Path = out_dir / "frames" / "frames.json"
        frames = None
        if reuse and frames_index.is_file():
            try:
                frames = deps.load_frames(out_dir) or None
                if frames:
                    progress(
                        "frames", len(frames), len(frames),
                        t("pmsg.frames_reuse", language, n=len(frames)),
                    )
            except Exception as exc:
                warnings.append(t("pmsg.warn_frames_reuse", language, exc=exc))
                frames = None
        if frames is None:
            check_cancel(cancel_event)
            progress("frames", 0, 1, t("pmsg.frames_extracting", language))
            t0 = time.monotonic()
            frames = deps.extract_frames(video_path, out_dir, config.frames)
            frames_elapsed = time.monotonic() - t0
            frames_index = deps.save_frame_index(frames, out_dir)
            progress(
                "frames", len(frames), len(frames),
                t("pmsg.frames_done", language, n=len(frames),
                  elapsed=format_elapsed(frames_elapsed, language)),
            )

        # 4) Frame analysis (VLM) + 5) Minutes generation ----------------------
        check_cancel(cancel_event)

        def _vp(cur: int, tot: int, msg: str) -> None:
            progress("vision", cur, tot, msg)

        progress(
            "vision", 0, len(frames),
            t("pmsg.vision_analyzing", language, vlm=config.ai.vlm_model),
        )
        t0 = time.monotonic()
        notes = deps.describe_frames(
            frames, client, out_dir,
            on_progress=_vp, cancel_event=cancel_event, reuse=reuse,
            language=language,
            source_language=source_language,
        )
        vision_elapsed = time.monotonic() - t0
        frame_notes_path = deps.save_frame_notes(notes, out_dir)
        progress(
            "vision", len(notes), len(notes),
            t("pmsg.vision_done", language,
              elapsed=format_elapsed(vision_elapsed, language)),
        )

        check_cancel(cancel_event)
        meta = _minutes.MinutesMeta(
            title=video_path.stem,
            duration_hint=_format_duration(duration),
        )

        def _mp(cur: int, tot: int, msg: str) -> None:
            progress("minutes", cur, tot, msg)

        # The loaded model's real context length (fetchable if it's LM
        # Studio). If it can't be fetched, generate_minutes falls back to the
        # character-count threshold.
        ctx_tokens: int | None = None
        _lcl = getattr(client, "loaded_context_length", None)
        if callable(_lcl):
            try:
                ctx_tokens = _lcl()
            except Exception:
                ctx_tokens = None

        # The start message is emitted right away by generate_minutes itself
        # via _mp (the short path says "generating minutes," the long path
        # says "partial summary 1/N...", etc.).
        markdown = deps.generate_minutes(
            segments,
            notes,
            client,
            config.ai,
            meta,
            on_progress=_mp,
            cancel_event=cancel_event,
            out_dir=out_dir,
            reuse=reuse,
            context_tokens=ctx_tokens,
            template_path=config.output.template_path or None,
            auto_structure=config.output.auto_structure,
            language=language,
            minutes_language=config.output.minutes_language,
        )
        minutes_path = deps.save_minutes(markdown, out_dir)
        # The completion message (with elapsed time) was already reported by
        # generate_minutes itself via _mp, so it's not repeated here.

        # Also write out a .docx (Word) version with the same content. Since
        # this is a secondary artifact, a conversion/write failure only warns
        # and continues (minutes.md is the primary artifact, so this doesn't
        # abort the run).
        try:
            minutes_docx_path = deps.save_minutes_docx(markdown, out_dir)
            progress(
                "minutes", 1, 1,
                t("pmsg.docx_saved", language, path=minutes_docx_path),
            )
        except Exception as exc:
            warnings.append(t("pmsg.warn_docx_failed", language, exc=exc))
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    progress("done", 1, 1, t("pmsg.done", language, path=minutes_path))
    return PipelineResult(
        video_path=video_path,
        out_dir=out_dir,
        minutes_path=minutes_path,
        transcript_txt=transcript_txt,
        transcript_json=transcript_json,
        frames_index=frames_index,
        frame_notes=frame_notes_path,
        n_segments=len(segments),
        n_frames=len(frames),
        warnings=warnings,
        minutes_docx_path=minutes_docx_path,
    )
