"""view — 画面（View）層。フォルダ ＝ この名前空間。

再エクスポートするのは抽象クラス ``MainView``（contract.py）だけ。Tkinter 実装
（``tk_main_window.TkMainWindow``）をここで import すると、Presenter のテストが
``meeting_minutes.view`` を読むだけで tkinter を巻き込んでしまうため、Tk 実装は
完全モジュールパス ``meeting_minutes.view.tk_main_window`` から import すること。
"""

from meeting_minutes.view.contract import MainView

__all__ = ["MainView"]
