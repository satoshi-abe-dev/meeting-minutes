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

# Launching directly by file path, e.g. `python src/meeting_minutes/gui.py`,
# leaves __package__ unset, so absolute imports (meeting_minutes.*) fail.
# Add the src layout's package parent — src/ (two levels above this file) —
# to sys.path. Launched via `python -m meeting_minutes.gui`, __package__ is
# already set, so this does nothing.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Never let the app talk to the outside world while it's running. Force
# offline mode before the HuggingFace-family libraries (huggingface_hub /
# faster_whisper / mlx_whisper) get imported for the first time. This
# assumes the Whisper model was already fetched during setup
# (scripts/setup.sh -> meeting_minutes.download_transcribe_model). The fetch
# script is a separate process/entry point, so it's unaffected by this
# setting (fetching the model still works).
# Uses setdefault, so a value the user set explicitly (e.g. for an internal
# mirror) is respected.
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
    # Priority: --lang > config.toml/[gui] language and env var > default en
    # (load_config has already rounded an unsupported config.gui.language
    # value to en.)
    language = args.lang or config.gui.language

    view = TkMainWindow(language=language)
    MainPresenter(view, config, language=language, run_pipeline=run_pipeline)
    view.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
