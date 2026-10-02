"""Tests for the optional CrewAI structure engine (Issue #192).

CI has neither CrewAI nor a real model: a fake `crewai` module and a fake LLM
client stand in, so nothing touches the network. The real CrewAI is exercised
only by test_structure_crewai_real.py, which skips when it isn't installed.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.machinery
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


@pytest.fixture(autouse=True)
def _isolate_storage_state(monkeypatch, tmp_path):
    """Each test starts with an unredirected, unverified engine, and its temp
    folders (and the atexit hook) stay inside the test."""
    monkeypatch.setattr(sc, "_redirected", False)
    monkeypatch.setattr(sc, "_verified", False)
    monkeypatch.setattr(sc, "_storage_dirs", [])
    monkeypatch.setattr(sc.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(sc.atexit, "register", lambda *a, **k: None)


def _fake_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, None)  # importlib.util.find_spec needs it
    return mod


def _install_fake_crewai(monkeypatch, *, output: str = GOOD, boom: Exception | None = None,
                         search_attempts: int = 0, usage: object | None = None):
    """Put a fake `crewai` (and crewai.tools) into sys.modules; returns a recorder."""
    rec = SimpleNamespace(llm_kwargs=None, llm_all=[], agents=[], tasks=[], crew_kwargs=None, tool_outputs=[])

    class LLM:
        def __init__(self, **kw):
            rec.llm_kwargs = kw
            rec.llm_all.append(kw)

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

    crewai = _fake_module("crewai")
    crewai.LLM, crewai.Agent, crewai.Task = LLM, Agent, Task
    crewai.Process, crewai.Crew = Process, Crew
    tools = _fake_module("crewai.tools")
    tools.tool = lambda name: (lambda fn: fn)  # the decorated function is the tool
    crewai.tools = tools
    monkeypatch.setitem(sys.modules, "crewai", crewai)
    monkeypatch.setitem(sys.modules, "crewai.tools", tools)

    # what the engine relies on in the real CrewAI: crewai_core.paths.db_storage_path,
    # read at call time by the storage class a Crew uses for its task outputs
    paths = _fake_module("crewai_core.paths")
    paths.db_storage_path = lambda: "/should/not/be/used"
    core = _fake_module("crewai_core")
    core.paths = paths
    monkeypatch.setitem(sys.modules, "crewai_core", core)
    monkeypatch.setitem(sys.modules, "crewai_core.paths", paths)

    class KickoffTaskOutputsSQLiteStorage:
        def __init__(self):
            self.db_path = os.path.join(sys.modules["crewai_core.paths"].db_storage_path(), "latest.db")

    storage_mod = _fake_module("crewai.memory.storage.kickoff_task_outputs_storage")
    storage_mod.KickoffTaskOutputsSQLiteStorage = KickoffTaskOutputsSQLiteStorage
    rec.storage_cls = KickoffTaskOutputsSQLiteStorage
    for name in ("crewai.memory", "crewai.memory.storage"):
        monkeypatch.setitem(sys.modules, name, _fake_module(name))
    monkeypatch.setitem(sys.modules, "crewai.memory.storage.kickoff_task_outputs_storage", storage_mod)
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
    assert os.environ["CREWAI_DISABLE_TRACKING"] == "true"
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
    # one LLM per agent (a shared one makes CrewAI's usage metrics count 3x)
    assert len(rec.llm_all) == 3
    assert all(k == kw for k in rec.llm_all)


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


# --- CrewAI's local data directory ------------------------------------------------

@pytest.fixture
def fake_paths(monkeypatch):
    """Just the fake crewai_core.paths (no fake crewai), for the storage tests."""
    paths = _fake_module("crewai_core.paths")
    paths.db_storage_path = lambda: "/should/not/be/used"
    core = _fake_module("crewai_core")
    core.paths = paths
    monkeypatch.setitem(sys.modules, "crewai_core", core)
    monkeypatch.setitem(sys.modules, "crewai_core.paths", paths)
    return paths


def _crewai_storage_dir():
    """The folder CrewAI would now use for its data (a fresh one per call)."""
    return sc.Path(sys.modules["crewai_core.paths"].db_storage_path())


def _our_dirs(tmp_path):
    return sorted(p.name for p in tmp_path.glob(f"{sc._STORAGE_PREFIX}*"))


def test_storage_is_redirected_to_private_temp_dirs(fake_paths, tmp_path):
    import os
    import stat

    sc._redirect_storage()
    path = fake_paths.db_storage_path()
    assert os.path.isdir(path)
    assert os.path.dirname(path) == str(tmp_path)  # never under ~/Library
    assert os.path.basename(path).startswith(f"{sc._STORAGE_PREFIX}{os.getpid()}-")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700  # private to the user
    sc._redirect_storage()  # idempotent: no second hook
    assert fake_paths.db_storage_path() != path  # each call gets its own folder
    sc.cleanup_storage()
    assert _our_dirs(tmp_path) == []


def test_storage_is_deleted_as_soon_as_the_run_ends(monkeypatch, tmp_path):
    rec = _install_fake_crewai(monkeypatch)
    sc._redirect_storage()
    original = sys.modules["crewai"].Crew.kickoff

    def kickoff_with_storage(self):
        (_crewai_storage_dir() / "latest_kickoff_task_outputs.db").write_text("material")
        assert _our_dirs(tmp_path)  # CrewAI wrote something during the run
        return original(self)

    sys.modules["crewai"].Crew.kickoff = kickoff_with_storage
    assert sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None)) == GOOD.strip()
    assert _our_dirs(tmp_path) == []  # gone right after the run, not at exit
    assert rec.crew_kwargs is not None


@pytest.mark.parametrize("boom", [RuntimeError("kickoff failed"), KeyboardInterrupt()])
def test_storage_is_deleted_even_when_the_run_fails(monkeypatch, tmp_path, boom):
    _install_fake_crewai(monkeypatch, boom=boom)
    sc._redirect_storage()
    original = sys.modules["crewai"].Crew.kickoff

    def kickoff_with_storage(self):
        (_crewai_storage_dir() / "x.db").write_text("material")
        return original(self)

    sys.modules["crewai"].Crew.kickoff = kickoff_with_storage
    with contextlib.suppress(KeyboardInterrupt):
        sc.CrewAIStructureEngine()(FakeLLM(), "資料", **_kwargs(None))
    assert _our_dirs(tmp_path) == []


def test_leftovers_of_dead_processes_are_removed_at_start(fake_paths, tmp_path):
    import os

    dead = tmp_path / f"{sc._STORAGE_PREFIX}999999999-abc"  # no such pid
    mine = tmp_path / f"{sc._STORAGE_PREFIX}{os.getpid()}-abc"  # a live process (this one)
    other = tmp_path / "unrelated-folder"
    for d in (dead, mine, other):
        d.mkdir()
        (d / "x.db").write_text("data")
    sc._redirect_storage()
    assert not dead.exists()
    assert mine.exists() and other.exists()  # live processes and unrelated folders untouched


# --- fail closed: never run CrewAI with its data directory unredirected ---------------

def test_refuses_when_crewai_core_paths_cannot_be_imported(monkeypatch):
    rec = _install_fake_crewai(monkeypatch)
    monkeypatch.setitem(sys.modules, "crewai_core.paths", None)  # `import crewai_core.paths` fails
    with pytest.raises(StructureEngineUnavailable) as err:
        sc.ensure_available()
    assert "redirected" in str(err.value) and "refuses to run" in str(err.value)
    assert sc._redirected is False
    # and the engine itself never builds or runs a crew
    msgs: list[str] = []
    out = sc.CrewAIStructureEngine()(
        FakeLLM(), "資料", **_kwargs(None, on_progress=lambda c, t, m: msgs.append(m))
    )
    assert out is None and rec.crew_kwargs is None
    assert any("refuses to run" in m for m in msgs)


def test_refuses_when_the_redirect_hook_is_missing(monkeypatch):
    _install_fake_crewai(monkeypatch)
    del sys.modules["crewai_core.paths"].db_storage_path
    with pytest.raises(StructureEngineUnavailable, match="db_storage_path is missing"):
        sc.ensure_available()


def test_refuses_when_the_database_would_still_go_elsewhere(monkeypatch):
    rec = _install_fake_crewai(monkeypatch)

    class Elsewhere:  # a CrewAI version that no longer asks db_storage_path()
        db_path = "/Users/someone/Library/Application Support/x/latest.db"

    storage_mod = sys.modules["crewai.memory.storage.kickoff_task_outputs_storage"]
    storage_mod.KickoffTaskOutputsSQLiteStorage = Elsewhere
    with pytest.raises(StructureEngineUnavailable, match="its database would be at"):
        sc.ensure_available()
    assert sc._verified is False
    assert rec.crew_kwargs is None


def test_refuses_when_the_storage_cannot_be_inspected(monkeypatch, tmp_path):
    _install_fake_crewai(monkeypatch)
    monkeypatch.setitem(sys.modules, "crewai.memory.storage.kickoff_task_outputs_storage", None)
    with pytest.raises(StructureEngineUnavailable, match="could not verify it"):
        sc.ensure_available()
    assert _our_dirs(tmp_path) == []  # the probe's temp folder is gone too


def test_verification_passes_and_leaves_nothing_behind(monkeypatch, tmp_path):
    _install_fake_crewai(monkeypatch)
    sc.ensure_available()
    assert sc._redirected is True and sc._verified is True
    assert _our_dirs(tmp_path) == []
    sc.ensure_available()  # idempotent


def test_a_broken_redirect_stops_the_pipeline_wiring_up_front(monkeypatch):
    _install_fake_crewai(monkeypatch)
    monkeypatch.setitem(sys.modules, "crewai_core.paths", None)
    with pytest.raises(StructureEngineUnavailable):
        se.make_run_pipeline("crewai")
