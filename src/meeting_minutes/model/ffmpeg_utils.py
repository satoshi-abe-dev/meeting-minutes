"""ffmpeg / ffprobe の存在確認と実行ラッパ。

外部ネットワークは一切使わない。ローカルの ffmpeg バイナリを呼ぶだけ。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class FFmpegNotFound(RuntimeError):
    """ffmpeg / ffprobe が PATH に無いときに送出する。"""


class FFmpegError(RuntimeError):
    """ffmpeg / ffprobe がゼロ以外の終了コードを返したときに送出する。"""


def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise FFmpegNotFound(
            "ffmpeg が見つかりません。`brew install ffmpeg` でインストールしてください。"
        )
    return exe


def ffprobe_path() -> str:
    exe = shutil.which("ffprobe")
    if exe is None:
        raise FFmpegNotFound(
            "ffprobe が見つかりません（通常は ffmpeg に同梱）。"
            "`brew install ffmpeg` でインストールしてください。"
        )
    return exe


def has_ffmpeg() -> bool:
    """ffmpeg と ffprobe が両方あれば True。テストの skip 判定などに使う。"""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str], *, desc: str = "ffmpeg") -> subprocess.CompletedProcess:
    """ffmpeg/ffprobe を実行し、失敗時は FFmpegError を送出する。

    args: 実行ファイル名を含む完全なコマンド列。
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - ffmpeg_path 側で弾く想定
        raise FFmpegNotFound(str(exc)) from exc

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise FFmpegError(
            f"{desc} が失敗しました (exit {proc.returncode}):\n" + "\n".join(tail)
        )
    return proc


def probe_duration(video_path: str | Path) -> float:
    """動画の長さ（秒）を返す。取得できない場合は 0.0。"""
    args = [
        ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    proc = run(args, desc="ffprobe(duration)")
    try:
        data = json.loads(proc.stdout or "{}")
        return float(data.get("format", {}).get("duration", 0.0) or 0.0)
    except (ValueError, KeyError, TypeError):
        return 0.0
