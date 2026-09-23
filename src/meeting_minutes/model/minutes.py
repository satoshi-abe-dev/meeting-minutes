"""Generate minutes (Markdown) from the transcript + frame analysis.

Rather than a free-form summary, the LLM fills in a fixed structure (date/time,
attendees, agenda, decisions, action items/owners, due dates). For long
transcripts, this is done in two stages: "chunk summarization → merge."
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, format_elapsed, t

from .cancel import check_cancel
from .config import (
    DEFAULT_MINUTES_LANGUAGE,
    AIConfig,
    apply_minutes_language,
    load_prompt,
    minutes_language_name,
)
from .llm_client import LLMClient
from .transcribe import Segment, transcript_to_text
from .vision import FrameNote, notes_to_text

# Progress callback: (completed steps, total steps, message)
ProgressFn = Callable[[int, int, str], None]

# --- Switching between one-shot and split generation --------------------
# Splitting turns the minutes into a "summary of a summary," which loses
# specificity, so one-shot generation is preferred whenever it fits within
# the context length. Whether it fits is judged by an estimated token count
# against the real context length when available (`context_tokens`, fetched
# by the pipeline from LM Studio's /api/v0/models), falling back to the
# character-count threshold below only when that isn't available.
#
# Measured Japanese-text behavior of the Qwen tokenizer family is about 0.74
# tokens/character (kana 0.52 / mixed kanji+kana 0.75-0.80 / verbatim spoken
# language 0.52). Rounded up a bit to stay on the safe side.
_TOKENS_PER_CHAR = 0.8
# Margin (in tokens) for the system prompt + template skeleton + uncertainty.
_PROMPT_MARGIN_TOKENS = 1500
# Upper bound on tokens expected for the minutes body (the final chat output).
# Used to reserve context budget. Since this is a fill-in-the-template task, real
# usage stays within this range. The larger max_tokens needed for reasoning
# models isn't used here (per docs/models_ja.md, reasoning models are out of scope).
_MINUTES_RESPONSE_TOKENS = 5000
# Token cap so that frames_text (the concatenated frame analysis) doesn't eat
# up the whole prompt. Allowed up to ctx/3 when the real context length is known.
_FRAMES_TOKEN_BUDGET = 6000

# Fallback (character-count threshold) for backends where the real context
# length can't be fetched. Roughly the character count that fits in what's
# left after subtracting the frames cap, response reservation, and margin,
# assuming a 32k context. Can be overridden via config.toml's
# [ai] chunk_trigger_chars / chunk_size_chars (lower it further on setups that
# can't raise the Context Length).
_CHUNK_TRIGGER_CHARS = 20000
_CHUNK_SIZE_CHARS = 12000


def _approx_tokens(text: str) -> int:
    """Roughly estimate the token count from the character count (based on
    measured Qwen-family Japanese behavior)."""
    return int(len(text) * _TOKENS_PER_CHAR) + 1


def _truncate_to_token_budget(text: str, budget_tokens: int) -> str:
    """Truncate by character if the estimated token count exceeds the budget."""
    if _approx_tokens(text) <= budget_tokens:
        return text
    keep = max(0, int(budget_tokens / _TOKENS_PER_CHAR) - 40)
    return text[:keep].rstrip() + "\n…（フレーム説明はコンテキスト長の都合でここまで）"

_DEFAULT_SYSTEM = """あなたは会議の議事録作成の専門家です。
渡された「文字起こし」と「画面キャプチャの説明」から、{lang}で正確な議事録を作成します。

厳守:
- 文字起こし・資料に明示的に出てくる内容だけを書く。人名・日付・数値・組織名を
  推測で補わない。実在しない担当者名や期限を作らない。
- 根拠がない項目は「（記載なし）」または「（該当なし）」と書く。
- 「スライドが表示された」「資料が共有された」のようなメタ説明は禁止。資料は中身
  （読み取れた文字・箇条書き・数値）をそのまま書く。
- 会議ではなく説明会・ブリーフィング（主に1人が話す）の場合、「決定事項」「宿題・
  アクションアイテム」は該当がなければ「（該当なし）」とし、「共有された情報」を厚く書く。
- テンプレートの見出しの下にある丸括弧（（…））の文は「そこに何を書くか」の指示です。
  その指示文そのものを議事録に書き写してはいけません。指示に従って実際の内容に置き換え、
  書くことが無ければ指示どおり「（記載なし）」「（該当なし）」等に置き換えます。
  丸括弧の指示文が最終的な議事録に残ってはいけません。
