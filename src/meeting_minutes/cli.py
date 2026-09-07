#!/usr/bin/env python3
"""コマンドラインから議事録生成パイプラインを実行する（動作確認・自動化用）。

    python src/meeting_minutes/cli.py 会議.mp4
    python src/meeting_minutes/cli.py 会議.mp4 --config config.toml
（開発者向けに `python -m meeting_minutes.cli 会議.mp4` も可）

GUI を使わずに全工程を回して output/<動画名>/minutes.md を作る。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# `python src/meeting_minutes/cli.py` のようにファイル指定で直接起動されると
# __package__ が未設定で絶対 import が通らない。src レイアウトのパッケージ親 = src/
# （このファイルの 2 つ上）を sys.path に足す。-m で起動された場合は何もしない。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_minutes.model.config import load_config  # noqa: E402
from meeting_minutes.model.pipeline import run  # noqa: E402

_STAGE_LABEL = {
    "audio": "音声抽出",
    "transcribe": "文字起こし",
    "frames": "フレーム抽出",
    "vision": "フレーム解析",
    "minutes": "議事録生成",
    "done": "完了",
}


def _make_reporter():
    last = {"stage": None, "t": 0.0}

    def report(stage: str, current: int, total: int, message: str) -> None:
        now = time.monotonic()
        # 同じ工程の細かい進捗は 0.5 秒に 1 回だけ出す（ログを溢れさせない）
        if stage == last["stage"] and now - last["t"] < 0.5 and stage != "done":
            return
        last["stage"] = stage
        last["t"] = now
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
    except Exception as exc:  # noqa: BLE001 - CLI なのでスタックより読めるメッセージを優先
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
