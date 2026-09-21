"""ローカル LLM / VLM サーバー（LM Studio 等）への OpenAI 互換クライアント。

`/v1/chat/completions` を httpx で直接叩くだけ。openai パッケージには依存しない。
接続先は config.ai.base_url。既定は LM Studio の http://localhost:1234/v1。
Ollama など OpenAI 互換 API を出す他基盤に差し替えても動く。

外部ネットワークへは接続しない（base_url が localhost 前提）。
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, normalize_language, t

from .config import AIConfig


class LLMConnectionError(RuntimeError):
    """ローカル LLM サーバーに接続できない／エラー応答のときに送出する。"""


class LLMClient:
    def __init__(self, config: AIConfig, *, language: str = DEFAULT_LANGUAGE):
        self.config = config
        # 例外メッセージ（接続エラー時のヒント）の言語。既定は "ja"。
        # GUI が --lang en のとき pipeline.run() 側で "en" に差し替える。
        self.language = normalize_language(language)
        # 関数内 import ではなくここで一度だけ（httpx は requirements 必須依存）
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout,
            headers={"Authorization": f"Bearer {config.api_key}"},
        )

    # --- 低レベル ---------------------------------------------------------
    def _post_chat(self, payload: dict) -> str:
        try:
            resp = self._client.post("/chat/completions", json=payload)
        except self._httpx.TimeoutException as exc:
            # サーバーは動いているが、応答（多くは生成中）が timeout より長い場合。
            # 「サーバーを起動して」のヒントは的外れなので専用メッセージにする。
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
            # Qwen3 系などの推論モデルは <think> 相当の内容を message.reasoning_content
            # に、最終回答を message.content に分けて返す。max_tokens が思考だけで
            # 尽きると content が空のまま返ってくる（HTTP は 200 なので気づきにくい）。
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

    # --- 高レベル -------------------------------------------------------
    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> str:
        """テキストのみのチャット補完。議事録生成やチャンク要約に使う。"""
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
        """画像 1 枚を VLM に説明させる。"""
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
        """サーバーに到達できるか軽く確認する（GET /models）。"""
        try:
            resp = self._client.get("/models")
            return resp.status_code < 500
        except self._httpx.RequestError:
            return False

    def list_models(self) -> list[str]:
        """サーバーが返すモデル id 一覧（取得できなければ空）。"""
        try:
            resp = self._client.get("/models")
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception:
            return []

    def loaded_context_length(self, model: str | None = None) -> int | None:
        """対象モデルの「実際にロードされている」コンテキスト長（トークン）を返す。

        LM Studio 拡張の GET /api/v0/models が返す `loaded_context_length` を読む。
        この値は「安全チェックを無効化してよい確かな上限」として使われるため、
        **対象モデルが実際にロードされている場合の loaded_context_length のみ**を
        返す。ID だけ一致（未ロード時の広告値 max_context_length）や、別モデルの
        ロード値へのフォールバックは行わない（誤値は HTTP 400 を再発させるため）。
        確認できなければ None（呼び出し側は文字数しきい値にフォールバックする）。
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
        """指定モデルそれぞれに極小のリクエストを投げ、実際に応答できるか確認する。

        1 つでも失敗したら LLMConnectionError を送出する。JIT ロードを前倒しで
        起こす効果もある。文字起こしなど重い処理の前に呼ぶこと。
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