- 出力は Markdown のみ。前置き・後書き・謝辞・自己言及は書かない。"""

_MINUTES_PREAMBLE = (
    "以下のテンプレートに沿って議事録を作成してください。丸括弧（（…））の中は"
    "「そこに何を書くか」の指示です。その文言自体は書き写さず、実際の内容に置き換えて"
    "ください（書くことが無ければ指示どおり「（記載なし）」等に）。\n\n"
)

# Appended to _MINUTES_PREAMBLE when minutes_language isn't Japanese. The
# structure/template below (built-in, a custom file, or even an
# auto-generated one whose generation failed and fell back to built-in) is
# written with Japanese heading labels — without this, a model tends to
# translate the body content but leave the heading labels themselves in
# Japanese verbatim (they read as fixed template formatting rather than text
# to translate), producing minutes with mixed languages. The system prompt's
# general "write consistently in {lang}" directive (config.apply_minutes_language)
# alone isn't specific enough to reliably override that.
_HEADING_TRANSLATION_NOTE = (
    "このテンプレートの見出し（#・##・- で始まるラベル）はひな形として日本語で"
    "書かれていますが、そのまま使わず、見出し・本文とも{lang}に翻訳し、出力全体を"
    "{lang}で統一してください。日本語の見出しをそのまま残してはいけません。\n\n"
)

# Only the minutes "structure." Can be swapped wholesale via config.toml's
# [output] template_path.
# Placeholders available: {title} {datetime_hint} {duration_hint}
# (if {transcript} / {frames} aren't written, an input section is appended automatically)
_MINUTES_STRUCTURE = """# 議事録: {title}

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
"""

# Input section. If a custom template doesn't write the corresponding
# placeholder, only the "missing one" is appended at the end (handled
# individually so that writing only {transcript} and forgetting {frames}
# doesn't drop the frame information entirely).
_INPUT_SEP = "\n---\n\n"
_INPUT_TRANSCRIPT = "## 入力: 文字起こし\n{transcript}\n"
_INPUT_FRAMES = "## 入力: 画面キャプチャの説明（時刻付き）\n{frames}\n"
# For token estimation (skeleton_tokens): the worst case where both are appended.
_MINUTES_INPUT = _INPUT_SEP + _INPUT_TRANSCRIPT + "\n" + _INPUT_FRAMES

# Placeholders inside the template. Only the names listed here are substituted;
# a bare { } is left untouched.
_PLACEHOLDER_RE = re.compile(
    r"\{(title|datetime_hint|duration_hint|transcript|frames)\}"
)

# "Instruction text" inside a template's full-width parentheses （ ）, allowing
# one level of nesting inside (e.g. "...if none, write '(not stated)'"). Used to
# detect whether this wording leaked as-is into the generated minutes (the
# prompt already tells the model not to do this; this is a safety net for
# smaller models).
_INSTRUCTION_RE = re.compile(r"（(?:[^（）]|（[^（）]*）)+）")
# Exclude short parenthetical phrases like "(not stated)" / "(not applicable)"
# that are legitimately allowed to appear in the actual minutes.
_INSTRUCTION_MIN_CHARS = 12


def _leaked_instructions(structure: str, minutes_md: str) -> list[str]:
    """Return the parenthetical instruction phrases from structure (the template in
    use) that still remain as-is in the generated minutes. Short boilerplate
    phrases like "(not stated)" are excluded."""
    seen: set[str] = set()
    leaks: list[str] = []
    for m in _INSTRUCTION_RE.finditer(structure):
        frag = m.group(0)
        if len(frag.strip("（）").strip()) < _INSTRUCTION_MIN_CHARS:
            continue
        if frag in minutes_md and frag not in seen:
            seen.add(frag)
            leaks.append(frag)
    return leaks


def _warn_leaked_instructions(
    structure: str, minutes_md: str, on_progress: ProgressFn | None,
    language: str = DEFAULT_LANGUAGE,
) -> None:
    if not on_progress:
        return
    leaks = _leaked_instructions(structure, minutes_md)
    if not leaks:
        return
    head = leaks[0][:40] + ("…" if len(leaks[0]) > 40 else "")
    on_progress(
        0, 1,
        t("pmsg.leaked_instructions", language, n=len(leaks), head=head),
    )


def _minutes_budget(
    system: str,
    structure: str,
    full_transcript: str,
    frames_text_raw: str,
    *,
    ctx: int,
    minutes_max_tokens: int,
    trigger_chars: int,
) -> tuple[str, bool]:
    """Return (frames_text truncated to fit the budget, whether one-shot generation fits).

    This depends on the size of the template (structure), so if Auto mode swaps
    in a generated structure, this must be recalculated against the resulting
    structure (this prevents the same kind of problem as PR #19, where swapping
    the structure made the final request exceed the real context length).
    """
    skeleton = (
        _approx_tokens(system) + _approx_tokens(structure) + _approx_tokens(_MINUTES_INPUT)
    )
    # Cap frames_text so it doesn't eat up the whole prompt. When ctx is known,
    # aim for "1/3" of it while not exceeding what's left after the response
    # reservation, margin, and skeleton (this avoids forcing the 6000 floor and
    # overflowing on a small ctx).
    if ctx <= 0:
        frames_budget = _FRAMES_TOKEN_BUDGET
    else:
        room_after_reserve = ctx - minutes_max_tokens - _PROMPT_MARGIN_TOKENS - skeleton
        frames_budget = max(0, min(ctx // 3, room_after_reserve))
    frames_text = _truncate_to_token_budget(frames_text_raw, frames_budget)

    if ctx > 0:
        est_prompt = (
            skeleton + _approx_tokens(full_transcript) + _approx_tokens(frames_text)
        )
        one_pass = est_prompt + minutes_max_tokens + _PROMPT_MARGIN_TOKENS <= ctx
    else:
        one_pass = len(full_transcript) <= trigger_chars
    return frames_text, one_pass


def load_minutes_structure(
    template_path: str | Path | None,
    *,
    on_warning: Callable[[str], None] | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Read a custom minutes template (structure only).

    If template_path is empty/None, use the built-in template. Even if given,
    if the file doesn't exist, can't be read, or is empty, don't stop with an
    error — fall back to the built-in template and notify via on_warning.
    """
    if not template_path:
        return _MINUTES_STRUCTURE
    p = Path(template_path).expanduser()
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:  # ValueError: UnicodeDecodeError
        if on_warning:
            on_warning(t("pmsg.tpl_unreadable", language, p=p, exc=exc))
        return _MINUTES_STRUCTURE
    if not text:
        if on_warning:
            on_warning(t("pmsg.tpl_empty", language, p=p))
        return _MINUTES_STRUCTURE
    return text


