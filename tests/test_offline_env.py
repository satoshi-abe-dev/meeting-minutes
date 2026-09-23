"""Verifies that the entry points (cli.py / gui.py) force runtime offline mode
(HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE) before the HuggingFace-family
libraries are imported.

Confirms, in a separate process, that just importing the module (without
calling main()) sets the environment variables — i.e. that it's set at the
module level. download_transcribe_model is out of scope for this, so also
confirms it's not set via that route.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")

# gui.py imports tkinter at module level (via the view layer); stub it on
# the subprocess side so this runs on a CI runner without tkinter too — all
# we verify is "is the env var set before meeting_minutes gets imported."
_TK_STUB = (
    "import sys, types\n"
    "tk = types.ModuleType('tkinter')\n"
    "for _s in ('filedialog', 'messagebox', 'ttk'):\n"
    "    _m = types.ModuleType('tkinter.' + _s)\n"
    "    setattr(tk, _s, _m); sys.modules['tkinter.' + _s] = _m\n"
    "sys.modules['tkinter'] = tk\n"
)

_REPORT = (
    "import os, importlib\n"
    "importlib.import_module({mod!r})\n"
    "print(os.environ.get('HF_HUB_OFFLINE'), os.environ.get('TRANSFORMERS_OFFLINE'))\n"
)


def _import_and_report(
    module: str, *, extra_env: dict | None = None, stub_tk: bool = False
) -> str:
    env = dict(os.environ)
    # Expose the package to the subprocess (this repo uses the src layout and isn't installed).
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("HF_HUB_OFFLINE", None)
    env.pop("TRANSFORMERS_OFFLINE", None)
    if extra_env:
        env.update(extra_env)
    code = (_TK_STUB if stub_tk else "") + _REPORT.format(mod=module)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def test_cli_import_forces_offline():
    assert _import_and_report("meeting_minutes.cli") == "1 1"


def test_gui_import_forces_offline():
    assert _import_and_report("meeting_minutes.gui", stub_tk=True) == "1 1"


def test_download_transcribe_model_import_does_not_force_offline():
    # download_transcribe_model is the side that fetches the model; it doesn't set offline mode.
    assert (
        _import_and_report("meeting_minutes.download_transcribe_model")
        == "None None"
    )


def test_entrypoint_respects_explicit_offline_opt_out():
    # Uses setdefault, so if the user has explicitly set "0" it's respected
    # (e.g. the case of wanting to go online for an internal mirror).
    out = _import_and_report(
        "meeting_minutes.cli",
        extra_env={"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0"},
    )
    assert out == "0 0"
