#!/usr/bin/env python3
"""議事録生成AI（ローカル処理）の GUI エントリポイント。

起動:
    python src/meeting_minutes/gui.py
    python src/meeting_minutes/gui.py --lang en   # 画面を英語で
（開発者向けに `python -m meeting_minutes.gui` も可。その場合は `cd src` するか
 `PYTHONPATH=src` を設定する。下の __package__ ブートストラップでどちらも動く。）

Model（``meeting_minutes.model.pipeline`` ほか）/ View（``meeting_minutes.view``）/
Presenter（``meeting_minutes.presenter``）を組み立てて起動するだけの薄いラッパー。
画面まわりは view/、画面ロジックは presenter/ にある。実処理は
``meeting_minutes.model.pipeline.run`` に委譲する。
"""

from __future__ import annotations

import argparse
import os
import sys

# `python src/meeting_minutes/gui.py` のようにファイル指定で直接起動されると
# __package__ が未設定で、絶対 import（meeting_minutes.*）が通らない。src レイアウトの
# パッケージ親 = src/（このファイルの 2 つ上）を sys.path に足す。
# `python -m meeting_minutes.gui` で起動された場合は __package__ 設定済みなので何もしない。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_minutes.model.config import load_config
from meeting_minutes.model.pipeline import run as run_pipeline
from meeting_minutes.presenter.main import MainPresenter
from meeting_minutes.view.tk_main_window import TkMainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description="議事録生成AIのGUIを起動する")
    parser.add_argument(
        "--lang",
        choices=["ja", "en"],
        default=None,
        help="表示言語（省略時は config.toml の [gui] language、既定 ja）",
    )
    args = parser.parse_args()

    config = load_config(None)
    # 優先順位: --lang > config.toml/[gui] language・環境変数 > 既定 ja
    # （config.gui.language は load_config で対応外の値を ja に丸め済み）。
    language = args.lang or config.gui.language

    view = TkMainWindow(language=language)
    MainPresenter(view, config, language=language, run_pipeline=run_pipeline)
    view.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
