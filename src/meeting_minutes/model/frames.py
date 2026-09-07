"""動画からフレーム画像を抜き出す。

方針:
    - シーンが切り替わったフレーム（スライド送り・画面共有の切替）を拾う
    - かつ、一定間隔でも 1 枚拾う（動きが少ない会議で取りこぼさないため）
ffmpeg の select フィルタでこの両方を 1 パスで選び、showinfo の出力から
各フレームの動画内タイムスタンプ（秒）を取り出す。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg_utils
from .config import FramesConfig

# showinfo が stderr に出す "pts_time:123.456" を拾う
_PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


@dataclass
class Frame:
    """抽出した 1 フレーム。"""

    timestamp: float  # 動画内の秒
    path: Path


def _hhmmss(seconds: float) -> str:
    seconds = max(0, round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}"


def _thin_by_gap(timestamps: list[float], min_gap: float) -> list[int]:
    """近すぎるフレームを間引き、残すインデックスを返す。"""
    kept: list[int] = []
    last_t = -1e9
    for i, t in enumerate(timestamps):
        if t - last_t >= min_gap:
            kept.append(i)
            last_t = t
    return kept


def _cap_count(indices: list[int], max_frames: int) -> list[int]:
    """max_frames を超えるなら等間隔で絞る。"""
    if max_frames <= 0 or len(indices) <= max_frames:
        return indices
    step = len(indices) / max_frames
    picked = [indices[int(i * step)] for i in range(max_frames)]
    # 重複除去（step が小さいと同じ index を拾いうる）
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
    """フレームを out_dir/frames/ に書き出し、Frame のリストを返す。"""
    video_path = Path(video_path)
    frames_dir = Path(out_dir) / "frames"
    raw_dir = frames_dir / "_raw"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)  # 前回の生成物（このツール自身の出力）を作り直す
    raw_dir.mkdir(parents=True, exist_ok=True)

    th = config.scene_threshold
    interval = config.interval_sec
    # 1フレーム目 / シーン変化 / 前回選択から interval 秒経過 のいずれかで選ぶ
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
        # 選ばれたフレームだけを可変レートで書き出す（ffmpeg 5.1+ の -vsync 後継）
        "-fps_mode",
        "vfr",
        "-q:v",
        "3",
        str(raw_dir / "raw_%05d.jpg"),
    ]
    proc = ffmpeg_utils.run(args, desc="ffmpeg(フレーム抽出)")

    # showinfo は stderr。出た順が raw_00001, raw_00002, ... に対応する。
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
    """フレーム一覧（時刻とパス）を JSON で保存する。

    索引はフレーム画像と同じ `out_dir/frames/` に置く（パスは out_dir 基準の相対のまま）。
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
    """save_frame_index が書いた frames/frames.json を読み戻す（再開用）。

    実ファイルが欠けているフレームは除外する。
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
