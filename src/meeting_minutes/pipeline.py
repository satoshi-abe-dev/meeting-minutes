"""全工程のオーケストレーション。

    動画 → 音声抽出 → 文字起こし → フレーム抽出 → フレーム解析(VLM) → 議事録生成

各工程の依存を Deps 経由で差し替えられるようにしてあり、GUI / CLI / テストの
いずれからも同じ run() を呼ぶ。進捗は on_progress コールバックで通知する。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

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
    frames_index: Optional[Path]
    frame_notes: Optional[Path]
    n_segments: int
    n_frames: int
    warnings: list[str] = field(default_factory=list)


def _noop(stage: str, current: int, total: int, message: str) -> None:  # noqa: ARG001
    pass


def _format_elapsed(seconds: float) -> str:
    """処理にかかった時間の表示用（`_format_duration` は動画長のおおよそ表示用で別物）。"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分"


def _format_duration(seconds: float) -> str:
    if not seconds:
        return "（不明）"
    seconds = int(round(seconds))
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
) -> PipelineResult:
    """動画 1 本を処理して議事録を書き出す。

    reuse: True なら `output/<動画名>/` に前回の transcript/transcript.json /
        frames/frames.json があれば再利用し、文字起こし・フレーム抽出をやり直さない（VLM 段階などで
        失敗したあとの再実行を速くする）。False で常に最初から。
    cancel_event: セットされていれば PipelineCancelled を送出して中断する。
        各ステージの開始前・フレーム解析の1枚ごと・議事録のチャンクごとで反応する。
        mlx-whisper の呼び出し中と ffmpeg 実行中は反応できない（doc/DESIGN.md 参照）。
    """
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"動画ファイルが見つかりません: {video_path}")

    deps = deps or Deps()
    progress = on_progress or _noop
    warnings: list[str] = []

    out_dir = config.output_root / video_path.stem
    # 音声ファイルと文字起こしは1つのフォルダーにまとめる。フレーム画像は frames/。
    transcript_dir = out_dir / "transcript"
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    # LLM サーバーを一度作り、以降ずっと使う。
    client = deps.make_client(config)
    minutes_path: Optional[Path] = None
    frame_notes_path: Optional[Path] = None
    try:
        # 0) 起動前チェック（重い処理の前に LLM サーバーとモデルを確認） --------
        check_cancel(cancel_event)
        progress(
            "preflight", 0, 1,
            f"LLM サーバーの応答を待っています…"
            f"（LLM: {config.llm.model} / VLM: {config.llm.vlm_model}）",
        )
        preflight = getattr(client, "preflight", None)
        if callable(preflight):
            preflight([config.llm.model, config.llm.vlm_model])
        progress("preflight", 1, 1, "LLM サーバー確認 OK")

        # 1) 音声抽出 --------------------------------------------------------
        check_cancel(cancel_event)
        progress("audio", 0, 1, "動画から音声を抽出中")
        t0 = time.monotonic()
        wav_path = deps.extract_audio(video_path, transcript_dir / "audio.wav")
        audio_elapsed = time.monotonic() - t0
        duration = 0.0
        try:
            duration = float(deps.probe_duration(video_path))
        except Exception as exc:  # noqa: BLE001 - 長さ取得の失敗は致命的でない
            warnings.append(f"動画長の取得に失敗: {exc}")
        progress("audio", 1, 1, f"音声抽出が完了（所要 {_format_elapsed(audio_elapsed)}）")

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
                    f"既存の文字起こしを再利用（{len(segments)} 区間）",
                )
            except Exception as exc:  # noqa: BLE001 - 壊れていたら作り直す
                warnings.append(f"transcript.json の再利用に失敗、作り直します: {exc}")
                segments = None
        if segments is None:
            check_cancel(cancel_event)

            def _tp(cur: int, tot: int, text: str) -> None:
                progress("transcribe", cur, tot, text.strip()[:60])

            backend = _transcribe.resolve_backend(config.transcribe)
            progress(
                "transcribe", 0, total_hint,
                f"文字起こしを開始（モデル: {config.transcribe.model} / backend={backend}）",
            )
            t0 = time.monotonic()
            segments = deps.transcribe_wav(
                wav_path,
                config.transcribe,
                on_progress=_tp,
                total_hint=total_hint,
                cancel_event=cancel_event,
            )
            transcribe_elapsed = time.monotonic() - t0
            transcript_json, transcript_txt = deps.save_transcript(segments, transcript_dir)
            progress(
                "transcribe", len(segments), len(segments),
                f"文字起こし完了（{len(segments)} 区間、所要 {_format_elapsed(transcribe_elapsed)}）",
            )

        # 3) フレーム抽出（再利用可）--------------------------------
        frames_index: Optional[Path] = out_dir / "frames" / "frames.json"
        frames = None
        if reuse and frames_index.is_file():
            try:
                frames = deps.load_frames(out_dir) or None
                if frames:
                    progress(
                        "frames", len(frames), len(frames),
                        f"既存のフレームを再利用（{len(frames)} 枚）",
                    )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"frames/frames.json の再利用に失敗、作り直します: {exc}")
                frames = None
        if frames is None:
            check_cancel(cancel_event)
            progress("frames", 0, 1, "フレームを抽出中")
            t0 = time.monotonic()
            frames = deps.extract_frames(video_path, out_dir, config.frames)
            frames_elapsed = time.monotonic() - t0
            frames_index = deps.save_frame_index(frames, out_dir)
            progress(
                "frames", len(frames), len(frames),
                f"フレーム抽出完了（{len(frames)} 枚、所要 {_format_elapsed(frames_elapsed)}）",
            )

        # 4) フレーム解析（VLM）+ 5) 議事録生成 ----------------------
        check_cancel(cancel_event)

        def _vp(cur: int, tot: int, msg: str) -> None:
            progress("vision", cur, tot, msg)

        progress(
            "vision", 0, len(frames),
            f"フレームを解析中（モデル: {config.llm.vlm_model}）",
        )
        t0 = time.monotonic()
        notes = deps.describe_frames(
            frames, client, out_dir,
            on_progress=_vp, cancel_event=cancel_event, reuse=reuse,
        )
        vision_elapsed = time.monotonic() - t0
        frame_notes_path = deps.save_frame_notes(notes, out_dir)
        progress(
            "vision", len(notes), len(notes),
            f"フレーム解析完了（所要 {_format_elapsed(vision_elapsed)}）",
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
            except Exception:  # noqa: BLE001 - 補助情報なので握りつぶす
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
        )
        minutes_path = deps.save_minutes(markdown, out_dir)
        # 完了メッセージ（所要時間つき）は generate_minutes 自身が _mp 経由で
        # 既に通知済みなので、ここで重ねて出さない。
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    progress("done", 1, 1, f"完了: {minutes_path}")
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
