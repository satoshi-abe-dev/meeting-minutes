"""The real CrewAI against a fake local OpenAI-compatible server (Issue #192).

Skipped unless CrewAI is installed (the optional extra, Python 3.10-3.13) and a
local port can be opened, so CI (no CrewAI) never runs it. Nothing leaves the
machine: every outbound connection is recorded and anything but loopback is
refused. Run it in the CrewAI environment:

    .venv-py313/bin/python -m pytest tests/test_structure_crewai_real.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_minutes.model import structure_crewai as sc
from meeting_minutes.model.config import AIConfig

GOOD = (
    "# 議事録: {title}\n\n- 日時: {datetime_hint}\n- 記録時間: {duration_hint}\n\n"
    "## 進捗\n（明示されたことだけ）\n\n## 決定事項\n（該当なしなら「（該当なし）」）\n"
)
_PARALLEL_CALLS = 12  # a small model once issued ~50 searches in one response


@pytest.fixture
def crewai_env(monkeypatch, tmp_path):
    # importing crewai creates ~/Library/Application Support/<name>: keep it in tmp
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CREWAI_STORAGE_DIR", "meeting-minutes-test")
    # Not importorskip: the engine must be the first to import crewai (it
    # redirects CrewAI's data directory and switches telemetry off before that).
    if importlib.util.find_spec("crewai") is None:
        pytest.skip("CrewAI is not installed")


@pytest.fixture
def fake_server():
    seen = SimpleNamespace(requests=[], connects=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.requests.append({"auth": self.headers.get("Authorization"), "model": body.get("model")})
            msgs = body["messages"]
            system = str(msgs[0]["content"])
            message: dict
            if body.get("tools") and not any(m.get("role") == "tool" for m in msgs):
                message = {  # the Researcher: ask for many searches at once
                    "role": "assistant", "content": None,
                    "tool_calls": [
                        {"id": f"call_{i}", "type": "function",
                         "function": {"name": "search_material",
                                      "arguments": json.dumps({"keyword": f"予算{i}"}, ensure_ascii=False)}}
                        for i in range(_PARALLEL_CALLS)
                    ],
                }
                finish = "tool_calls"
            else:
                answer = GOOD if "設計者" in system else "判定: 技術定例"
                text = f"Thought: I now can give a great answer\nFinal Answer: {answer}"
                message = {"role": "assistant", "content": text}
                finish = "stop"
            resp = {
                "id": "x", "object": "chat.completion", "created": 0, "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:  # e.g. a sandbox without local port binding
        pytest.skip(f"cannot open a local port: {exc}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    seen.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield seen
    server.shutdown()


@pytest.fixture
def loopback_only(monkeypatch, fake_server):
    original = socket.socket.connect

    def guarded(self, addr):
        fake_server.connects.append(addr)
        host = addr[0] if isinstance(addr, tuple) else addr
        local = ("127.0.0.1", "::1", "localhost")
        if isinstance(host, str) and host not in local and not host.startswith("/"):
            raise OSError(f"refused non-local connection to {addr}")
        return original(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def test_real_crewai_against_a_local_server(crewai_env, fake_server, loopback_only, tmp_path):
    class Client:
        config = AIConfig(base_url=fake_server.base_url, api_key="local-test", llm_model="test-model")

    msgs: list[str] = []
    engine = sc.CrewAIStructureEngine()
    out = engine(
        Client(), "予算は100万円です。担当は田中。", model="test-model", max_tokens=500,
        minutes_system="s", ctx=0, on_progress=lambda c, t, m: msgs.append(m), cancel_event=None,
    )

    assert out is not None and "## 進捗" in out, msgs  # passes the same checks
    # only the configured local server was ever contacted, with the configured key and model
    assert fake_server.connects
    assert all(a[0] == "127.0.0.1" for a in fake_server.connects)
    assert {r["auth"] for r in fake_server.requests} == {"Bearer local-test"}
    assert {r["model"] for r in fake_server.requests} == {"test-model"}
    # the reported LLM calls match what the server really received
    assert engine.stats.llm_calls == len(fake_server.requests)
    # the cap holds against a burst of parallel tool calls
    assert 0 < engine.stats.tool_calls <= sc._MAX_TOOL_CALLS
    # no meeting-derived data was left in the (fake) home directory: CrewAI's
    # task-output database lives in the temp dir that is removed at exit
    assert list(tmp_path.rglob("*.db")) == []
    # and the temp folders CrewAI used are already gone, not waiting for exit
    assert not list(Path(tempfile.gettempdir()).glob(f"{sc._STORAGE_PREFIX}{os.getpid()}-*"))


def test_real_crewai_telemetry_switches_are_honored(crewai_env):
    sc.ensure_available()  # imports crewai the way the engine does
    from crewai_core.telemetry import Telemetry

    assert os.environ["CREWAI_DISABLE_TELEMETRY"] == "true"
    assert os.environ["OTEL_SDK_DISABLED"] == "true"
    assert Telemetry._is_telemetry_disabled()  # CrewAI's own decision
