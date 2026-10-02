"""Developer comparison of the structure-generation engines (Issue #192).

Takes an existing output folder (the one holding transcript.json), runs the
single-call engine and the CrewAI engine on exactly the same material, and
saves both structures side by side with wall-clock time and the number of LLM
calls. Results go under output/ (git-ignored), never into the repo.

"The same material" is guaranteed by construction: the run goes through
generate_minutes itself, so the material is built by the existing logic (the
full transcript when it fits the structure budget, otherwise the existing chunk
summaries). The engines are hooked in as the structure generator, receive that
one material string, and the run is stopped right after, before any minutes
are generated.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from . import minutes as _minutes
from .config import Config
from .llm_client import LLMClient
from .structure_engine import StructureEngineUnavailable
from .transcribe import load_transcript

ENGINE_NAMES = ("single", "crewai")


@dataclass
class EngineResult:
    engine: str
    structure: str | None  # None = failed a check / errored (the pipeline would fall back)
    seconds: float
    llm_calls: int | None  # None = not reported
    tool_calls: int = 0
    notes: list[str] = field(default_factory=list)  # progress/warning messages


class _Captured(Exception):
    """Raised from the capturing generator to stop generate_minutes after the structure step."""


class _CountingClient:
    """Wraps an LLM client and counts chat() calls."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.calls = 0

    def chat(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        return self._inner.chat(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _single_engine(client: Any, material: str, **kw: Any) -> tuple[str | None, int | None, int]:
    counting = _CountingClient(client)
    out = _minutes._generate_structure(cast(LLMClient, counting), material, **kw)
    return out, counting.calls, 0


def _crewai_engine(client: Any, material: str, **kw: Any) -> tuple[str | None, int | None, int]:
    from .structure_crewai import CrewAIStructureEngine, ensure_available

    ensure_available()  # a missing CrewAI is an error here, not a failed engine
    engine = CrewAIStructureEngine()
    out = engine(client, material, **kw)
    return out, engine.stats.llm_calls, engine.stats.tool_calls


# name -> runner(client, material, **kwargs) -> (structure, llm_calls, tool_calls)
Runner = Callable[..., tuple["str | None", "int | None", int]]
DEFAULT_RUNNERS: dict[str, Runner] = {"single": _single_engine, "crewai": _crewai_engine}


def _find_transcript_dir(source: Path) -> Path:
    for cand in (source, source / "transcript"):
        if (cand / "transcript.json").is_file():
            return cand
    raise FileNotFoundError(f"transcript.json not found in {source} (or {source / 'transcript'})")


def run_comparison(
    source: str | Path,
    config: Config,
    *,
    out_root: str | Path | None = None,
    engines: tuple[str, ...] = ENGINE_NAMES,
    runners: dict[str, Runner] | None = None,
    client: Any | None = None,
    language: str = "en",
) -> Path:
    """Compare the engines on the transcript in source. Returns the result folder."""
    source = Path(source).expanduser().resolve()
    segments, _detected = load_transcript(_find_transcript_dir(source))
    runners = runners or DEFAULT_RUNNERS
    unknown = [e for e in engines if e not in runners]
    if unknown:
        raise ValueError(f"unknown engine(s): {unknown}")

    root = Path(out_root) if out_root is not None else config.output_root / "_compare"
    result_dir = root / f"{source.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    (result_dir / "work").mkdir(parents=True, exist_ok=True)
    # Reuse chunk summaries from the original run when they match (saves a long
    # LLM pass); generate_minutes re-validates them and ignores stale ones.
    partials = source / "work" / "minutes_partials.json"
    if partials.is_file():
        shutil.copy2(partials, result_dir / "work" / "minutes_partials.json")

    own_client = client is None
    client = client or LLMClient(config.ai, language=language)
    results: list[EngineResult] = []
    captured: dict[str, Any] = {}

    def capture(cl: Any, material: str, **kw: Any) -> str | None:
        captured["material"] = material
        for name in engines:
            notes: list[str] = []
            kw_run = dict(kw)
            kw_run["on_progress"] = lambda c, t, m, _n=notes: _n.append(m)
            t0 = time.monotonic()
            try:
                structure, llm_calls, tool_calls = runners[name](cl, material, **kw_run)
            except StructureEngineUnavailable:
                raise
            except Exception as exc:
                structure, llm_calls, tool_calls = None, None, 0
                notes.append(f"error: {str(exc)[:300]}")
            results.append(
                EngineResult(name, structure, time.monotonic() - t0, llm_calls, tool_calls, notes)
            )
        raise _Captured

    ctx_tokens: int | None = None
    lcl = getattr(client, "loaded_context_length", None)
    if callable(lcl):
        try:
            ctx_tokens = lcl()
        except Exception:
            ctx_tokens = None

    try:
        _minutes.generate_minutes(
            segments, [], client, config.ai,
            _minutes.MinutesMeta(title=source.name),
            out_dir=result_dir, auto_structure=True, context_tokens=ctx_tokens,
            structure_generator=capture, language=language,
            minutes_language=config.output.minutes_language,
        )
    except _Captured:
        pass
    finally:
        if own_client:
            client.close()
    if "material" not in captured:
        raise RuntimeError("the structure step was never reached")

    _write_results(result_dir, source, captured["material"], results)
    return result_dir


def _write_results(
    result_dir: Path, source: Path, material: str, results: list[EngineResult]
) -> None:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    (result_dir / "material.txt").write_text(material, encoding="utf-8")
    for r in results:
        if r.structure is not None:
            (result_dir / f"structure_{r.engine}.txt").write_text(r.structure + "\n", encoding="utf-8")
    (result_dir / "comparison.json").write_text(
        json.dumps(
            {"source": source.name, "material_sha256_12": digest,
             "material_chars": len(material), "results": [asdict(r) for r in results]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        f"# Structure engine comparison: {source.name}",
        "",
        f"Both engines received the same material ({len(material)} chars, sha256 {digest}).",
        "Compare the structures by reading them; there is no automatic score.",
        "",
        "| engine | result | seconds | LLM calls | tool calls |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        calls = "n/a" if r.llm_calls is None else str(r.llm_calls)
        status = "ok" if r.structure is not None else "failed (would fall back)"
        lines.append(f"| {r.engine} | {status} | {r.seconds:.1f} | {calls} | {r.tool_calls} |")
    for r in results:
        lines += ["", f"## {r.engine}", ""]
        lines += [f"- {n}" for n in r.notes] or ["- (no messages)"]
        if r.structure is not None:
            lines += ["", "```markdown", r.structure, "```"]
    (result_dir / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