def _fill_minutes_template(
    structure: str,
    meta: MinutesMeta,
    transcript: str,
    frames: str,
    minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
) -> str:
    """Fill the template (structure) with meta info and input, returning the finished prompt.

    - Rather than sequential .replace calls, this is a single-pass substitution
      that scans the template string once (re.sub + a callback). Substituted
      values (e.g. transcript) are not rescanned, so a string like "{frames}"
      that happens to appear inside the transcript is not caught up in it. A
      bare { } (e.g. in a JSON example) isn't in _PLACEHOLDER_RE, so it's left alone.
    - If the structure doesn't write {transcript} / {frames}, only the "missing one" is appended at the end.
    - minutes_language: when not Japanese, adds an explicit instruction to
      also translate the template's (Japanese) heading labels — see
      _HEADING_TRANSLATION_NOTE.
    """
    preamble = _MINUTES_PREAMBLE
    lang_name = minutes_language_name(minutes_language)
    if lang_name != "日本語":
        preamble += _HEADING_TRANSLATION_NOTE.replace("{lang}", lang_name)
    body = preamble + structure
    tail: list[str] = []
    if "{transcript}" not in structure:
        tail.append(_INPUT_TRANSCRIPT)
    if "{frames}" not in structure:
        tail.append(_INPUT_FRAMES)
    if tail:
        body += _INPUT_SEP + "\n".join(tail)

    values = {
        "title": meta.title,
        "datetime_hint": meta.datetime_hint,
        "duration_hint": meta.duration_hint,
        "transcript": transcript,
        "frames": frames,
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], body)


_CHUNK_SYSTEM = "あなたは会議の記録を整理するアシスタントです。Markdown の箇条書きのみ出力します。"

_CHUNK_PROMPT = """次の会議の文字起こしの一部です。後で議事録にまとめるための素材として、
話題・発言の要点・数値・決定事項の候補・宿題の候補を、時刻を保ったまま{lang}で箇条書きにしてください。
要約しすぎず、固有名詞・数字・日付・地名・持ち物名はそのまま残してください。
文字起こしに無い人名・日付・数値を補わないでください。推測は書かず、書かれていることだけを拾います。

--- 文字起こし（部分） ---
{chunk}
"""


# --- "Auto" mode: automatically generate the minutes structure from the meeting content ----------------
_DEFAULT_STRUCTURE_SYSTEM = """あなたは議事録のフォーマット設計の専門家です。
渡された会議の内容（全文または要約）から、その会議に合った議事録の「型」（見出し構成）
だけを{lang}で設計します。実際の議事録本文は書きません。

守ること:
- 出力は Markdown の見出しと、その下に置く「何を書くか」の指示文（丸括弧）だけ。
  実際の会議内容・発言・数値・結論は書かない。
- タイトル行とメタ情報には {title} {datetime_hint} {duration_hint} の3つを、実際の値で
  埋めず文字列のまま入れる。
- {transcript} や {frames} は書かない（入力セクションは後で自動的に足される）。
- 見出しは会議の主題に合わせる。決定事項・次アクションに相当する見出しは必ず入れる。
- 各見出しの指示文に「文字起こし・資料に明示的に出てくることだけ書く」「推測で
  人名・日付・数値・期限を補わない」という趣旨を含める。
- 前置き・後書き・自己言及・コードブロック囲みは書かない。"""

# Only {material} is substituted in (not via .format — a single str.replace is
# used instead, so as not to break literals like {title} elsewhere in the
# prompt body).
_STRUCTURE_PROMPT = """次の会議の内容（全文またはその要約）を踏まえて、この会議に最も適した
議事録の「型」（見出し構成）だけを作ってください。実際の議事録は書かないでください。

厳守:
- 出力は Markdown の見出しと、各見出しの指示文（丸括弧）だけ。会議の実内容は書かない。
- タイトル行・メタ情報に {title} {datetime_hint} {duration_hint} の3つを、実際の値で
  埋めず文字列のまま必ず入れる。
- {transcript} {frames} は書かない。
- 見出しはこの会議の主題に合わせる（例: 団体旅行の説明会なら「スケジュール」「持ち物」
  「集合場所・時間」「注意事項」など）。決定事項・次アクションに当たる見出しは必ず入れる。
- 各見出しの指示文に「文字起こし・資料に明示的に出てくることだけ書く／推測で補わない」
  趣旨を含める。
- 前置き・後書き・自己言及は書かない。

--- 会議の内容（全文または要約） ---
{material}
"""

