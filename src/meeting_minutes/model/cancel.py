"""Shared component for cooperative cancellation.

Since a Python thread can't be force-stopped from the outside, the approach
is to call this `check_cancel()` at breakpoints in long-running processing,
and unwind via an exception if `threading.Event` is set. Since `pipeline.py`
imports `transcribe` / `frames` / `vision` / `minutes`, those modules
importing `pipeline` back would create a cycle, so this lives in its own
zero-dependency module instead.
"""

from __future__ import annotations

import threading


class PipelineCancelled(RuntimeError):
    """Raised when the user cancels."""


def check_cancel(event: threading.Event | None) -> None:
    """Raise PipelineCancelled if event is set."""
    if event is not None and event.is_set():
        raise PipelineCancelled("ユーザーによって中断されました")
