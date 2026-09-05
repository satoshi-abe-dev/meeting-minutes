"""frames モジュールの純粋ロジック（間引き・上限・時刻整形）のテスト。

実際の ffmpeg 抽出は needs_ffmpeg マーカーを付け、ffmpeg が無ければ skip する。
"""

from __future__ import annotations

import pytest

from meeting_minutes import ffmpeg_utils
from meeting_minutes.config import FramesConfig
from meeting_minutes.frames import (
    Frame,
    _cap_count,
    _hhmmss,
    _thin_by_gap,
    extract_frames,
    load_frames,
    save_frame_index,
)


def test_hhmmss():
    assert _hhmmss(0) == "000000"
    assert _hhmmss(65) == "000105"
    assert _hhmmss(3661) == "010101"


def test_thin_by_gap_drops_close_frames():
    ts = [0.0, 1.0, 2.0, 10.0, 10.5, 20.0]
    kept = _thin_by_gap(ts, min_gap=4.0)
    assert kept == [0, 3, 5]  # 0s, 10s, 20s


def test_thin_by_gap_keeps_all_when_far_apart():
    ts = [0.0, 5.0, 10.0]
    assert _thin_by_gap(ts, min_gap=4.0) == [0, 1, 2]


def test_save_then_load_frames_roundtrip(tmp_path):
    fdir = tmp_path / "frames"
    fdir.mkdir()
    f1 = fdir / "frame_0001_000000.jpg"
    f2 = fdir / "frame_0002_000012.jpg"
    f1.write_bytes(b"x")
    f2.write_bytes(b"y")
    frames = [Frame(timestamp=0.0, path=f1), Frame(timestamp=12.0, path=f2)]
    save_frame_index(frames, tmp_path)

    loaded = load_frames(tmp_path)
    assert [f.timestamp for f in loaded] == [0.0, 12.0]
    assert [p.path.name for p in loaded] == [f1.name, f2.name]
    assert all(f.path.is_file() for f in loaded)


def test_load_frames_skips_missing_files(tmp_path):
    (tmp_path / "frames.json").write_text(
        '[{"timestamp": 1.0, "path": "frames/gone.jpg"}]', encoding="utf-8"
    )
    assert load_frames(tmp_path) == []


def test_cap_count_no_op_under_limit():
    assert _cap_count([0, 1, 2], max_frames=10) == [0, 1, 2]
    assert _cap_count([0, 1, 2], max_frames=0) == [0, 1, 2]


def test_cap_count_downsamples_evenly():
    indices = list(range(100))
    capped = _cap_count(indices, max_frames=10)
    assert len(capped) == 10
    assert capped[0] == 0
    assert capped == sorted(capped)
    assert capped[-1] < 100


@pytest.mark.needs_ffmpeg
@pytest.mark.skipif(not ffmpeg_utils.has_ffmpeg(), reason="ffmpeg が無い")
def test_probe_duration_on_generated_clip(tmp_path):
    # 2 秒のテスト用動画を ffmpeg 自身で生成して長さを測る
    clip = tmp_path / "clip.mp4"
    ffmpeg_utils.run(
        [
            ffmpeg_utils.ffmpeg_path(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            str(clip),
        ],
        desc="ffmpeg(test clip)",
    )
    dur = ffmpeg_utils.probe_duration(clip)
    assert 1.5 <= dur <= 2.5


@pytest.mark.needs_ffmpeg
@pytest.mark.skipif(not ffmpeg_utils.has_ffmpeg(), reason="ffmpeg が無い")
def test_extract_frames_real_clip(tmp_path):
    # 24 秒のクリップ。途中で赤い矩形を出してシーン変化を作る。
    clip = tmp_path / "clip.mp4"
    ffmpeg_utils.run(
        [
            ffmpeg_utils.ffmpeg_path(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=24:size=320x240:rate=15",
            "-vf",
            "drawbox=enable='between(t,8,16)':color=red@1:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        desc="ffmpeg(test clip)",
    )
    cfg = FramesConfig(interval_sec=8, scene_threshold=0.3, max_frames=20, min_gap_sec=3)
    frames = extract_frames(clip, tmp_path / "out", cfg)

    assert len(frames) >= 3
    # 時刻は昇順、各ファイルは実在、min_gap 以上離れている
    times = [f.timestamp for f in frames]
    assert times == sorted(times)
    assert all(f.path.is_file() for f in frames)
    assert all(b - a >= cfg.min_gap_sec - 0.5 for a, b in zip(times, times[1:]))
    # 生の中間ディレクトリは片付けられている
    assert not (tmp_path / "out" / "frames" / "_raw").exists()

    index = save_frame_index(frames, tmp_path / "out")
    assert index.is_file()
