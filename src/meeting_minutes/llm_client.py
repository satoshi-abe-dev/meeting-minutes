"""ローカル LLM / VLM サーバー（LM Studio 等）への OpenAI 互換クライアント。

`/v1/chat/completions` を httpx で直接叩くだけ。openai パッケージには依存しない。
接続先は config.llm.base_url。既定は LM Studio の http://localhost:1234/v1。
Ollama など OpenAI 互換 API を出す他基盤に差し替えても動く。

外部ネットワークへは接続しない（base_url が localhost 前提）。
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from .config import LLMConfig


class LLMConnectionError(RuntimeError):
    """ローカル LLM サーバーに接続できない／エラー応答のときに送出する。"""


_HINT = (
    "ローカル LLM サーバーに接続できません。LM Studio を開き、"
    "Settings → Local Models → Local Model API で『Local API server』を ON "
    "（Running）にしてください。旧 UI では Developer タブの Local Server を Start。"
    "（接続先: {base_url}）"
)

_TIMEOUT_HINT = (
    "【タイムアウト】ローカル LLM サーバーへのリクエストが {timeout:.0f} 秒以内に"
    "終わらず、タイムアウトしました。サーバー自体は動いていて、応答の生成に時間が"
    "かかっているだけの可能性が高いです"
    "（大きいモデルほど、また出力トークン数が多いほど時間がかかります）。"
    "config.toml の [llm] timeout を増やしてください（例: 600）。"
)

_MODEL_HINT = (
    "LM Studio でモデルがロードされていない可能性があります。"
    "『Just-in-time model loading』を ON にするか、Loaded Instances で "
    "対象モデルをロードしてください。config の model / vlm_model が Library の"
    "モデルキー（`curl {base_url}/models` で確認可）と一致しているかも確認してください。"
)

_CONTEXT_HINT = (
    "プロンプトがモデルのコンテキスト長を超えています。LM Studio でこの LLM を"
    "ロードするときに Context Length を 32768 以上に設定して読み込み直してください"
    "（一度ロード済みなら Eject してから設定し直す）。詳しくは doc/models.md の"
    "「コンテキスト長の設定」を参照。"
    "コンテキスト長を大きくできない場合は、config.toml の "
    "[llm] chunk_trigger_chars / chunk_size_chars を小さくすると分割要約に切り替わり、"
    "1 回あたりのプロンプトが短くなります。"
)

_REASONING_HINT = (
    "モデルが「思考」（reasoning）に max_tokens を使い切り、本文を1文字も"
    "出力できませんでした（reasoning は {reasoning_len} 文字生成、本文は空、"
    "finish_reason={finish_reason!r}）。Qwen3 系などの推論モデルは、入力が長い"
    "ほど思考に多くのトークンを使います。config.toml の [llm] max_tokens を"
    "増やすか、LM Studio 側でこのモデルの reasoning（思考の強さ）を下げてください。"
)


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
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
            # 「サーバーを起動して」の _HINT は的外れなので専用メッセージにする。
            raise LLMConnectionError(
                _TIMEOUT_HINT.format(timeout=self.config.timeout) + f"\n詳細: {exc}"
            ) from exc
        except self._httpx.RequestError as exc:
            raise LLMConnectionError(
                _HINT.format(base_url=self.config.base_url) + f"\n詳細: {exc}"
            ) from exc

        if resp.status_code >= 400:
            body = resp.text[:500]
            msg = (
                f"LLM サーバーがエラーを返しました (HTTP {resp.status_code}): {body}"
            )
            low = body.lower()
            if "no models loaded" in low or "model_not_found" in low or (
                resp.status_code == 400 and '"param": "model"' in low
            ):
                msg += "\n" + _MODEL_HINT.format(base_url=self.config.base_url)
            elif (
                "context length" in low
                or "context window" in low
                or "tokens to keep from the initial prompt" in low
                or "prompt is too long" in low
                or "exceeds the context" in low
            ):
                msg += "\n" + _CONTEXT_HINT
            raise LLMConnectionError(msg)
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = (message.get("content") or "").strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMConnectionError(
                f"LLM サーバーの応答を解釈できません: {resp.text[:500]}"
            ) from exc

        if not content:
            # Qwen3 系などの推論モデルは <think> 相当の内容を message.reasoning_content
            # に、最終回答を message.content に分けて返す。max_tokens が思考だけで
            # 尽きると content が空のまま返ってくる（HTTP は 200 なので気づきにくい）。
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if reasoning:
                raise LLMConnectionError(
                    _REASONING_HINT.format(
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
            "model": model or self.config.model,
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
        except Exception:  # noqa: BLE001 - 補助情報なので握りつぶす
            return []

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
                    f"起動前チェックに失敗しました（モデル {model!r}）。\n{exc}"
                ) from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _image_to_data_url(image_path: str | Path) -> str:
    image_path = Path(image_path)
    mime, _ = mimetypes.guess_type(image_path.name)
    mime = mime or "image/jpeg"
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"
