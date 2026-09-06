"""文字起こし＋フレーム解析から議事録（Markdown）を生成する。

自由文の要約ではなく、決まった型（日時・出席者・議題・決定事項・宿題／担当・期限）
を LLM に埋めさせる。文字起こしが長い場合は「チャンク要約 → 統合」の 2 段で処理する。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .cancel import check_cancel
from .config import LLMConfig, load_prompt
from .llm_client import LLMClient
from .transcribe import Segment, transcript_to_text
from .vision import FrameNote, notes_to_text

# 進捗コールバック: (完了ステップ, 総ステップ, メッセージ)
ProgressFn = Callable[[int, int, str], None]

# これを超えたら分割要約する（おおよその文字数。日本語なら 1 文字 ≒ 1〜1.5 トークン）。
# 分割すると「要約の要約」から議事録を作ることになり具体性が大きく落ちるため、
# コンテキスト長に収まる限りは一発生成（分割なし）を優先する。LLM 側は 32k 程度の
# コンテキストで運用する前提（doc/models.md「コンテキスト長の設定」）。
# 実値は config.toml の [llm] chunk_trigger_chars / chunk_size_chars で調整可能で、
# 以下はそれが無い場合のフォールバック既定値。
_CHUNK_TRIGGER_CHARS = 40000
_CHUNK_SIZE_CHARS = 15000

_DEFAULT_SYSTEM = """あなたは会議の議事録作成の専門家です。
渡された「文字起こし」と「画面キャプチャの説明」から、日本語で正確な議事録を作成します。

厳守:
- 文字起こし・資料に明示的に出てくる内容だけを書く。人名・日付・数値・組織名を
  推測で補わない。実在しない担当者名や期限を作らない。
- 根拠がない項目は「（記載なし）」または「（該当なし）」と書く。
- 「スライドが表示された」「資料が共有された」のようなメタ説明は禁止。資料は中身
  （読み取れた文字・箇条書き・数値）をそのまま書く。
- 会議ではなく説明会・ブリーフィング（主に1人が話す）の場合、「決定事項」「宿題・
  アクションアイテム」は該当がなければ「（該当なし）」とし、「共有された情報」を厚く書く。
- 出力は Markdown のみ。前置き・後書き・謝辞・自己言及は書かない。"""

_MINUTES_TEMPLATE = """以下のテンプレートに沿って議事録を作成してください。

# 議事録: {title}

- 日時: {datetime_hint}
- 記録時間: {duration_hint}
- 出席者: （文字起こしから読み取れる範囲で。不明なら「（記載なし）」）

## 目的・アジェンダ
（文字起こし冒頭で述べられた目的・進行予定。無ければ「（記載なし）」）

## 議論の要点
（時系列の箇条書き。具体的に書く。固有名詞・数値・日付・地名・持ち物名・手順を
落とさない。「〜について説明した」で終わらせず、説明された中身まで書く）

## 決定事項
（「〜する」「〜に決めた」と明言されたものだけ。検討中・保留はここに書かない。
無ければ「（該当なし）」）

## 宿題・アクションアイテム
（「誰が・いつまでに・何を」する、と文字起こしで明言された場合のみ下表を作る。
明言が無ければ表は作らず「（明示的なアクションアイテムなし）」と書く。
担当者名・期限を推測で埋めない）

| 内容 | 担当 | 期限 |
| --- | --- | --- |

## 保留・次回への持ち越し
（未確定・次回送りと述べられたもの。無ければ「（該当なし）」）

## 資料（スライド）の内容
（各スクリーンショットから読み取れた文字・箇条書き・数値・表を、時刻順にそのまま
列挙する。「スライドが表示された」「資料が共有された」等のメタ説明は書かない。
読み取れる情報が無ければ「（読み取れる資料なし）」）

---

## 入力: 文字起こし
{transcript}

## 入力: 画面キャプチャの説明（時刻付き）
{frames}
"""

_CHUNK_PROMPT = """次の会議の文字起こしの一部です。後で議事録にまとめるための素材として、
話題・発言の要点・数値・決定事項の候補・宿題の候補を、時刻を保ったまま日本語で箇条書きにしてください。
要約しすぎず、固有名詞・数字・日付・地名・持ち物名はそのまま残してください。
文字起こしに無い人名・日付・数値を補わないでください。推測は書かず、書かれていることだけを拾います。

