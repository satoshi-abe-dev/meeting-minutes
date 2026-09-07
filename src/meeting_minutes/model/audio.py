"""動画から音声トラックを抽出する。

faster-whisper が扱いやすいよう 16kHz / モノラル / PCM wav に変換する。
"""

from __future__ import annotations

from pathlib import Path

from . import ffmpeg_utils


def extract_audio(video_path: str | Path, out_wav: str | Path) -> Path:
    """video_path の音声を 16kHz モノラル wav として out_wav に書き出す。

    戻り値は書き出した wav の Path。
    """
    video_path = Path(video_path)
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    args = [
        ffmpeg_utils.ffmpeg_path(),
        "-y",  # 既存ファイルは上書き（out ディレクトリは output/ 配下のみ）
        "-i",
        str(video_path),
        "-vn",  # 映像を捨てる
        "-ac",
        "1",  # モノラル
        "-ar",
        "16000",  # 16kHz
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    ffmpeg_utils.run(args, desc="ffmpeg(音声抽出)")
    if not out_wav.is_file():  # pragma: no cover - 通常は run が失敗を送出
        raise ffmpeg_utils.FFmpegError("音声抽出に失敗しました（出力ファイルが作られていません）")
    return out_wav
