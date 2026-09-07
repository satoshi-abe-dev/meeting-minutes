#!/usr/bin/env python3
"""議事録生成AI（ローカル処理）の GUI エントリポイント。

    python gui.py

Model（``meeting_minutes.pipeline`` ほか）/ View（``meeting_minutes.view``）/
Presenter（``meeting_minutes.presenter``）を組み立てて起動するだけの薄いラッパー。
画面まわりは view/、画面ロジックは presenter/ にある。実処理は
``meeting_minutes.pipeline.run`` に委譲する。

構成:
    gui.py                                これ（Model・View・Presenter を組み立てて起動）
    src/meeting_minutes/
        view/
            contract.py       MainView（抽象クラス＝Presenter が依存する契約）
            tk_main_window.py  TkMainWindow（Tkinter 実装。ウィジェット構築のみ）
        presenter/
            main.py           MainPresenter（進捗計算・フォーマット3択の解決・
                              成功/失敗/中断の状態遷移などの画面ロジック一式）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from meeting_minutes.config import load_config  # noqa: E402
from meeting_minutes.pipeline import run as run_pipeline  # noqa: E402
from meeting_minutes.presenter.main import MainPresenter  # noqa: E402
from meeting_minutes.view.tk_main_window import TkMainWindow  # noqa: E402


def main() -> int:
    config = load_config(None)
    view = TkMainWindow()
    MainPresenter(view, config, run_pipeline=run_pipeline)
    view.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
