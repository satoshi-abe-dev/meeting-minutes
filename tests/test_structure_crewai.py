"""Tests for the optional CrewAI structure engine (Issue #192).

CI has neither CrewAI nor a real model: a fake `crewai` module and a fake LLM
client stand in, so nothing touches the network. The real CrewAI is exercised
only by test_structure_crewai_real.py, which skips when it isn't installed.
"""

from __future__ import annotations

import functools
import os
import sys
import types
from types import SimpleNamespace

import pytest

from meeting_minutes.model import structure_crewai as sc
from meeting_minutes.model import structure_engine as se
from meeting_minutes.model.cancel import PipelineCancelled
from meeting_minutes.model.config import AIConfig
from meeting_minutes.model.minutes import MinutesMeta, generate_minutes
from meeting_minutes.model.structure_engine import StructureEngineUnavailable
from meeting_minutes.model.transcribe import Segment

GOOD = (
    "# 議事録: {title}\n\n- 日時: {datetime_hint}\n- 記録時間: {duration_hint}\n\n"
    "## 進捗\n（明示されたことだけ）\n\n## 決定事項\n（該当なしなら「（該当なし）」）\n"
)


class FakeLLM:
    """The local OpenAI-compatible client used by the pipeline (chat only)."""

    def __init__(self, config: AIConfig | None = None):
        self.config = config or AIConfig(
            base_url="http://localhost:9999/v1", api_key="local-test", llm_model="test-model"
        )
        self.calls: list[str] = []

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append(user)
        if "議事録の「型」" in user:
            return GOOD
        return "# 議事録\n本文\n"


def _install_fake_crewai(monkeypatch, *, output: str = GOOD, boom: Exception | None = None,
                         search_attempts: int = 0, usage: object | None = None):
    """Put a fake `crewai` (and crewai.tools) into sys.modules; returns a recorder."""
    rec = SimpleNamespace(llm_kwargs=None, agents=[], tasks=[], crew_kwargs=None, tool_outputs=[])

    class LLM:
        def __init__(self, **kw):
            rec.llm_kwargs = kw

    class Agent:
        def __init__(self, **kw):
            self.kw = kw
            self.tools = kw.get("tools", [])
            rec.agents.append(self)

    class Task:
        def __init__(self, **kw):
            self.kw = kw
            rec.tasks.append(self)

    class Process:
        sequential = "sequential"

    class Crew:
        def __init__(self, **kw):
            rec.crew_kwargs = kw

        def kickoff(self):
            if boom is not None:
                raise boom
            researcher = rec.agents[1]
            for i in range(search_attempts):  # a small model hammering the tool
                rec.tool_outputs.append(researcher.tools[0](f"キーワード{i}"))
            return SimpleNamespace(raw=output, token_usage=usage)

    crewai = types.ModuleType("crewai")
    crewai.LLM, crewai.Agent, crewai.Task = LLM, Agent, Task
    crewai.Process, crewai.Crew = Process, Crew
    tools = types.ModuleType("crewai.tools")
    tools.tool = lambda name: (lambda fn: fn)  # the decorated function is the tool
    crewai.tools = tools
    monkeypatch.setitem(sys.modules, "crewai", crewai)
    monkeypatch.setitem(sys.modules, "crewai.tools", tools)
    return rec


def _kwargs(client, **over):
    base = {
        "model": "test-model", "max_tokens": 1000, "minutes_system": "sys", "ctx": 0,
        "on_progress": None, "cancel_event": None,
    }
    base.update(over)
    return base


# --- the tool ---------------------------------------------------------------

def test_search_material_finds_lines_and_bounds_output():
    material = "\n".join(f"予算は{i}万円" for i in range(20)) + "\n担当は田中"
    out = sc.search_material(material, "予算")
    assert "20 件" in out
    assert out.count("\n- ") == 5  # at most 5 lines shown
    assert "ほか 15 行" in out
    assert "担当は田中" in sc.search_material(material, "担当")
    assert "ありません" in sc.search_material(material, "存在しない語")
    assert "空" in sc.search_material(material, "  ")


