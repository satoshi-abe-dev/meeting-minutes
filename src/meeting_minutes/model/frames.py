"""Extract frame images from a video.

Policy:
    - Pick up frames where the scene changed (slide advance, screen-share switch)
    - Also pick up one frame at a fixed interval regardless (so a low-motion
      meeting doesn't get missed)
ffmpeg's select filter picks both of these in a single pass, and each
frame's timestamp within the video (in seconds) is pulled from showinfo's
output.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_utils
from .config import FramesConfig

# Picks up the "pts_time:123.456" that showinfo writes to stderr
_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


@dataclass
class Frame:
    """One extracted frame."""

    timestamp: float  # seconds within the video
    path: Path


def _hhmmss(seconds: float) -> str:
    seconds = max(0, round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}"


def _thin_by_gap(timestamps: list[float], min_gap: float) -> list[int]:
    """Thin out frames that are too close together and return the indices to keep."""
    kept: list[int] = []
    last_t = -1e9
    for i, t in enumerate(timestamps):
        if t - last_t >= min_gap:
            kept.append(i)
            last_t = t
    return kept


def _cap_count(indices: list[int], max_frames: int) -> list[int]:
    """If over max_frames, thin down at even intervals."""
    if max_frames <= 0 or len(indices) <= max_frames:
        return indices
    step = len(indices) / max_frames
    picked = [indices[int(i * step)] for i in range(max_frames)]
    # Deduplicate (a small step can pick up the same index more than once)
    seen: set[int] = set()
    result = []
    for idx in picked:
        if idx not in seen:
            seen.add(idx)
            result.append(idx)
    return result


def extract_frames(
    video_path: str | Path,
    out_dir: str | Path,
    config: FramesConfig,
) -> list[Frame]:
    """Write frames out to out_dir/frames/ and return a list of Frame."""
    video_path = Path(video_path)
    frames_dir = Path(out_dir) / "frames"
    raw_dir = frames_dir / "_raw"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)  # regenerate the previous output (this tool's own output)
    raw_dir.mkdir(parents=True, exist_ok=True)

    th = config.scene_threshold
    interval = config.interval_sec
    # Pick whichever comes first: the 1st frame / a scene change / interval
    # seconds elapsed since the last pick
    select_expr = (
        f"select='isnan(prev_selected_t)+gt(scene\\,{th})+gte(t-prev_selected_t\\,{interval})'"
    )
    args = [
        ffmpeg_utils.ffmpeg_path(),
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"{select_expr},showinfo",
        # Write out only the selected frames at a variable rate (the
        # successor to -vsync in ffmpeg 5.1+)
        "-fps_mode",
        "vfr",
        "-q:v",
        "3",
        str(raw_dir / "raw_%05d.jpg"),
    ]
    proc = ffmpeg_utils.run(args, desc="ffmpeg(フレーム抽出)")

    # showinfo goes to stderr; the order it appears in matches raw_00001, raw_00002, ...
    timestamps = [float(m) for m in _PTS_RE.findall(proc.stderr or "")]
    raw_files = sorted(raw_dir.glob("raw_*.jpg"))

    n = min(len(timestamps), len(raw_files))
    timestamps = timestamps[:n]
    raw_files = raw_files[:n]

    keep = _thin_by_gap(timestamps, config.min_gap_sec)
    keep = _cap_count(keep, config.max_frames)
    keep_set = set(keep)

    frames: list[Frame] = []
    for i, (ts, raw) in enumerate(zip(timestamps, raw_files, strict=True)):
        if i not in keep_set:
            continue
        seq = len(frames) + 1
        final = frames_dir / f"frame_{seq:04d}_{_hhmmss(ts)}.jpg"
        shutil.move(str(raw), str(final))
        frames.append(Frame(timestamp=ts, path=final))

    shutil.rmtree(raw_dir, ignore_errors=True)
    return frames


def save_frame_index(frames: list[Frame], out_dir: str | Path) -> Path:
    """Save the frame list (timestamp and path) as JSON.

    The index is placed in the same `out_dir/frames/` as the frame images
    (paths stay relative to out_dir).
    """
    out_dir = Path(out_dir)
    index_path = out_dir / "frames" / "frames.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"timestamp": f.timestamp, "path": str(f.path.relative_to(out_dir))}
        if f.path.is_relative_to(out_dir)
        else {"timestamp": f.timestamp, "path": str(f.path)}
        for f in frames
    ]
    index_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return index_path


def load_frames(out_dir: str | Path) -> list[Frame]:
    """Read back frames/frames.json as written by save_frame_index (for resuming).

    Excludes frames whose actual file is missing.
    """
    out_dir = Path(out_dir)
    data = json.loads((out_dir / "frames" / "frames.json").read_text(encoding="utf-8"))
    frames: list[Frame] = []
    for d in data:
        p = Path(d["path"])
        if not p.is_absolute():
            p = out_dir / p
        if p.is_file():
            frames.append(Frame(timestamp=float(d["timestamp"]), path=p))
    return frames