# Required placeholders that an auto-generated structure must satisfy (all must
# be present as literals). If even one is missing, treat it as "a non-reusable
# structure with real values mixed in" and fall back to the built-in one.
_REQUIRED_PLACEHOLDERS = ("{title}", "{datetime_hint}", "{duration_hint}")

_STRUCTURE_FILENAME = "structure_used.txt"
# Working subfolder for intermediate artifacts (the auto-generated structure,
# chunk summaries). Placed under work/ so the name doesn't collide with
# minutes.md (the deliverable) directly under out_dir. pipeline.py eagerly
# mkdirs it. Both display and filesystem joins derive from this.
_WORK_DIR = "work"


def _structure_system_prompt(minutes_language: str = DEFAULT_MINUTES_LANGUAGE) -> str:
    try:
        text = load_prompt("structure_ja.txt").strip()
        text = text or _DEFAULT_STRUCTURE_SYSTEM
    except FileNotFoundError:
        text = _DEFAULT_STRUCTURE_SYSTEM
    return apply_minutes_language(text, minutes_language)


def _structure_material_budget(ctx: int, response_tokens: int) -> int:
    """The token budget available for the "material" (full text or concatenated
    chunk summaries) in a structure-generation request.

    Assumes ctx > 0. What's left after subtracting the system prompt + user
    prompt skeleton + response reservation + margin. Can go negative or zero
    (with an extremely small ctx). The response reservation, response_tokens,
    uses the same minutes_max_tokens as the minutes body (this prevents a
    reasoning model from burning it all on "thinking" and returning an empty
    response — sharing the same cap accepted in PR #19).
    """
    skeleton = _approx_tokens(_structure_system_prompt()) + _approx_tokens(_STRUCTURE_PROMPT)
    return ctx - skeleton - response_tokens - _PROMPT_MARGIN_TOKENS


def _struct_fits_one_pass(
    text: str, ctx: int, trigger_chars: int, response_tokens: int
) -> bool:
    """Whether the full meeting transcript can be used as-is as input to
    structure generation (whether it fits the light budget).

    If it doesn't fit, the caller uses the existing chunk summaries as material
    instead (no new full-text-read path is added). When ctx is unknown, judged
    by the same character-count threshold as one-shot minutes generation.
    """
    if ctx <= 0:
        return len(text) <= trigger_chars
    return _approx_tokens(text) <= _structure_material_budget(ctx, response_tokens)


def _fit_structure_material(material: str, ctx: int, response_tokens: int) -> str:
    """Truncate the tail if the structure-generation material exceeds the budget
    (pass through unchanged if ctx is unknown).

    For a very long meeting, material — all chunk summaries concatenated — can
    still be too large, overflowing the structure-generation request itself.
    Checked against the same budget as the full-text path
    (_struct_fits_one_pass), dropping the tail past what fits (the beginning
    through the middle is enough to design the headings).
    """
    if ctx <= 0:
        return material
    budget = _structure_material_budget(ctx, response_tokens)
    if budget <= 0 or _approx_tokens(material) <= budget:
        return material
    keep = max(0, int(budget / _TOKENS_PER_CHAR) - 40)
    return material[:keep].rstrip() + "\n…（以降はコンテキスト長の都合で省略）"


def _fit_merged_transcript(
    merged: str,
    system: str,
    structure: str,
    frames_text: str,
    *,
    ctx: int,
    minutes_max_tokens: int,
) -> tuple[str, bool]:
    """Truncate merged_transcript so the merge step's real prompt fits within ctx.

    _structure_fits_minutes_skeleton() is only a minimal check that assumes
    transcript/frames are zero, so for a meeting with many chunks, even if it
    passes the "the structure alone fits" check, adding the real
    merged_transcript (all partials concatenated) + frames can still overflow
    the merge request. Here the budget is taken against the real token count,
    dropping the tail past what fits. Returns (the merged_transcript that fits,
    whether it was truncated). Passed through unchanged if ctx is unknown.
    """
    if ctx <= 0:
        return merged, False
    budget = ctx - (
        _approx_tokens(system)
        + _approx_tokens(structure)
        + _approx_tokens(_MINUTES_INPUT)
        + _approx_tokens(frames_text)
        + minutes_max_tokens
        + _PROMPT_MARGIN_TOKENS
    )
    if _approx_tokens(merged) <= budget:
        return merged, False
    keep = max(0, int(budget / _TOKENS_PER_CHAR) - 40)
    return merged[:keep].rstrip() + "\n…（以降はコンテキスト長の都合で省略）", True