def test_tool_budget_stops_after_the_cap():
    budget = sc._ToolBudget("予算の話", limit=2)
    assert "予算" in budget.search("予算")
    assert "予算" in budget.search("予算")
    assert budget.search("予算") == sc._LIMIT_REACHED
    assert budget.calls == 2  # refused calls aren't counted as searches


# --- telemetry ----------------------------------------------------------------

def test_disable_telemetry_overrides_existing_values(monkeypatch):
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "false")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    sc.disable_telemetry()
    assert os.environ["CREWAI_DISABLE_TELEMETRY"] == "true"
    assert os.environ["OTEL_SDK_DISABLED"] == "true"
    assert os.environ["CREWAI_TRACING_ENABLED"] == "false"


def test_telemetry_is_off_by_the_time_crewai_is_imported(monkeypatch):
    seen = {}
    rec = _install_fake_crewai(monkeypatch)
    for var in ("CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    original = sys.modules["crewai"].Crew.kickoff

    def spying(self):
        seen.update({v: os.environ.get(v) for v in ("CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED")})
        return original(self)

    sys.modules["crewai"].Crew.kickoff = spying
    sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None))
    assert seen == {"CREWAI_DISABLE_TELEMETRY": "true", "OTEL_SDK_DISABLED": "true"}
    assert rec.crew_kwargs is not None


# --- the engine (fake CrewAI) ---------------------------------------------------

def test_engine_talks_only_to_the_configured_local_server(monkeypatch):
    rec = _install_fake_crewai(monkeypatch)
    client = FakeLLM()
    out = sc.CrewAIStructureEngine()(client, "資料", **_kwargs(client, max_tokens=777))
    assert out == GOOD.strip()
    kw = rec.llm_kwargs
    assert kw["model"] == "openai/test-model"
    assert kw["base_url"] == "http://localhost:9999/v1"
    assert kw["api_key"] == "local-test"
    assert kw["max_tokens"] == 777
    assert "api.openai.com" not in repr(kw)
    assert client.calls == []  # the agents use CrewAI's LLM, not the pipeline's client


def test_engine_builds_three_agents_with_hard_caps(monkeypatch):
    rec = _install_fake_crewai(monkeypatch)
    sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None))
    assert len(rec.agents) == 3
    iters = [a.kw["max_iter"] for a in rec.agents]
    assert iters == [2, 4, 2]
    assert all(a.kw["allow_delegation"] is False for a in rec.agents)
    assert all(a.kw["max_execution_time"] <= sc._MAX_EXECUTION_SECONDS for a in rec.agents)
    assert [len(a.tools) for a in rec.agents] == [0, 1, 0]  # only the Researcher has a tool
    assert rec.crew_kwargs["process"] == "sequential"
    # the Designer sees both earlier results
    assert rec.tasks[2].kw["context"] == [rec.tasks[0], rec.tasks[1]]
    # and gets the same rules as the single-call engine
    assert "{title}" in rec.tasks[2].kw["description"]


def test_tool_cap_holds_even_when_the_model_hammers_the_tool(monkeypatch):
    rec = _install_fake_crewai(monkeypatch, search_attempts=50)
    engine = sc.CrewAIStructureEngine()
    engine(FakeLLM(), "キーワード0 を含む行", **_kwargs(None))
    assert engine.stats.tool_calls == sc._MAX_TOOL_CALLS
    assert rec.tool_outputs.count(sc._LIMIT_REACHED) == 50 - sc._MAX_TOOL_CALLS


def test_stats_report_llm_calls_from_usage_metrics(monkeypatch):
    _install_fake_crewai(monkeypatch, usage=SimpleNamespace(successful_requests=7, total_tokens=1234))
    engine = sc.CrewAIStructureEngine()
    engine(FakeLLM(), "資料", **_kwargs(None))
    assert engine.stats.llm_calls == 7
    assert engine.stats.total_tokens == 1234


def test_code_fenced_answer_is_unwrapped(monkeypatch):
    _install_fake_crewai(monkeypatch, output="```markdown\n" + GOOD.strip() + "\n```")
    assert sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None)) == GOOD.strip()


