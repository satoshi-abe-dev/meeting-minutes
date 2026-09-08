"""全工程のオーケストレーション。

    動画 → 音声抽出 → 文字起こし → フレーム抽出 → フレーム解析(VLM) → 議事録生成

各工程の依存を Deps 経由で差し替えられるようにしてあり、GUI / CLI / テストの
いずれからも同じ run() を呼ぶ。進捗は on_progress コールバックで通知する。
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
from . import ffmpeg_utils as _ffmpeg_utils
from . import frames as _frames
from . import minutes as _minutes
from . import transcribe as _transcribe
from . import vision as _vision
from .cancel import PipelineCancelled, check_cancel  # noqa: F401 - 呼び出し側の再export
from .config import Config
from .llm_client import LLMClient

# on_progress(stage, current, total, message)
#   stage: "preflight" | "audio" | "transcribe" | "frames" | "vision" | "minutes" | "done"
#   current/total: その工程内の進捗（total=0 は不定）
ProgressFn = Callable[[str, int, int, str], None]

STAGES = ("preflight", "audio", "transcribe", "frames", "vision", "minutes")


def _default_make_client(cfg: Config) -> LLMClient:
    return LLMClient(cfg.llm)


@dataclass
class Deps:
    """各工程の実装。テストではここを差し替える。

    dataclass の生成する __init__ が各値をインスタンス属性へ代入するため、
    関数を直接デフォルトに置いても記述子（bound method）化されない。
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
    """動画 1 本を処理して議事録を書き出す。

    reuse: True なら `output/<動画名>/` に前回の transcript/transcript.json /
        frames/frames.json があれば再利用し、文字起こし・フレーム抽出をやり直さない（VLM 段階などで
        失敗したあとの再実行を速くする）。False で常に最初から。
    cancel_event: セットされていれば PipelineCancelled を送出して中断する。
        各ステージの開始前・フレーム解析の1枚ごと・議事録のチャンクごとで反応する。
        mlx-whisper の呼び出し中と ffmpeg 実行中は反応できない（docs/DESIGN.md 参照）。
    language: on_progress へ渡す進捗メッセージ・エラーヒントの言語（"ja" / "en"）。
        既定 "ja"。GUI が --lang en のとき "en" を渡す。CLI は渡さない（＝ja）。
        文字起こし言語（config.transcribe.language）や議事録の中身は対象外。
    """
    language = normalize_language(language)
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(t("pmsg.err_video_not_found", language, path=video_path))

    deps = deps or Deps()
    progress = on_progress or _noop
    warnings: list[str] = []

    out_dir = config.output_root / video_path.stem
    # 中間生成物は種類ごとにサブフォルダへ。frames 画像は frames/、音声・文字起こしは
    # transcript/、議事録の型・チャンク要約は minutes/（gui.log は GUI 側で logs/ に作る）。
    transcript_dir = out_dir / "transcript"
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    (out_dir / "minutes").mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    # LLM サーバーを一度作り、以降ずっと使う。接続エラーヒントも language に従わせる。
    client = deps.make_client(config)
    with contextlib.suppress(AttributeError):
        client.language = language
    minutes_path: Path | None = None
    frame_notes_path: Path | None = None
    try:
        # 0) 起動前チェック（重い処理の前に LLM サーバーとモデルを確認） --------
        check_cancel(cancel_event)
        progress(
            "preflight", 0, 1,
            t("pmsg.pre_wait", language,
              model=config.llm.model, vlm=config.llm.vlm_model),
        )
        preflight = getattr(client, "preflight", None)
        if callable(preflight):
            preflight([config.llm.model, config.llm.vlm_model])
        progress("preflight", 1, 1, t("pmsg.pre_ok", language))

        # 1) 音声抽出 --------------------------------------------------------
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

        # 2) 文字起こし（再利用可）--------------------------------------
        total_hint = int(duration / 4) if duration else 0  # 1 区間 ≒ 4 秒と仮定
        transcript_json = transcript_dir / "transcript.json"
        transcript_txt = transcript_dir / "transcript.txt"
        segments = None
        if reuse and transcript_json.is_file():
            try:
                segments = deps.load_transcript(transcript_dir)
                progress(
                    "transcribe", len(segments), len(segments),
                    t("pmsg.transcribe_reuse", language, n=len(segments)),
                )
            except Exception as exc:
                warnings.append(t("pmsg.warn_transcript_reuse", language, exc=exc))
                segments = None
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
            segments = deps.transcribe_wav(
                wav_path,
                config.transcribe,
                on_progress=_tp,
                total_hint=total_hint,
                cancel_event=cancel_event,
                language=language,
            )
            transcribe_elapsed = time.monotonic() - t0
            transcript_json, transcript_txt = deps.save_transcript(segments, transcript_dir)
            progress(
                "transcribe", len(segments), len(segments),
                t("pmsg.transcribe_done", language, n=len(segments),
                  elapsed=format_elapsed(transcribe_elapsed, language)),
            )

        # 3) フレーム抽出（再利用可）--------------------------------
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

        # 4) フレーム解析（VLM）+ 5) 議事録生成 ----------------------
        check_cancel(cancel_event)

        def _vp(cur: int, tot: int, msg: str) -> None:
            progress("vision", cur, tot, msg)

        progress(
            "vision", 0, len(frames),
            t("pmsg.vision_analyzing", language, vlm=config.llm.vlm_model),
        )
        t0 = time.monotonic()
        notes = deps.describe_frames(
            frames, client, out_dir,
            on_progress=_vp, cancel_event=cancel_event, reuse=reuse,
            language=language,
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

        # ロード中モデルの実コンテキスト長（LM Studio なら取得可）。取れなければ
        # generate_minutes 側は文字数しきい値にフォールバックする。
        ctx_tokens: int | None = None
        _lcl = getattr(client, "loaded_context_length", None)
        if callable(_lcl):
            try:
                ctx_tokens = _lcl()
            except Exception:
                ctx_tokens = None

        # 開始メッセージは generate_minutes 自身が _mp 経由ですぐ出す
        # （短いパスは「議事録を生成中」、長いパスは「部分要約 1/N…」等）。
        markdown = deps.generate_minutes(
            segments,
            notes,
            client,
            config.llm,
            meta,
            on_progress=_mp,
            cancel_event=cancel_event,
            out_dir=out_dir,
            reuse=reuse,
            context_tokens=ctx_tokens,
            template_path=config.output.template_path or None,
            auto_structure=config.output.auto_structure,
            language=language,
        )
        minutes_path = deps.save_minutes(markdown, out_dir)
        # 完了メッセージ（所要時間つき）は generate_minutes 自身が _mp 経由で
        # 既に通知済みなので、ここで重ねて出さない。
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
    )
