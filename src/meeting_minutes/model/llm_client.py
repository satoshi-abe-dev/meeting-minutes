"""An OpenAI-compatible client for a local LLM/VLM server (LM Studio, etc.).

Just calls `/v1/chat/completions` directly with httpx. Does not depend on the
openai package. The endpoint is config.ai.base_url; defaults to LM Studio's
http://localhost:1234/v1. Also works if swapped for another OpenAI-compatible
backend such as Ollama.

Never connects to an external network (base_url is assumed to be localhost).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, normalize_language, t

from .config import AIConfig


class LLMConnectionError(RuntimeError):
    """Raised when the local LLM server can't be reached, or returns an error response."""


class LLMClient:
    def __init__(self, config: AIConfig, *, language: str = DEFAULT_LANGUAGE):
        self.config = config
        # The language of exception messages (connection-error hints). Defaults to "ja".
        # pipeline.run() swaps it to "en" when the GUI is run with --lang en.
        self.language = normalize_language(language)
        # Imported here once rather than inside a function (httpx is a required dependency)
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    # --- Low-level ---------------------------------------------------------
    def _post_chat(self, payload: dict) -> str:
        try:
            resp = self._client.post("/chat/completions", json=payload)
        except self._httpx.TimeoutException as exc:
            # The server is up, but the response (usually still generating)
            # took longer than the timeout. The "start the server" hint would
            # be misleading here, so use a dedicated message.
            raise LLMConnectionError(
                t("pmsg.llm_hint_timeout", self.language, timeout=self.config.timeout)
                + t("pmsg.llm_err_detail", self.language, exc=exc)
            ) from exc
        except self._httpx.RequestError as exc:
            raise LLMConnectionError(
                t("pmsg.llm_hint_conn", self.language, base_url=self.config.base_url)
                + t("pmsg.llm_err_detail", self.language, exc=exc)
            ) from exc

        if resp.status_code >= 400:
            body = resp.text[:500]
            msg = t(
                "pmsg.llm_err_http", self.language,
                status=resp.status_code, body=body,
            )
            low = body.lower()
            if "no models loaded" in low or "model_not_found" in low or (
                resp.status_code == 400 and '"param": "model"' in low
            ):
                msg += "\n" + t(
                    "pmsg.llm_hint_model", self.language, base_url=self.config.base_url
                )
            elif (
                "context length" in low
                or "context window" in low
                or "tokens to keep from the initial prompt" in low
                or "prompt is too long" in low
                or "exceeds the context" in low
            ):
                msg += "\n" + t("pmsg.llm_hint_context", self.language)
            raise LLMConnectionError(msg)
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = (message.get("content") or "").strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMConnectionError(
                t("pmsg.llm_err_unparsable", self.language, body=resp.text[:500])
            ) from exc

        if not content:
            # Reasoning models like the Qwen3 family split their response:
            # <think>-equivalent content goes into message.reasoning_content,
            # and the final answer into message.content. If max_tokens runs
            # out purely on thinking, content comes back empty (easy to miss
            # since the HTTP status is still 200).
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if reasoning:
                raise LLMConnectionError(
                    t(
                        "pmsg.llm_hint_reasoning", self.language,
                        reasoning_len=len(reasoning),
                        finish_reason=choice.get("finish_reason"),
                    )
                )
        return content

    # --- High-level -------------------------------------------------------
    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> str:
        """Text-only chat completion. Used for minutes generation and chunk summarization."""
        payload = {
            "model": model or self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        return self._post_chat(payload).strip()

    def describe_image(
        self,
        image_path: str | Path,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.1,
    ) -> str:
        """Have the VLM describe a single image."""
        data_url = _image_to_data_url(image_path)
        payload = {
            "model": model or self.config.vlm_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        return self._post_chat(payload).strip()

    def ping(self) -> bool:
        """A lightweight check that the server can be reached (GET /models)."""
        try:
            resp = self._client.get("/models")
            return resp.status_code < 500
        except self._httpx.RequestError:
            return False

    def list_models(self) -> list[str]:
        """The list of model ids the server reports (empty if it can't be fetched)."""
        try:
            resp = self._client.get("/models")
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    def loaded_context_length(self, model: str | None = None) -> int | None:
        """Return the target model's "actually loaded" context length (in tokens).

        Reads `loaded_context_length` as returned by LM Studio's extension
        endpoint GET /api/v0/models. Since this value is used as "a reliable
        ceiling that lets a safety check be skipped," this returns
        **`loaded_context_length` only when the target model is actually
        loaded**. It does not fall back to a match on ID alone (the
        advertised `max_context_length` when not loaded) or to another
        model's loaded value (a wrong value would just reproduce the HTTP
        400). Returns None if it can't be confirmed (the caller falls back to
        the character-count threshold).
        """
        try:
            from urllib.parse import urlsplit

            u = urlsplit(self.config.base_url)
            v0 = f"{u.scheme}://{u.netloc}/api/v0/models"
            resp = self._client.get(v0)
            if resp.status_code >= 400:
                return None
            entries = resp.json().get("data", [])
        except Exception:
            return None

        want = model or self.config.llm_model
        for e in entries:
            if e.get("type") not in (None, "llm"):
                continue
            if e.get("id") != want or e.get("state") != "loaded":
                continue
            v = e.get("loaded_context_length")
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return None

    def preflight(self, models: list[str]) -> None:
        """Send a minimal request to each given model to confirm it actually responds.

        Raises LLMConnectionError if even one fails. Also has the effect of
        triggering JIT loading ahead of time. Call this before heavy
        processing such as transcription.
        """
        seen: list[str] = []
        for model in models:
            if not model or model in seen:
                continue
            seen.append(model)
            try:
                self.chat("", "ping", model=model, max_tokens=1)
            except LLMConnectionError as exc:
                raise LLMConnectionError(
                    t(
                        "pmsg.llm_err_preflight", self.language,
                        model=repr(model), detail=exc,
                    )
                ) from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _image_to_data_url(image_path: str | Path) -> str:
    image_path = Path(image_path)
    mime, _ = mimetypes.guess_type(image_path.name)
    mime = mime or "image/jpeg"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"
