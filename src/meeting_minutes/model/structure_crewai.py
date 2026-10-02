"""An optional CrewAI engine for Auto-mode structure generation (Issue #192).

An experiment, not the default: the single-call engine in minutes.py
(_generate_structure) stays the default, and this engine is used only when the
GUI is started with `--structure-engine crewai`. Three agents split the work:

    Classifier  decides the meeting type from the material
    Researcher  looks things up in the material through a search tool
                (numbers? deadlines? action items?)
    Designer    writes the structure from the two results

The output goes through the same checks (check_generated_structure) and the
same fallback as the single-call engine.

CrewAI is an optional extra (requirements-agent.txt) and is imported lazily, so
this module imports fine without it. Importing the module also turns CrewAI's
telemetry off, before CrewAI itself can be imported; nothing here talks to
anything but the local OpenAI-compatible server from config.ai.

Hard caps: each agent has a small max_iter, the Researcher's tool is limited to
_MAX_TOOL_CALLS calls in total (a small model once issued ~50 searches in one
response), and every agent has a wall-clock limit. LLM calls are counted from
CrewAI's usage metrics and reported in `stats`.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meeting_minutes.i18n import DEFAULT_LANGUAGE, t

from .cancel import check_cancel
from .config import DEFAULT_MINUTES_LANGUAGE
from .minutes import (
    _STRUCTURE_PROMPT,
    ProgressFn,
    StructureGenerator,
    _structure_system_prompt,
    check_generated_structure,
)
from .structure_engine import StructureEngineUnavailable

# CrewAI's own off switches for usage statistics (docs.crewai.com/en/telemetry;
# any one is enough, all are set; CrewAI 1.15.23's Telemetry checks the first
# three). CREWAI_TRACING_ENABLED keeps the opt-in tracing to CrewAI's cloud off
# as well.
_TELEMETRY_OFF = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_DISABLE_TRACKING": "true",
    "CREWAI_TRACING_ENABLED": "false",
}


def disable_telemetry() -> None:
    """Turn CrewAI's telemetry off. Overrides any existing value on purpose
    (this tool never sends usage data), and must run before crewai is imported."""
    os.environ.update(_TELEMETRY_OFF)


disable_telemetry()

_STORAGE_PREFIX = "meeting-minutes-crewai-"
_redirected = False
_storage_dirs: list[str] = []


def _pid_alive_windows(pid: int) -> bool:
    """Whether a process exists, on Windows, without os.kill: there signal 0 is
    CTRL_C_EVENT, so os.kill(pid, 0) would send Ctrl+C instead of checking."""
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_INVALID_PARAMETER = 87  # no process with this id

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Only "no such process" counts as dead; anything else (e.g. access
        # denied) means it exists or we can't tell, so it isn't treated as stale.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # can't tell: don't treat it as stale
    return True


def _remove_stale_storage() -> None:
    """Delete storage folders left behind by processes that no longer exist
    (a crash, a force quit). Folders of live processes, including another
    running copy of this app, are left alone."""
    root = Path(tempfile.gettempdir())
    for d in root.glob(f"{_STORAGE_PREFIX}*"):
        m = re.fullmatch(rf"{re.escape(_STORAGE_PREFIX)}(\d+)-.*", d.name)
        if m and d.is_dir() and not _pid_alive(int(m.group(1))):
            shutil.rmtree(d, ignore_errors=True)


def _new_storage_dir() -> str:
    path = tempfile.mkdtemp(prefix=f"{_STORAGE_PREFIX}{os.getpid()}-")  # POSIX: mode 0700
    _storage_dirs.append(path)
    return path


def cleanup_storage() -> None:
    """Delete everything CrewAI stored. Called as soon as a run ends (also after
    an exception) rather than at exit, to keep the window short."""
    while _storage_dirs:
        shutil.rmtree(_storage_dirs.pop(), ignore_errors=True)


_REFUSE = (
    "The CrewAI engine can't be used: this version of CrewAI does not allow its data "
    "directory to be redirected ({why}). CrewAI would then save the meeting material "
    "under ~/Library/Application Support, so the engine refuses to run. "
    "Use the CrewAI version from requirements-agent.txt (checked with 1.15.x)."
)


def _redirect_storage() -> None:
    """Keep CrewAI's local data out of ~/Library/Application Support.

    CrewAI saves each task's output in a SQLite file under its data directory,
    and that record includes the task description and the agent messages, i.e.
    the whole material handed to the agents (the transcript or the chunk
    summaries). Left alone, that would sit outside output/. Point the directory
    at private temp folders (mode 0700 on POSIX) that are deleted when a run ends
    (cleanup_storage), at exit as a backstop, and, for a process that was killed
    mid-run, at the next start. Must run before crewai is imported (it resolves
    the directory at import time).

    Fails closed: if the directory can't be redirected (the hook this relies on,
    crewai_core.paths.db_storage_path, is internal to CrewAI 1.15.x and may
    change), raises StructureEngineUnavailable instead of running without it.
    """
    global _redirected
    if _redirected:
        return
    try:
        import crewai_core.paths as paths
    except ImportError as exc:
        raise StructureEngineUnavailable(_REFUSE.format(why=f"crewai_core.paths: {exc}")) from exc
    if not callable(getattr(paths, "db_storage_path", None)):
        raise StructureEngineUnavailable(_REFUSE.format(why="crewai_core.paths.db_storage_path is missing"))
    _remove_stale_storage()
    atexit.register(cleanup_storage)
    paths.db_storage_path = _new_storage_dir
    _redirected = True


_verified = False


def _verify_storage_redirect() -> None:
    """Check on the real CrewAI that its task-output database lands in our temp
    folder, not in ~/Library. Builds the storage object CrewAI itself uses for a
    crew (this creates an empty database in a temp folder, deleted right away).
    Raises StructureEngineUnavailable if that can't be confirmed."""
    global _verified
    if _verified:
        return
    try:
        from crewai.memory.storage.kickoff_task_outputs_storage import (
            KickoffTaskOutputsSQLiteStorage,
        )

        db_path = Path(KickoffTaskOutputsSQLiteStorage().db_path).resolve()
        ours = [Path(d).resolve() for d in _storage_dirs]
    except Exception as exc:
        cleanup_storage()
        raise StructureEngineUnavailable(_REFUSE.format(why=f"could not verify it: {exc}")) from exc
    cleanup_storage()
    if not any(db_path.is_relative_to(d) for d in ours):
        raise StructureEngineUnavailable(_REFUSE.format(why=f"its database would be at {db_path}"))
    _verified = True


