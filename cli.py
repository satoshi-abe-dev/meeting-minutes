#!/usr/bin/env python3
"""コマンドラインから議事録生成パイプラインを実行する（動作確認・自動化用）。

    python cli.py 会議.mp4
    python cli.py 会議.mp4 --config config.toml

GUI を使わずに全工程を回して output/<動画名>/minutes.md を作る。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# src レイアウトなので import 前に src/ を通す
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from meeting_minutes.config import load_config  # noqa: E402
from meeting_minutes.pipeline import run  # noqa: E402

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
    parser.add_argument(
        "--template",
        default=None,
        help="議事録テンプレート（構造）のパス。省略時は config.toml の [output] template_path。"
        "見つからない/読めない場合は内蔵テンプレートにフォールバック",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    if args.template is not None:
        config.output.template_path = args.template

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
