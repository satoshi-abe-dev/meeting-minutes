"""Checks for ffmpeg / ffprobe and wraps running them.

Never uses an external network at all. Just calls the local ffmpeg binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class FFmpegNotFound(RuntimeError):
    """Raised when ffmpeg / ffprobe isn't on PATH."""


class FFmpegError(RuntimeError):
    """Raised when ffmpeg / ffprobe returns a non-zero exit code."""


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
    """True if both ffmpeg and ffprobe are present. Used e.g. for deciding whether to skip a test."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str], *, desc: str = "ffmpeg") -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe, raising FFmpegError on failure.

    args: the full command list, including the executable name.
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - expected to be caught by ffmpeg_path instead
        raise FFmpegNotFound(str(exc)) from exc

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise FFmpegError(
            f"{desc} が失敗しました (exit {proc.returncode}):\n" + "\n".join(tail)
        )
    return proc


def probe_duration(video_path: str | Path) -> float:
    """Return the video's length (seconds). 0.0 if it can't be determined."""
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