# --- Hard caps ------------------------------------------------------------
# max_iter per agent: the Classifier and Designer need no tool, so 2 is plenty.
_MAX_ITER = {"classifier": 2, "researcher": 4, "designer": 2}
# Total calls of the search tool across the whole run (counted by us).
_MAX_TOOL_CALLS = 6
# Wall-clock limit for one agent, in seconds (also bounded by config.ai.timeout).
_MAX_EXECUTION_SECONDS = 900
_SEARCH_MAX_HITS = 5
_SEARCH_LINE_CHARS = 200

_LIMIT_REACHED = (
    "検索の回数が上限に達しました。これ以上は検索せず、これまでの結果で最終回答を書いてください。"
)


@dataclass
class EngineStats:
    """What the last run cost, for the comparison script."""

    llm_calls: int | None = None  # None: CrewAI reported no usage metrics
    tool_calls: int = 0
    total_tokens: int | None = None
    notes: list[str] = field(default_factory=list)


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):  # e.g. a None entry in sys.modules
        return False


_NOT_INSTALLED = (
    "CrewAI is not installed, so --structure-engine crewai can't be used. "
    "Install the optional extra in a Python 3.10-3.13 environment: "
    "pip install -r requirements-agent.txt"
)


def _import_crewai() -> Any:
    """Import crewai (after telemetry is off and its data directory is
    redirected), or raise StructureEngineUnavailable. Never runs CrewAI with
    its data directory unredirected."""
    disable_telemetry()
    if not _installed("crewai"):
        raise StructureEngineUnavailable(_NOT_INSTALLED)
    _redirect_storage()
    try:
        import crewai
        import crewai.tools
    except ImportError as exc:
        raise StructureEngineUnavailable(_NOT_INSTALLED) from exc
    _verify_storage_redirect()
    return crewai


