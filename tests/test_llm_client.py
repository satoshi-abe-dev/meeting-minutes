"""Tests for llm_client's error messages and preflight (no server required)."""

from __future__ import annotations

import json

import httpx
import pytest

from meeting_minutes.model.config import AIConfig
from meeting_minutes.model.llm_client import LLMClient, LLMConnectionError


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


def _client() -> LLMClient:
    return LLMClient(AIConfig(base_url="http://localhost:1234/v1"))


def test_400_no_models_loaded_adds_model_hint(monkeypatch):
    c = _client()
    body = (
        '{"error": {"message": "No models loaded. Please load a model.", '
        '"type": "invalid_request_error", "param": "model"}}'
    )
    monkeypatch.setattr(c._client, "post", lambda *a, **k: _Resp(400, body))

    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    msg = str(ei.value)
    assert "HTTP 400" in msg
    assert "Just-in-time model loading" in msg  # the model hint is included


def test_400_context_length_exceeded_adds_context_hint(monkeypatch):
    c = _client()
    body = (
        '{"error":"The number of tokens to keep from the initial prompt is greater '
        'than the context length. Try to load the model with a larger context '
        'length, or provide a shorter input"}'
    )
    monkeypatch.setattr(c._client, "post", lambda *a, **k: _Resp(400, body))

    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    msg = str(ei.value)
    assert "HTTP 400" in msg
    assert "Context Length" in msg  # points to it by LM Studio's setting name
    assert "chunk_trigger_chars" in msg  # also guides toward the alternative
    assert "Just-in-time model loading" not in msg  # doesn't show the misleading model-not-loaded hint


def test_500_error_has_no_model_hint(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c._client, "post", lambda *a, **k: _Resp(500, "internal error")
    )
    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    assert "Just-in-time model loading" not in str(ei.value)


def test_timeout_gets_dedicated_hint_not_server_down_hint(monkeypatch):
    c = _client()

    def raise_timeout(*a, **k):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(c._client, "post", raise_timeout)

    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    msg = str(ei.value)
    assert "[Timeout]" in msg  # the error kind is stated explicitly
    assert "Increase [ai] timeout" in msg
    assert "Local API server" not in msg  # doesn't show the server-not-running hint


def test_connection_error_still_gets_server_down_hint(monkeypatch):
    c = _client()

    def raise_connect_error(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(c._client, "post", raise_connect_error)

    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    assert "Local API server" in str(ei.value)


def test_error_hints_translated_when_language_en(monkeypatch):
    c = LLMClient(AIConfig(base_url="http://localhost:1234/v1"), language="en")

    def raise_connect_error(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(c._client, "post", raise_connect_error)
    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    msg = str(ei.value)
    assert "Cannot connect to the local LLM server" in msg
    assert "Details:" in msg
    assert "接続できません" not in msg


def test_preflight_failure_message_translated_when_language_en(monkeypatch):
    c = LLMClient(AIConfig(base_url="http://localhost:1234/v1"), language="en")

    def fake_chat(system, user, *, model=None, **kw):
        raise LLMConnectionError("boom")

    monkeypatch.setattr(c, "chat", fake_chat)
    with pytest.raises(LLMConnectionError) as ei:
        c.preflight(["bad-model"])
    msg = str(ei.value)
    assert "Preflight check failed" in msg
    assert "bad-model" in msg
    assert "起動前チェック" not in msg


def test_empty_content_with_reasoning_raises_dedicated_hint(monkeypatch):
    """The case where a reasoning model uses up max_tokens on thinking alone and returns an empty body."""
    c = _client()
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "考え中" * 100,
                    },
                    "finish_reason": "length",
                }
            ]
        }
    )
    monkeypatch.setattr(c._client, "post", lambda *a, **k: _Resp(200, body))

    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    msg = str(ei.value)
    assert "max_tokens" in msg
    assert "300" in msg  # the reasoning character count (len("考え中"*100) == 300) is included


def test_empty_content_without_reasoning_just_returns_empty(monkeypatch):
    """Don't assume something is broken just because an empty string comes back with no reasoning present."""
    c = _client()
    body = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}
    )
    monkeypatch.setattr(c._client, "post", lambda *a, **k: _Resp(200, body))

    assert c.chat("", "hi") == ""


def test_loaded_context_length_reads_lmstudio_v0(monkeypatch):
    c = _client()
    body = json.dumps(
        {
            "data": [
                {
                    "id": "qwen2.5-7b-instruct",
                    "type": "llm",
                    "state": "loaded",
                    "loaded_context_length": 32768,
                    "max_context_length": 262144,
                },
                {"id": "some-vlm", "type": "vlm", "state": "not-loaded",
                 "max_context_length": 262144},
            ]
        }
    )
    monkeypatch.setattr(c._client, "get", lambda *a, **k: _Resp(200, body))
    # returns only a loaded model's loaded_context_length (not the advertised max_context_length)
    assert c.loaded_context_length() == 32768
    assert c.loaded_context_length("qwen2.5-7b-instruct") == 32768


def test_loaded_context_length_none_when_target_not_loaded(monkeypatch):
    """If the target model isn't loaded, never fall back to the advertised value (max_context_length)."""
    c = _client()
    body = json.dumps(
        {
            "data": [
                {"id": "qwen2.5-7b-instruct", "type": "llm", "state": "not-loaded",
                 "max_context_length": 262144},
                {"id": "other-llm", "type": "llm", "state": "loaded",
                 "loaded_context_length": 4096},
            ]
        }
    )
    monkeypatch.setattr(c._client, "get", lambda *a, **k: _Resp(200, body))
    # ID matches but not loaded -> None (also doesn't leak other-llm's loaded value of 4096)
    assert c.loaded_context_length() is None


def test_loaded_context_length_none_when_unavailable(monkeypatch):
    c = _client()
    monkeypatch.setattr(c._client, "get", lambda *a, **k: _Resp(404, "not found"))
    assert c.loaded_context_length() is None

    def boom(*a, **k):
        raise httpx.ConnectError("x")

    monkeypatch.setattr(c._client, "get", boom)
    assert c.loaded_context_length() is None


def test_preflight_ok(monkeypatch):
    c = _client()
    calls: list[str] = []
    monkeypatch.setattr(
        c, "chat", lambda system, user, **kw: calls.append(kw.get("model")) or "ok"
    )
    c.preflight(["llm-a", "vlm-b", "llm-a"])  # duplicates are collapsed to one
    assert calls == ["llm-a", "vlm-b"]


def test_preflight_raises_with_model_name(monkeypatch):
    c = _client()

    def fake_chat(system, user, *, model=None, **kw):
        if model == "bad-model":
            raise LLMConnectionError("HTTP 400: No models loaded")
        return "ok"

    monkeypatch.setattr(c, "chat", fake_chat)
    with pytest.raises(LLMConnectionError) as ei:
        c.preflight(["good", "bad-model"])
    assert "bad-model" in str(ei.value)
