"""Extract the audio track from a video.

Converts to 16kHz / mono / PCM wav so faster-whisper can handle it easily.
"""

from __future__ import annotations

from pathlib import Path

from . import ffmpeg_utils


def extract_audio(video_path: str | Path, out_wav: str | Path) -> Path:
    """Write video_path's audio out to out_wav as a 16kHz mono wav.

    Returns the Path of the wav that was written.
    """
    video_path = Path(video_path)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    args = [
        ffmpeg_utils.ffmpeg_path(),
        "-y",  # overwrite an existing file (the out directory is always under output/)
        "-i",
        str(video_path),
        "-vn",  # discard the video
        "-ac",
        "1",  # mono
        "-ar",
        "16000",  # 16kHz
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    ffmpeg_utils.run(args, desc="ffmpeg(音声抽出)")
    if not out_wav.is_file():  # pragma: no cover - run normally raises on failure
        raise ffmpeg_utils.FFmpegError("音声抽出に失敗しました（出力ファイルが作られていません）")
    return out_wav
