#!/usr/bin/env python3
"""Developer tool: compare structure-generation engines on an existing output folder.

    python scripts/compare_structure_engines.py output/<meeting-folder>

Runs the single-call engine and the CrewAI engine (needs requirements-agent.txt
in a Python 3.10-3.13 environment) on the same material and saves both
structures, the wall-clock time and the LLM-call counts under output/_compare/.
See docs/crewai_experiment_en.md.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from meeting_minutes.model.config import load_config
from meeting_minutes.model.structure_compare import ENGINE_NAMES, run_comparison
from meeting_minutes.model.structure_engine import StructureEngineUnavailable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="an output folder containing transcript.json")
    parser.add_argument("--config", default=None, help="config TOML (default: config.toml)")
    parser.add_argument("--out-root", default=None, help="where results go (default: output/_compare)")
    parser.add_argument(
        "--engines", nargs="+", choices=ENGINE_NAMES, default=list(ENGINE_NAMES),
        help="engines to run (default: all)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        result_dir = run_comparison(
            args.source, config, out_root=args.out_root, engines=tuple(args.engines)
        )
    except (FileNotFoundError, StructureEngineUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Results: {result_dir / 'comparison.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