--- 文字起こし（部分） ---
{chunk}
"""


@dataclass
class MinutesMeta:
    title: str
    datetime_hint: str = "（記載なし）"
    duration_hint: str = "（不明）"


def _format_elapsed(seconds: float) -> str:
    """処理にかかった時間の表示用（pipeline.py にも同名の小関数がある。用途が
    近い割にモジュールをまたぐほどではないのでローカルに複製している）。"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分"


# minutes_partials.json のフォーマット版。チャンク境界のシグネチャを持つ。
_PARTIALS_FORMAT = 2


def _partials_path(out_dir: Path) -> Path:
    return out_dir / "minutes_partials.json"


def _load_partials(
    out_dir: Path, *, size_chars: int, num_segments: int
) -> list[str]:
    """中断・タイムアウトで途中まで進んだチャンク要約を読み戻す（無ければ空）。

    再利用の可否は、要約を作ったときのチャンク分割条件（`chunk_size_chars` と
    分割入力のセグメント総数）が今回と一致するかで判定する。一致しなければ
    チャンク境界がずれて内容の重複・欠落が起きるため採用しない（作り直す）。
    メタ情報の無い旧形式（JSON 配列）も、境界を検証できないので採用しない。

    見出し行（"### 部分 N"）しか無く本文が空のエントリは、LLM が空応答を返した
    形跡（例: 推論モデルが思考だけで max_tokens を使い切った）とみなし無効にする。
    そのエントリ以降は信用せず、そこから要約をやり直す。
    """
    path = _partials_path(out_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    # 旧形式（配列）・未知形式は作り直す
    if not isinstance(data, dict) or data.get("format") != _PARTIALS_FORMAT:
        return []
    # 分割条件が変わっている（例: Context Length 対策で chunk_size_chars を下げた）
    if data.get("chunk_size_chars") != size_chars or data.get("num_segments") != num_segments:
        return []

    entries = data.get("partials")
    if not (isinstance(entries, list) and all(isinstance(x, str) for x in entries)):
        return []

    valid: list[str] = []
    for entry in entries:
        body = entry.split("\n", 1)[1] if "\n" in entry else ""
        if not body.strip():
            break
        valid.append(entry)
    return valid


def _save_partials(
    partials: list[str],
    out_dir: Path,
    *,
    size_chars: int,
    num_segments: int,
    num_chunks: int,
) -> None:
    """チャンク要約を1つ終えるたびに呼び、その時点までを丸ごと書き直す。

    再開時に整合を検証できるよう、チャンク分割条件（`chunk_size_chars` と
    分割入力のセグメント総数）を一緒に保存する。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": _PARTIALS_FORMAT,
        "chunk_size_chars": size_chars,
        "num_segments": num_segments,
        "num_chunks": num_chunks,
        "partials": partials,
    }
    _partials_path(out_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _system_prompt() -> str:
    try:
        text = load_prompt("minutes_ja.txt").strip()
        return text or _DEFAULT_SYSTEM
    except FileNotFoundError:
        return _DEFAULT_SYSTEM


def _split_segments(segments: list[Segment], size_chars: int) -> list[list[Segment]]:
    chunks: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_len = 0
    for seg in segments:
        seg_len = len(seg.text) + 12  # タイムスタンプ分の余白
        if cur and cur_len + seg_len > size_chars:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.append(seg)
        cur_len += seg_len
    if cur:
        chunks.append(cur)
    return chunks


def generate_minutes(
    segments: list[Segment],
    notes: list[FrameNote],
    client: LLMClient,
    llm_config: LLMConfig,
    meta: MinutesMeta,
    *,
    on_progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
    out_dir: str | Path | None = None,
    reuse: bool = True,
) -> str:
    """議事録の Markdown 文字列を返す。

    cancel_event: セットされていれば、チャンク要約の合間（長い文字起こしの場合）で
        中断する。短いパス（1 回の chat 呼び出し）は呼び出し中に反応できない。
    out_dir: 指定すると、長い文字起こしのチャンク要約を1つ終えるたびに
        `minutes_partials.json` として書き出す。タイムアウトや中断のあとの
        再実行では、reuse=True ならここから再開し、終わっているチャンクを
        summarize し直さない。
    reuse: False なら out_dir に部分要約が残っていても無視して最初から。
    """
    check_cancel(cancel_event)
    system = _system_prompt()
    full_transcript = transcript_to_text(segments)
    frames_text = notes_to_text(notes) or "（フレームなし）"

    trigger_chars = getattr(llm_config, "chunk_trigger_chars", None) or _CHUNK_TRIGGER_CHARS
    size_chars = getattr(llm_config, "chunk_size_chars", None) or _CHUNK_SIZE_CHARS

    if len(full_transcript) <= trigger_chars:
        if on_progress:
            on_progress(
                0, 1,
                f"議事録を生成中…応答を待っています（モデル: {llm_config.model}）",
            )
        user = _MINUTES_TEMPLATE.format(
            title=meta.title,
            datetime_hint=meta.datetime_hint,
            duration_hint=meta.duration_hint,
            transcript=full_transcript,
            frames=frames_text,
        )
        t0 = time.monotonic()
        md = client.chat(system, user)
        elapsed = _format_elapsed(time.monotonic() - t0)
        if on_progress:
            on_progress(1, 1, f"議事録を生成しました（所要 {elapsed}）")
        return md.strip() + "\n"

    # --- 長い場合: チャンク要約 -> 統合 ---
    chunks = _split_segments(segments, size_chars)
    total_steps = len(chunks) + 1
    out_path = Path(out_dir) if out_dir is not None else None

    partials: list[str] = []
    if reuse and out_path is not None:
        existing = _load_partials(
            out_path, size_chars=size_chars, num_segments=len(segments)
        )
        if 0 < len(existing) <= len(chunks):
            partials = existing
            if on_progress:
                on_progress(
                    len(partials), total_steps,
                    f"既存の部分要約を再利用（{len(partials)}/{len(chunks)}）",
                )
        elif on_progress and _partials_path(out_path).is_file():
            on_progress(
                0, total_steps,
                "保存済みの部分要約は分割設定が変わっている（または旧形式）ため使わず、"
                "最初から要約し直します",
            )

    for i in range(len(partials) + 1, len(chunks) + 1):
        check_cancel(cancel_event)
        if on_progress:
            on_progress(
                i - 1, total_steps,
                f"部分要約 {i}/{len(chunks)} の応答を待っています…"
                f"（モデル: {llm_config.model}）",
            )
        chunk_text = transcript_to_text(chunks[i - 1])
        t0 = time.monotonic()
        summary = client.chat(
            system="あなたは会議の記録を整理するアシスタントです。Markdown の箇条書きのみ出力します。",
            user=_CHUNK_PROMPT.format(chunk=chunk_text),
            # 固定の小さい上限（旧: 1500）だと、推論モデルが思考だけで使い切って
            # 本文が空になることがあった。議事録本体と同じ max_tokens を与える。
            max_tokens=llm_config.max_tokens,
        )
        elapsed = _format_elapsed(time.monotonic() - t0)
        partials.append(f"### 部分 {i}\n{summary.strip()}")
        if out_path is not None:
            _save_partials(
                partials, out_path,
                size_chars=size_chars,
                num_segments=len(segments),
                num_chunks=len(chunks),
            )
        if on_progress:
            on_progress(
                i, total_steps,
                f"部分要約 {i}/{len(chunks)} 完了（所要 {elapsed}）",
            )

    if on_progress:
        on_progress(
            len(chunks), total_steps,
            f"議事録に統合中…応答を待っています（モデル: {llm_config.model}）",
        )
    merged_transcript = "\n\n".join(partials)
    user = _MINUTES_TEMPLATE.format(
        title=meta.title,
        datetime_hint=meta.datetime_hint,
        duration_hint=meta.duration_hint,
        transcript=merged_transcript,
        frames=frames_text,
    )
    t0 = time.monotonic()
    md = client.chat(system, user)
    elapsed = _format_elapsed(time.monotonic() - t0)
    if on_progress:
        on_progress(total_steps, total_steps, f"議事録を生成しました（所要 {elapsed}）")
    return md.strip() + "\n"


def save_minutes(markdown: str, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "minutes.md"
    path.write_text(markdown, encoding="utf-8")
    return path
