"""view — the screen (View) layer. This folder = this namespace.

Only the abstract class ``MainView`` (contract.py) is re-exported here.
Importing the Tkinter implementation (``tk_main_window.TkMainWindow``) here
would drag tkinter into Presenter tests just by reading
``meeting_minutes.view``, so import the Tk implementation from its full
module path, ``meeting_minutes.view.tk_main_window``, instead.
"""

from meeting_minutes.view.contract import MainView

__all__ = ["MainView"]
