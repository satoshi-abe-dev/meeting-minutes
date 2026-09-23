#!/usr/bin/env python3
"""Run the minutes-generation pipeline from the command line (for smoke tests / automation).

    python src/meeting_minutes/cli.py meeting.mp4
    python src/meeting_minutes/cli.py meeting.mp4 --config config.toml
(Developers can also use `python -m meeting_minutes.cli meeting.mp4`.)

Runs every stage without the GUI to produce output/<video name>/minutes.md.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Launching by file path leaves __package__ unset, breaking absolute
# imports. Add src/ (package parent) to sys.path; no-op when launched via -m.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force offline mode before HF-family libraries (huggingface_hub /
# faster_whisper / mlx_whisper) first import — assumes the model was already
# fetched via scripts/setup.sh. The separate fetch script isn't affected.
# setdefault respects a value the user set explicitly (e.g. an internal mirror).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from meeting_minutes.model.config import load_config
from meeting_minutes.model.pipeline import run

_STAGE_LABEL = {
    "audio": "音声抽出",
    "transcribe": "文字起こし",
    "frames": "フレーム抽出",
    "vision": "フレーム解析",
    "minutes": "議事録生成",
    "done": "完了",
}


def _make_reporter():
    last_stage: str | None = None
    last_t = 0.0
    # Most recently suppressed call for the current stage, if any. A fast
    # synchronous stage (e.g. mlx's per-segment loop) can pack many same-
    # stage calls — including its own completion message — into one 0.5s
    # window; flushed on the next stage change so nothing is lost, only delayed.
    pending: tuple[str, int, int, str] | None = None

    def _emit(stage: str, current: int, total: int, message: str) -> None:
        nonlocal last_stage, last_t
        last_stage = stage
        last_t = time.monotonic()
        label = _STAGE_LABEL.get(stage, stage)
        if total:
            print(f"[{label}] {current}/{total}  {message}", flush=True)
        else:
            print(f"[{label}] {message}", flush=True)

    def report(stage: str, current: int, total: int, message: str) -> None:
        nonlocal pending
        if stage != last_stage and pending is not None:
            _emit(*pending)
            pending = None
        now = time.monotonic()
        # Only print fine-grained progress within the same stage once every
        # 0.5 seconds (to avoid flooding the log)
        if stage == last_stage and now - last_t < 0.5 and stage != "done":
            pending = (stage, current, total, message)
            return
        pending = None
        _emit(stage, current, total, message)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打ち合わせ動画から議事録(Markdown)を生成する")
    parser.add_argument("video", help="打ち合わせ動画のパス")
    parser.add_argument(
        "--config",
        default=None,
        help="設定 TOML のパス（省略時: config.toml → config.example.toml の順で探す）",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="既存の文字起こし・フレームを再利用せず最初からやり直す",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    started = time.monotonic()
    try:
        result = run(
            args.video, config, on_progress=_make_reporter(), reuse=not args.fresh
        )
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"失敗しました: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print()
    print(f"議事録: {result.minutes_path}")
    print(f"文字起こし: {result.transcript_txt}  ({result.n_segments} 区間)")
    print(f"フレーム: {result.n_frames} 枚  ({result.out_dir / 'frames'})")
    for w in result.warnings:
        print(f"警告: {w}")
    print(f"所要時間: {elapsed:.1f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
