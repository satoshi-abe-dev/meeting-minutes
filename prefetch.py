#!/usr/bin/env python3
"""文字起こしモデルを事前ダウンロードするランチャー。

    python prefetch.py
    python prefetch.py --config config.toml

実体は meeting_minutes.prefetch。scripts/setup.sh から呼ばれる。
"""

from __future__ import annotations

import sys
from pathlib import Path

# src レイアウトなので import 前に src/ を通す
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from meeting_minutes.prefetch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
