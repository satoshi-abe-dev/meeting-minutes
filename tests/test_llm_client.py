"""llm_client のエラーメッセージと preflight のテスト（サーバー不要）。"""

from __future__ import annotations

import json

import httpx
import pytest

from meeting_minutes.config import LLMConfig
from meeting_minutes.llm_client import LLMClient, LLMConnectionError


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


def _client() -> LLMClient:
    return LLMClient(LLMConfig(base_url="http://localhost:1234/v1"))


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
    assert "Just-in-time model loading" in msg  # モデルヒントが付く


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
    assert "Context Length" in msg  # LM Studio の設定名で誘導
    assert "chunk_trigger_chars" in msg  # 代替手段も案内
    assert "Just-in-time model loading" not in msg  # モデル未ロードの誤ヒントは出さない


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
    assert "タイムアウト" in msg  # エラー種別が明記されている
    assert "timeout を増やして" in msg
    assert "Local API server" not in msg  # サーバー未起動用のヒントは出さない


def test_connection_error_still_gets_server_down_hint(monkeypatch):
    c = _client()

    def raise_connect_error(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(c._client, "post", raise_connect_error)

    with pytest.raises(LLMConnectionError) as ei:
        c.chat("", "hi")
    assert "Local API server" in str(ei.value)


def test_empty_content_with_reasoning_raises_dedicated_hint(monkeypatch):
    """推論モデルが思考だけで max_tokens を使い切り、本文が空で返るケース。"""
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
    assert "300" in msg  # reasoning の文字数（len("考え中"*100) == 300）が出る


def test_empty_content_without_reasoning_just_returns_empty(monkeypatch):
    """reasoning が無いのに空文字が返る場合まで壊れているとは決めつけない。"""
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
                    "max_context_length": 32768,
                },
                {"id": "some-vlm", "type": "vlm", "state": "not-loaded",
                 "max_context_length": 262144},
            ]
        }
    )
    monkeypatch.setattr(c._client, "get", lambda *a, **k: _Resp(200, body))
    assert c.loaded_context_length() == 32768  # 既定 model に一致するエントリ
    assert c.loaded_context_length("qwen2.5-7b-instruct") == 32768


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
    c.preflight(["llm-a", "vlm-b", "llm-a"])  # 重複は 1 回に
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