def test_output_goes_through_the_same_checks(monkeypatch):
    msgs: list[str] = []
    _install_fake_crewai(monkeypatch, output="# 議事録: {title}\n## 議論\n（略）\n")  # placeholders missing
    out = sc.CrewAIStructureEngine()(
        FakeLLM(), "資料", **_kwargs(None, on_progress=lambda c, t, m: msgs.append(m))
    )
    assert out is None
    assert any("missing placeholders" in m for m in msgs)


def test_oversized_structure_is_rejected(monkeypatch):
    _install_fake_crewai(monkeypatch, output=GOOD + "あ" * 50_000)
    out = sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None, ctx=4000))
    assert out is None


def test_crew_failure_returns_none_with_a_warning(monkeypatch):
    msgs: list[str] = []
    _install_fake_crewai(monkeypatch, boom=RuntimeError("agent loop exploded"))
    out = sc.CrewAIStructureEngine()(
        FakeLLM(), "資料", **_kwargs(None, on_progress=lambda c, t, m: msgs.append(m))
    )
    assert out is None
    assert any("agent loop exploded" in m for m in msgs)


def test_cancel_before_start_propagates(monkeypatch):
    import threading

    _install_fake_crewai(monkeypatch)
    ev = threading.Event()
    ev.set()
    with pytest.raises(PipelineCancelled):
        sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None, cancel_event=ev))


def test_missing_crewai_gives_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "crewai", None)  # makes `import crewai` raise ImportError
    with pytest.raises(StructureEngineUnavailable) as err:
        sc.load_generator()
    assert "requirements-agent.txt" in str(err.value)


# --- wired into generate_minutes ----------------------------------------------

def _segs(n=5):
    return [Segment(start=i * 3.0, end=i * 3.0 + 3.0, text=f"進捗の発言{i}") for i in range(n)]


def test_generate_minutes_uses_the_crewai_structure(monkeypatch, tmp_path):
    _install_fake_crewai(monkeypatch)
    client = FakeLLM()
    generate_minutes(
        _segs(), [], client, client.config, MinutesMeta(title="定例"),
        out_dir=tmp_path, auto_structure=True, structure_generator=sc.CrewAIStructureEngine(),
    )
    assert len(client.calls) == 1  # only the minutes body; the structure came from the crew
    assert "## 進捗" in client.calls[0]
    assert (tmp_path / "work" / "structure_used.txt").is_file()


def test_generate_minutes_falls_back_when_the_crew_fails(monkeypatch, tmp_path):
    _install_fake_crewai(monkeypatch, boom=RuntimeError("nope"))
    client = FakeLLM()
    generate_minutes(
        _segs(), [], client, client.config, MinutesMeta(title="定例"),
        out_dir=tmp_path, auto_structure=True, structure_generator=sc.CrewAIStructureEngine(),
    )
    assert "## 宿題・アクションアイテム" in client.calls[0]  # the built-in structure
    assert not (tmp_path / "work" / "structure_used.txt").exists()


# --- engine selection ---------------------------------------------------------

def test_default_engine_is_the_unchanged_pipeline_run():
    from meeting_minutes.model import pipeline

    assert se.make_run_pipeline() is pipeline.run
    assert se.make_run_pipeline("single") is pipeline.run
    assert se.DEFAULT_STRUCTURE_ENGINE == "single"


def test_crewai_engine_binds_the_generator_into_deps(monkeypatch):
    _install_fake_crewai(monkeypatch)
    run = se.make_run_pipeline("crewai")
    assert isinstance(run, functools.partial)
    gen = run.keywords["deps"].generate_minutes
    assert isinstance(gen.keywords["structure_generator"], sc.CrewAIStructureEngine)


def test_crewai_engine_without_the_extra_fails_up_front(monkeypatch):
    monkeypatch.setitem(sys.modules, "crewai", None)
    with pytest.raises(StructureEngineUnavailable):
        se.make_run_pipeline("crewai")


def test_unknown_engine_is_rejected():
    with pytest.raises(ValueError):
        se.make_run_pipeline("langchain")
