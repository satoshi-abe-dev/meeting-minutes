"""協調的キャンセル（cooperative cancellation）のための共通部品。

Python のスレッドは外部から強制停止できないため、長い処理の節目でこの
`check_cancel()` を呼び、`threading.Event` が立っていたら例外で巻き戻す方式にする。
`pipeline.py` が `transcribe` / `frames` / `vision` / `minutes` を import する構造上、
これらのモジュールが `pipeline` を逆 import すると循環するため、依存ゼロの
このモジュールに置く。
"""

from __future__ import annotations

import threading


class PipelineCancelled(RuntimeError):
    """ユーザーが中断した際に送出する。"""


def check_cancel(event: threading.Event | None) -> None:
    """event がセットされていれば PipelineCancelled を送出する。"""
    if event is not None and event.is_set():
        raise PipelineCancelled("ユーザーによって中断されました")