def _structure_fits_minutes_skeleton(
    minutes_system: str, structure: str, ctx: int, minutes_max_tokens: int
) -> bool:
    """Whether the generated structure alone doesn't eat up the merge step's minimum budget.

    Assuming both transcript and frames are zero, checks whether system +
    structure + input-section skeleton + response reservation + margin fit
    within ctx. If it doesn't fit, that means "switching to split generation
    can't save it either" — the structure itself is too large (treated as an
    invalid structure, the same as a missing placeholder). When ctx is unknown
    (<=0), no judgment is made (returns True, deferring to the existing
    character-count-threshold path).
    """
    if ctx <= 0:
        return True
    minimum = (
        _approx_tokens(minutes_system)
        + _approx_tokens(structure)
        + _approx_tokens(_MINUTES_INPUT)
        + minutes_max_tokens
        + _PROMPT_MARGIN_TOKENS
    )
    return minimum <= ctx


def _generate_structure(
    client: LLMClient,
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
    """Build the minutes structure from the meeting content (full text or
    summary) with a single chat call.

    Pass the same minutes_max_tokens for max_tokens as the minutes body (so
    reasoning-model resilience matches the main generation). Returns None on
    failure (an exception, an empty response, a missing required placeholder,
    or the structure itself being too large to fit the merge step). The caller
    falls back to the built-in template when this returns None.
    """
    check_cancel(cancel_event)
    if on_progress:
        on_progress(
            0, 1,
            t("pmsg.struct_generating", language, model=model),
        )
    try:
        out = client.chat(
            system=_structure_system_prompt(minutes_language),
            user=_STRUCTURE_PROMPT.replace("{material}", material),
            max_tokens=max_tokens,
        ).strip()
    except Exception as exc:
        if on_progress:
            on_progress(0, 1, t("pmsg.struct_gen_failed", language, exc=exc))
        return None
    missing = [ph for ph in _REQUIRED_PLACEHOLDERS if ph not in out]
    if not out or missing:
        if on_progress:
            reason = (
                t("pmsg.struct_reason_empty", language)
                if not out
                else t("pmsg.struct_reason_missing", language,
                       names=" ".join(missing))
            )
            on_progress(0, 1, t("pmsg.struct_invalid", language, reason=reason))
        return None
    if not _structure_fits_minutes_skeleton(minutes_system, out, ctx, max_tokens):
        if on_progress:
            on_progress(0, 1, t("pmsg.struct_too_big", language))
        return None
    return out


def _resolve_auto_structure(
    client: LLMClient,
    material: str,
    fallback_structure: str,
    out_dir: str | Path | None,
    *,
    model: str,
    max_tokens: int,
    minutes_system: str,
    ctx: int,
    on_progress: ProgressFn | None,
    cancel_event: threading.Event | None,
    language: str = DEFAULT_LANGUAGE,
    minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
) -> str:
    """Auto-generate the structure, saving it to out_dir and returning it on
    success. Returns fallback on failure.

    When falling back to the built-in template on failure, if a previous run's
    work/structure_used.txt is still sitting in the same out_dir, it would be
    mistaken for "the structure used this time" (and could get copied into
    templates/ by mistake under the "copy it there if you like it" workflow).
    Delete it.
    """
    generated = _generate_structure(
        client, material, model=model, max_tokens=max_tokens,
        minutes_system=minutes_system, ctx=ctx,
        on_progress=on_progress, cancel_event=cancel_event, language=language,
        minutes_language=minutes_language,
    )
    struct_relpath = f"{_WORK_DIR}/{_STRUCTURE_FILENAME}"  # for display (/ separated)
    if generated is None:
        if out_dir is not None:
            try:
                dst = Path(out_dir) / _WORK_DIR / _STRUCTURE_FILENAME
                dst.unlink(missing_ok=True)
            except OSError as exc:
                if on_progress:
                    on_progress(
                        0, 1,
                        t("pmsg.struct_rm_failed", language,
                          name=struct_relpath, exc=exc),
                    )
        return fallback_structure
    if out_dir is not None:
        try:
            dst = Path(out_dir) / _WORK_DIR / _STRUCTURE_FILENAME
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(generated + "\n", encoding="utf-8")
        except OSError as exc:
            if on_progress:
                on_progress(
                    0, 1,
                    t("pmsg.struct_save_failed", language,
                      name=struct_relpath, exc=exc),
                )
    if on_progress:
        on_progress(
            0, 1,
            t("pmsg.struct_saved", language, name=struct_relpath),
        )
    return generated


@dataclass
class MinutesMeta:
    title: str
    datetime_hint: str = "（記載なし）"
    duration_hint: str = "（不明）"


# The format version of work/minutes_partials.json. Carries a signature of the chunk boundaries.
_PARTIALS_FORMAT = 2


def _partials_path(out_dir: Path) -> Path:
    return out_dir / _WORK_DIR / "minutes_partials.json"


def _load_partials(
    out_dir: Path,
    *,
    size_chars: int,
    num_segments: int,
    minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
) -> list[str]:
    """Read back chunk summaries that got partway through before an interruption
    or timeout (empty if there are none).

    Whether they can be reused is judged by whether the chunking conditions
    used when the summaries were made (`chunk_size_chars` and the total number
    of segments in the split input) match this time. If they don't match, the
    chunk boundaries would be shifted, causing duplicated or missing content, so
    they aren't adopted (redone from scratch instead). The old format with no
    metadata (a JSON array) is also not adopted, since its boundaries can't be verified.

    Also discarded if the language the summaries were written in doesn't
    match minutes_language for this run — otherwise a run resumed with a
    different minutes_language would splice old-language partials together
    with newly-generated ones, producing minutes with mixed languages.
    Partials saved before this field existed have no "minutes_language" key;
    they're treated as having been written in the default ("日本語"), same
    as the pre-feature hardcoded behavior.

    An entry that has only a heading line ("### 部分 N") with an empty body is
    treated as a sign the LLM returned an empty response (e.g. a reasoning
    model used up max_tokens on thinking alone) and is invalidated. Nothing
    from that entry onward is trusted; summarization is redone starting there.
    """
    path = _partials_path(out_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    # Old format (array) / unknown format: redo from scratch
    if not isinstance(data, dict) or data.get("format") != _PARTIALS_FORMAT:
        return []
    # The chunking conditions have changed (e.g. chunk_size_chars was lowered to work around Context Length)
    if data.get("chunk_size_chars") != size_chars or data.get("num_segments") != num_segments:
        return []
    # minutes_language changed since these partials were saved
    default_lang_name = minutes_language_name(DEFAULT_MINUTES_LANGUAGE)
    if data.get("minutes_language", default_lang_name) != minutes_language_name(
        minutes_language
    ):
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
    minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
) -> None:
    """Called each time one chunk summary finishes, rewriting everything up to that point.

    Saves the chunking conditions (`chunk_size_chars` and the total number of
    segments in the split input) alongside, so consistency can be verified on
    resume. Also saves the (normalized) language the summaries were written
    in, so _load_partials can tell whether it still matches minutes_language
    on a later run.
    """
    _partials_path(out_dir).parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": _PARTIALS_FORMAT,
        "chunk_size_chars": size_chars,
        "num_segments": num_segments,
        "num_chunks": num_chunks,
        "minutes_language": minutes_language_name(minutes_language),
        "partials": partials,
    }
    _partials_path(out_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _system_prompt(minutes_language: str = DEFAULT_MINUTES_LANGUAGE) -> str:
    try:
        text = load_prompt("minutes_ja.txt").strip()
        text = text or _DEFAULT_SYSTEM
    except FileNotFoundError:
        text = _DEFAULT_SYSTEM
    return apply_minutes_language(text, minutes_language)


def _summarize_chunks(
    chunks: list[list[Segment]],
    client: LLMClient,
    llm_config: AIConfig,
    *,
    partials: list[str],
    out_path: Path | None,
    size_chars: int,
    num_segments: int,
    max_tokens: int,
    total_steps: int,
    on_progress: ProgressFn | None,
    cancel_event: threading.Event | None,
    language: str = DEFAULT_LANGUAGE,
    minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
) -> list[str]:
    """Summarize the unprocessed chunks in order, returning partials filled in.

    Skips anything already present in partials (reused on resume, or run ahead
    of time as material for structure generation in Auto mode). Does nothing
    if everything is already done. Each time one finishes, rewrites
    work/minutes_partials.json if out_path is given. Pass the same value for
    max_tokens as the caller's budget calculation (fit_chunk_size).
    """
    for i in range(len(partials) + 1, len(chunks) + 1):
        check_cancel(cancel_event)
        if on_progress:
            on_progress(
                i - 1, total_steps,
                t("pmsg.chunk_wait", language,
                  i=i, n=len(chunks), model=llm_config.llm_model),
            )
        chunk_text = transcript_to_text(chunks[i - 1])
        t0 = time.monotonic()
        summary = client.chat(
            system=_CHUNK_SYSTEM,
            user=_CHUNK_PROMPT.format(
                chunk=chunk_text, lang=minutes_language_name(minutes_language)
            ),
            max_tokens=max_tokens,
        )
        elapsed = format_elapsed(time.monotonic() - t0, language)
        partials.append(f"### 部分 {i}\n{summary.strip()}")
        if out_path is not None:
            _save_partials(
                partials, out_path,
                size_chars=size_chars,
                num_segments=num_segments,
                num_chunks=len(chunks),
                minutes_language=minutes_language,
            )
        if on_progress:
            on_progress(
                i, total_steps,
                t("pmsg.chunk_done", language, i=i, n=len(chunks), elapsed=elapsed),
            )
    return partials


def _split_segments(segments: list[Segment], size_chars: int) -> list[list[Segment]]:
    chunks: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_len = 0
    for seg in segments:
        seg_len = len(seg.text) + 12  # margin for the timestamp
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
    llm_config: AIConfig,
    meta: MinutesMeta,
    *,
    on_progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
    out_dir: str | Path | None = None,
    reuse: bool = True,
    context_tokens: int | None = None,
    template_path: str | Path | None = None,
    auto_structure: bool = False,
    language: str = DEFAULT_LANGUAGE,
    minutes_language: str = DEFAULT_MINUTES_LANGUAGE,
) -> str:
    """Return the minutes as a Markdown string.

    language: the language of the progress/warning messages passed to
        on_progress ("ja" / "en", default "en"). Does not affect the minutes
        body or system prompt (that's on the LLM side, separately).
    minutes_language: the language the minutes body itself is written in —
        and, in Auto mode, its auto-generated heading structure, and the
        intermediate per-chunk summaries used for long transcripts. Default
        "ja" matches pre-existing hardcoded behavior. Free-form, not
        validated. Distinct from `language` above, which only controls
        progress/warning message text.

    cancel_event: if set, interrupts between chunk summaries (for long
        transcripts). A short path (a single chat call) can't respond to it
        mid-call.
    out_dir: if given, writes out `work/minutes_partials.json` each time one
        chunk summary of a long transcript finishes. On a re-run after a
        timeout or interruption, if reuse=True, resumes from here and doesn't
        re-summarize chunks that already finished.
    reuse: if False, ignores any partial summaries left in out_dir and starts over from scratch.
    context_tokens: the real context length (in tokens) of the loaded model, if
        known. If the one-shot generation prompt is estimated not to fit
        within it, falls back to split generation regardless of the
        character-count threshold. If None, judged by chunk_trigger_chars
        (characters) instead.
    template_path: path to a custom template that replaces the minutes
        "structure." Empty/None means the built-in template. If it doesn't
        exist, can't be read, or is empty, falls back to the built-in template
        with a warning. The system prompt (the rules against fabrication,
        etc.) is always applied regardless of the template. The same template
        is used for both the one-shot path and the merge step of split generation.
    auto_structure: if True, Auto mode. Has the LLM generate the minutes
        structure (heading layout) from the meeting content just once, and
        uses that as the template (taking priority over template_path). Mindful
        of the token budget: if the full text fits the light budget, it's used
        as-is; if not, the existing chunk summaries are used as material
        instead (no new full-text-read path is added). The generated structure
        is saved to out_dir/work/structure_used.txt. If generation fails (an
        exception, an empty response, or a missing required placeholder), falls
        back to the built-in template with a warning.
    """
    check_cancel(cancel_event)
    system = _system_prompt(minutes_language)
    full_transcript = transcript_to_text(segments)
    frames_text_raw = notes_to_text(notes) or "（フレームなし）"

    structure = load_minutes_structure(
        template_path,
        on_warning=(
            (lambda m: on_progress(0, 1, t("pmsg.warn_prefix", language, msg=m)))
            if on_progress
            else None
        ),
        language=language,
    )

    trigger_chars = getattr(llm_config, "chunk_trigger_chars", None) or _CHUNK_TRIGGER_CHARS
    size_chars = getattr(llm_config, "chunk_size_chars", None) or _CHUNK_SIZE_CHARS

    # If the manual setting (config.toml's [ai] context_tokens) is non-zero,
    # prefer it; only use the auto-detected value (the context_tokens argument
    # passed by the pipeline) when it's 0.
    ctx = int(getattr(llm_config, "context_tokens", 0) or 0) or int(context_tokens or 0)
    # Upper bound on response tokens for the minutes body / chunk summaries.
    # Use the same value in the budget calculation and the real request.
    minutes_max_tokens = min(
        int(getattr(llm_config, "max_tokens", _MINUTES_RESPONSE_TOKENS)),
        _MINUTES_RESPONSE_TOKENS,
    )

    def fit_chunk_size(frames_text_now: str, *, warn: bool) -> None:
        """Also narrow size_chars so an individual chunk's prompt doesn't overflow."""
        nonlocal size_chars
        if ctx <= 0:
            return
        safe = ctx - minutes_max_tokens - _PROMPT_MARGIN_TOKENS - _approx_tokens(frames_text_now)
        if safe > 500:
            size_chars = min(size_chars, int(safe / _TOKENS_PER_CHAR))
        elif warn and on_progress:
            # Truncating frames leaves almost no room to shrink the chunk further.
            # Proceed anyway (the real chat call will return a 400 plus a cause
            # hint), but warn first.
            on_progress(0, 1, t("pmsg.ctx_too_small", language, ctx=ctx))

    # First, estimate the budget with the current structure (built-in or a
    # file). If Auto mode swaps in a generated structure, recalculate this
    # against the resulting structure (below).
    frames_text, one_pass = _minutes_budget(
        system, structure, full_transcript, frames_text_raw,
        ctx=ctx, minutes_max_tokens=minutes_max_tokens, trigger_chars=trigger_chars,
    )
    fit_chunk_size(frames_text, warn=True)

    # --- State for the long-transcript case (chunk splitting, partial
    # summaries). Set up once, only when needed.
    chunks: list[list[Segment]] = []
    total_steps = 0
    out_path: Path | None = None
    partials: list[str] = []
    _long_ready = False

    def ensure_long_state() -> None:
        nonlocal chunks, total_steps, out_path, partials, _long_ready
        if _long_ready:
            return
        _long_ready = True
        chunks = _split_segments(segments, size_chars)
        total_steps = len(chunks) + 1
        out_path = Path(out_dir) if out_dir is not None else None
        if reuse and out_path is not None:
            existing = _load_partials(
                out_path,
                size_chars=size_chars,
                num_segments=len(segments),
                minutes_language=minutes_language,
            )
            if 0 < len(existing) <= len(chunks):
                partials = existing
                if on_progress:
                    on_progress(
                        len(partials), total_steps,
                        t("pmsg.partials_reuse", language,
                          n=len(partials), m=len(chunks)),
                    )
            elif on_progress and _partials_path(out_path).is_file():
                on_progress(
                    0, total_steps, t("pmsg.partials_stale", language)
                )

    def summarize() -> list[str]:
        # Use the same max_tokens in the budget calculation (fit_chunk_size) and
        # the real request. Capped at _MINUTES_RESPONSE_TOKENS (5000) is plenty
        # for a summary and also matches the budget.
        nonlocal partials
        partials = _summarize_chunks(
            chunks, client, llm_config,
            partials=partials, out_path=out_path,
            size_chars=size_chars, num_segments=len(segments),
            max_tokens=minutes_max_tokens,
            total_steps=total_steps, on_progress=on_progress,
            cancel_event=cancel_event, language=language,
            minutes_language=minutes_language,
        )
        return partials

    if auto_structure:
        # If the full text fits the light budget for structure generation, use
        # it as-is; if not, use the existing chunk summaries as material
        # instead (no path that re-reads the raw full text is added). Either
        # way, the tail past what fits the structure-generation request's
        # budget is dropped.
        if _struct_fits_one_pass(full_transcript, ctx, trigger_chars, minutes_max_tokens):
            material = full_transcript
            pre_summarized = False
        else:
            ensure_long_state()
            material = _fit_structure_material(
                "\n\n".join(summarize()), ctx, minutes_max_tokens
            )
            pre_summarized = True
        # The fallback is always "built-in" (matching the spec and warning text
        # — even when a file was specified). The response reservation is
        # minutes_max_tokens, same as the minutes body (matches reasoning-model
        # resilience). Also falls back to built-in when the generated structure
        # is too large to fit the merge step (switching to split generation
        # can't save it).
        structure = _resolve_auto_structure(
            client, material, _MINUTES_STRUCTURE, out_dir,
            model=llm_config.llm_model, max_tokens=minutes_max_tokens,
            minutes_system=system, ctx=ctx,
            on_progress=on_progress, cancel_event=cancel_event, language=language,
            minutes_language=minutes_language,
        )
        # After structure generation (a heavy LLM call), check for a
        # cancellation before moving on to minutes generation.
        check_cancel(cancel_event)
        # Recalculate the budget against the resulting structure's size (so
        # that swapping the structure doesn't push the final request over the
        # context length). If already chunked, leave the chunking settings as-is.
        frames_text, one_pass = _minutes_budget(
            system, structure, full_transcript, frames_text_raw,
            ctx=ctx, minutes_max_tokens=minutes_max_tokens, trigger_chars=trigger_chars,
        )
        if pre_summarized:
            one_pass = False
        else:
            fit_chunk_size(frames_text, warn=False)

    if one_pass:
        if on_progress:
            on_progress(
                0, 1,
                t("pmsg.minutes_generating", language, model=llm_config.llm_model),
            )
        user = _fill_minutes_template(
            structure, meta, full_transcript, frames_text,
            minutes_language=minutes_language,
        )
        t0 = time.monotonic()
        md = client.chat(system, user, max_tokens=minutes_max_tokens)
        elapsed = format_elapsed(time.monotonic() - t0, language)
        if on_progress:
            on_progress(1, 1, t("pmsg.minutes_generated", language, elapsed=elapsed))
        md = md.strip() + "\n"
        _warn_leaked_instructions(structure, md, on_progress, language)
        return md

    # --- The long-transcript case: chunk summarization -> merge ---
    if on_progress and ctx > 0 and not _long_ready:
        on_progress(0, 1, t("pmsg.switch_to_split", language, ctx=ctx))
    ensure_long_state()
    summarize()

    if on_progress:
        on_progress(
            len(chunks), total_steps,
            t("pmsg.merging", language, model=llm_config.llm_model),
        )
    merged_transcript, merged_truncated = _fit_merged_transcript(
        "\n\n".join(partials), system, structure, frames_text,
        ctx=ctx, minutes_max_tokens=minutes_max_tokens,
    )
    if merged_truncated and on_progress:
        on_progress(0, 1, t("pmsg.merge_truncated", language))
    user = _fill_minutes_template(
        structure, meta, merged_transcript, frames_text,
        minutes_language=minutes_language,
    )
    t0 = time.monotonic()
    md = client.chat(system, user, max_tokens=minutes_max_tokens)
    elapsed = format_elapsed(time.monotonic() - t0, language)
    if on_progress:
        on_progress(
            total_steps, total_steps,
            t("pmsg.minutes_generated", language, elapsed=elapsed),
        )
    md = md.strip() + "\n"
    _warn_leaked_instructions(structure, md, on_progress, language)
    return md


def save_minutes(markdown: str, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "minutes.md"
    path.write_text(markdown, encoding="utf-8")
    return path
