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

# Launching directly by file path, e.g. `python src/meeting_minutes/cli.py`,
# leaves __package__ unset, so absolute imports fail. Add the src layout's
# package parent — src/ (two levels above this file) — to sys.path. Does
# nothing when launched with -m.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Never let the app talk to the outside world while it's running. Force
# offline mode before the HuggingFace-family libraries (huggingface_hub /
# faster_whisper / mlx_whisper) get imported for the first time. This
# assumes the Whisper model was already fetched during setup
# (scripts/setup.sh -> meeting_minutes.download_transcribe_model). The fetch
# script is a separate process/entry point, so it's unaffected by this
# setting (fetching the model still works).
# Uses setdefault, so a value the user set explicitly (e.g. for an internal
# mirror) is respected.
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

    def report(stage: str, current: int, total: int, message: str) -> None:
        nonlocal last_stage, last_t
        now = time.monotonic()
        # Only throttle genuine fine-grained, rapidly-repeating progress
        # within the same stage (0 < current < total) to once every 0.5
        # seconds, so as not to flood the log. A stage's first (current=0)
        # and last (current=total) message — including one-off milestone
        # announcements that share that same current/total, like the
        # detected/extraction/minutes-language notices — always print, even
        # if another message for the same stage was just printed.
        if (
            stage == last_stage
            and now - last_t < 0.5
            and stage != "done"
            and 0 < current < total
        ):
            return
        last_stage = stage
        last_t = now
        label = _STAGE_LABEL.get(stage, stage)
        if total:
            print(f"[{label}] {current}/{total}  {message}", flush=True)
        else:
            print(f"[{label}] {message}", flush=True)

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