def ensure_available() -> None:
    """Raise StructureEngineUnavailable unless CrewAI can be imported."""
    _import_crewai()


def search_material(material: str, keyword: str) -> str:
    """The Researcher's tool: the lines of the material that contain keyword.

    Plain Python (no CrewAI), so it is tested directly. Returns a short,
    bounded text so a small model's context isn't flooded.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return "キーワードが空です。"
    needle = keyword.lower()
    hits = [ln.strip() for ln in material.splitlines() if needle in ln.lower()]
    if not hits:
        return f"「{keyword}」を含む行はありません。"
    shown = [ln[:_SEARCH_LINE_CHARS] for ln in hits[:_SEARCH_MAX_HITS]]
    more = f"\n（ほか {len(hits) - len(shown)} 行）" if len(hits) > len(shown) else ""
    return f"「{keyword}」を含む行 {len(hits)} 件:\n" + "\n".join(f"- {ln}" for ln in shown) + more


class _ToolBudget:
    """Counts search-tool calls and refuses past the cap (thread-safe)."""

    def __init__(self, material: str, limit: int = _MAX_TOOL_CALLS):
        self._material = material
        self._limit = limit
        self._lock = threading.Lock()
        self.calls = 0

    def search(self, keyword: str) -> str:
        with self._lock:
            if self.calls >= self._limit:
                return _LIMIT_REACHED
            self.calls += 1
        return search_material(self._material, keyword)


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Agents sometimes wrap the final answer in a code fence; unwrap it."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def _build_crew(
    crewai: Any,
    *,
    ai_config: Any,
    material: str,
    designer_rules: str,
    max_tokens: int,
    budget: _ToolBudget,
) -> Any:
    """Assemble the three agents and their tasks into a sequential Crew."""
    # The local server is passed explicitly: no environment-variable fallback
    # that could point at a hosted OpenAI endpoint. One LLM object per agent:
    # CrewAI sums each agent's usage, so a shared LLM would be counted once per
    # agent (verified against CrewAI 1.15.23: 3 requests were reported as 9).
    def make_llm() -> Any:
        return crewai.LLM(
            model=f"openai/{ai_config.llm_model}",
            base_url=ai_config.base_url,
            api_key=ai_config.api_key,
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=ai_config.timeout,
        )

    from crewai.tools import tool

    @tool("search_material")
    def search_material_tool(keyword: str) -> str:
        """Search the meeting material for a keyword (a short word such as
        金額, 期限, 担当). Returns up to a few matching lines."""
        return budget.search(keyword)

    def agent(key: str, role: str, goal: str, backstory: str, tools: list | None = None) -> Any:
        return crewai.Agent(
            role=role, goal=goal, backstory=backstory, llm=make_llm(),
            tools=tools or [], allow_delegation=False, verbose=False,
            max_iter=_MAX_ITER[key], max_retry_limit=1,
            max_execution_time=int(min(_MAX_EXECUTION_SECONDS, ai_config.timeout)),
        )

    classifier = agent(
        "classifier", "会議の種類の判定者",
        "会議の内容を読み、会議の種類と主題を短く判定する",
        "あなたは多くの会議を見てきた書記です。"
        "内容から会議の種類（例: 技術定例、説明会、予算会議）を見極めます。",
    )
    researcher = agent(
        "researcher", "資料の調査者",
        "議事録の見出しを決める材料として、数値・期限・担当者・宿題が資料にあるか調べる",
        "あなたは資料の検索ツールで事実を確認する調査員です。検索は多くても数回にとどめ、"
        "推測では答えません。",
        tools=[search_material_tool],
    )
    designer = agent(
        "designer", "議事録フォーマットの設計者",
        "判定結果と調査結果から、議事録の型（見出し構成）だけを設計する",
        "あなたは議事録のフォーマット設計の専門家です。実際の議事録は書きません。",
    )

    t_classify = crewai.Task(
        description=(
            "次の会議の内容（全文またはその要約）を読み、会議の種類と主題を2〜3行で判定してください。\n\n"
            f"{material}"
        ),
        expected_output="会議の種類と主題（2〜3行）",
        agent=classifier,
    )
    t_research = crewai.Task(
        description=(
            f"会議の内容は全部で {len(material)} 文字です。検索ツールを使い、次を調べてください: "
            "金額・数値、期限・日程、担当者、宿題・次アクション。"
            "それぞれ資料に出てくるかどうかを1行ずつ答えてください（あれば一例を添える）。"
        ),
        expected_output="4項目それぞれについて、資料にあるかどうかの1行",
        agent=researcher,
    )
    t_design = crewai.Task(
        description=(
            f"{designer_rules}\n\n"
            "会議の種類の判定結果と、資料の調査結果を踏まえて、この会議に最も適した議事録の型を出力してください。"
            "出力は Markdown の型だけにしてください。"
        ),
        expected_output="Markdown の見出しと指示文だけで書かれた議事録の型",
        agent=designer,
        context=[t_classify, t_research],
    )
    return crewai.Crew(
        agents=[classifier, researcher, designer],
        tasks=[t_classify, t_research, t_design],
        process=crewai.Process.sequential,
        verbose=False,
    )


class CrewAIStructureEngine:
    """Callable with the same signature as minutes._generate_structure."""

    def __init__(self, ai_config: Any | None = None):
        self._ai_config = ai_config
        self.stats = EngineStats()

    def __call__(
        self,
        client: Any,
        material: str,
        *,
        model: str,
        max_tokens: int,
        minutes_system: str,
        ctx: int,
        on_progress: ProgressFn | None,
        cancel_event: threading.Event | None,
        language: str = DEFAULT_LANGUAGE,
        minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
    ) -> str | None:
        check_cancel(cancel_event)
        self.stats = EngineStats()
        if on_progress:
            on_progress(0, 1, t("pmsg.struct_generating", language, model=model))
        try:
            crewai = _import_crewai()
            ai_config = self._ai_config or client.config
            budget = _ToolBudget(material)
            crew = _build_crew(
                crewai, ai_config=ai_config, material=material,
                designer_rules=_structure_system_prompt(minutes_language)
                + "\n\n" + _STRUCTURE_PROMPT.replace("{material}", "（上の判定結果と調査結果）"),
                max_tokens=max_tokens, budget=budget,
            )
            result = crew.kickoff()
            out = _strip_code_fence(str(getattr(result, "raw", None) or result))
            self.stats.tool_calls = budget.calls
            usage = getattr(result, "token_usage", None)
            if usage is not None:
                self.stats.llm_calls = getattr(usage, "successful_requests", None)
                self.stats.total_tokens = getattr(usage, "total_tokens", None)
        except Exception as exc:
            if on_progress:
                on_progress(0, 1, t("pmsg.struct_gen_failed", language, exc=exc))
            return None
        finally:
            cleanup_storage()  # the material must not outlive the run, however it ended
        return check_generated_structure(
            out, minutes_system=minutes_system, ctx=ctx, max_tokens=max_tokens,
            on_progress=on_progress, language=language,
        )


def load_generator() -> StructureGenerator:
    """The CrewAI engine, ready to pass as generate_minutes(structure_generator=).

    Raises StructureEngineUnavailable if CrewAI isn't installed, so the caller
    can show a clear message at start-up rather than failing mid-run.
    """
    ensure_available()
    return CrewAIStructureEngine()
