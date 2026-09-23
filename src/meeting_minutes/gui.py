#!/usr/bin/env python3
"""GUI entry point for the Meeting Minutes AI (local processing).

Launch:
    python src/meeting_minutes/gui.py
    python src/meeting_minutes/gui.py --lang en   # display in English
(Developers can also use `python -m meeting_minutes.gui`; in that case, run
 `cd src` first or set `PYTHONPATH=src`. The __package__ bootstrap below
 makes either launch method work.)

A thin wrapper that just assembles and starts the Model
(``meeting_minutes.model.pipeline`` etc.) / View (``meeting_minutes.view``) /
Presenter (``meeting_minutes.presenter``). The screen lives in view/, its
logic in presenter/. Actual processing is delegated to
``meeting_minutes.model.pipeline.run``.
"""

from __future__ import annotations

import argparse
import os
import sys

# Launching by file path leaves __package__ unset, breaking absolute
# imports. Add src/ (package parent) to sys.path; no-op when launched via -m.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force offline mode before HF-family libraries (huggingface_hub /
# faster_whisper / mlx_whisper) first import — assumes the model was already
# fetched via scripts/setup.sh. The separate fetch script isn't affected.
# setdefault respects a value the user set explicitly (e.g. an internal mirror).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

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
        help="表示言語（省略時は config.toml の [gui] language、既定 en）",
    )
    args = parser.parse_args()

    config = load_config(None)
    # Priority: --lang > config.toml/[gui] language + env var > default en
    # (load_config already rounds an unsupported value to en).
    language = args.lang or config.gui.language

    view = TkMainWindow(language=language)
    MainPresenter(view, config, language=language, run_pipeline=run_pipeline)
    view.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
