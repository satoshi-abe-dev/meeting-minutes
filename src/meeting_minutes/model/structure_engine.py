"""Picks the structure-generation engine for Auto mode (developer option).

Kept apart from gui.py (which needs tkinter) so the wiring can be tested
without a display. The default engine is the single-call one inside
minutes.generate_minutes; nothing here runs unless another engine is asked for.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from . import minutes as _minutes
from . import pipeline as _pipeline


class StructureEngineUnavailable(RuntimeError):
    """Raised when a requested structure engine can't be used (CrewAI not installed)."""


STRUCTURE_ENGINES = ("single", "crewai")
DEFAULT_STRUCTURE_ENGINE = "single"


def make_run_pipeline(engine: str = DEFAULT_STRUCTURE_ENGINE) -> Callable:
    """Return the pipeline entry point (pipeline.run) wired to the given engine.

    "single" returns pipeline.run unchanged. "crewai" returns it with
    generate_minutes pre-bound to the CrewAI engine; raises
    structure_crewai.StructureEngineUnavailable (checked now, not mid-run) if
    CrewAI isn't installed.
    """
    if engine == "single":
        return _pipeline.run
    if engine == "crewai":
        # Imported only here: importing structure_crewai switches CrewAI's
        # telemetry off, and the default path should not touch the environment.
        from .structure_crewai import load_generator

        generator = load_generator()
        deps = _pipeline.Deps(
            generate_minutes=functools.partial(
                _minutes.generate_minutes, structure_generator=generator
            )
        )
        return functools.partial(_pipeline.run, deps=deps)
    raise ValueError(f"unknown structure engine: {engine!r} (choose from {STRUCTURE_ENGINES})")
