"""文字起こし＋フレーム解析から議事録（Markdown）を生成する。

自由文の要約ではなく、決まった型（日時・出席者・議題・決定事項・宿題／担当・期限）
を LLM に埋めさせる。文字起こしが長い場合は「チャンク要約 → 統合」の 2 段で処理する。
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
from .config import AIConfig, load_prompt
from .llm_client import LLMClient
from .transcribe import Segment, transcript_to_text
from .vision import FrameNote, notes_to_text

# 進捗コールバック: (完了ステップ, 総ステップ, メッセージ)
ProgressFn = Callable[[int, int, str], None]

# --- 一発生成 / 分割生成の切り替え --------------------------------------
# 分割すると「要約の要約」から議事録を作ることになり具体性が落ちるため、コンテキスト
# 長に収まる限りは一発生成を優先する。収まるかの判定は、可能なら実コンテキスト長
# （`context_tokens`。pipeline が LM Studio の /api/v0/models から取得）に対する
# 概算トークン数で行い、取れないときだけ下の文字数しきい値にフォールバックする。
#
# Qwen 系トークナイザでの日本語の実測は約 0.74 トークン/文字
# （かな 0.52 / 漢字かな交じり 0.75〜0.80 / 逐語の話し言葉 0.52）。安全側に少し盛る。
_TOKENS_PER_CHAR = 0.8
# system プロンプト＋テンプレ雛形＋不確実性のマージン（トークン）。
_PROMPT_MARGIN_TOKENS = 1500
# 議事録本文（＝最終 chat の出力）に見込む上限トークン。context 予約に使う。
# 型を埋めるタスクなので実運用ではこの範囲に収まる。推論モデル用の大きい
# max_tokens はここでは使わない（docs/models_ja.md のとおり推論モデルは非対象）。
_MINUTES_RESPONSE_TOKENS = 5000
# frames_text（フレーム解析の連結）がプロンプトを食い尽くさないための上限トークン。
# 実コンテキスト長が分かるときは ctx/3 まで許容する。
_FRAMES_TOKEN_BUDGET = 6000

# 実コンテキスト長が取得できない基盤向けのフォールバック（文字数しきい値）。
# 32k コンテキスト前提で frames 上限・応答予約・マージンを引いた残りに収まる
# おおよその文字数。config.toml の [ai] chunk_trigger_chars / chunk_size_chars で
# 上書きできる（Context Length を上げられない環境ではさらに小さくする）。
_CHUNK_TRIGGER_CHARS = 20000
_CHUNK_SIZE_CHARS = 12000


def _approx_tokens(text: str) -> int:
    """文字数からトークン数をざっくり見積もる（Qwen 系日本語の実測に基づく）。"""
    return int(len(text) * _TOKENS_PER_CHAR) + 1


def _truncate_to_token_budget(text: str, budget_tokens: int) -> str:
    """推定トークン数が budget を超えるなら文字単位で切り詰める。"""
    if _approx_tokens(text) <= budget_tokens:
        return text
    keep = max(0, int(budget_tokens / _TOKENS_PER_CHAR) - 40)
    return text[:keep].rstrip() + "\n…（フレーム説明はコンテキスト長の都合でここまで）"

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

# 議事録の「構造」だけ。config.toml の [output] template_path で丸ごと差し替え可能。
# 使えるプレースホルダー: {title} {datetime_hint} {duration_hint}
# （{transcript} / {frames} を書かなければ、末尾に入力セクションが自動で足される）
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

# 入力セクション。カスタムテンプレートが該当プレースホルダーを書いていない場合、
# 「足りない方だけ」を末尾に補う（{transcript} だけ書いて {frames} を忘れても、
# フレーム情報が丸ごと消えないように個別に扱う）。
_INPUT_SEP = "\n---\n\n"
_INPUT_TRANSCRIPT = "## 入力: 文字起こし\n{transcript}\n"
_INPUT_FRAMES = "## 入力: 画面キャプチャの説明（時刻付き）\n{frames}\n"
# トークン見積もり（skeleton_tokens）用: 両方補った最大ケース。
_MINUTES_INPUT = _INPUT_SEP + _INPUT_TRANSCRIPT + "\n" + _INPUT_FRAMES

# テンプレート内のプレースホルダー。ここに載っている名前だけ置換し、素の { } は触らない。
_PLACEHOLDER_RE = re.compile(
    r"\{(title|datetime_hint|duration_hint|transcript|frames)\}"
)

# テンプレートの丸括弧内「指示文」。全角 （ ）で囲まれ、内側に 1 段だけ入れ子
# （例: 「…無ければ「（記載なし）」」）を許す。生成後の議事録にこの文言がそのまま
# 残っていないかの検出に使う（プロンプトでモデルに禁止しているが、小さいモデル向けの保険）。
_INSTRUCTION_RE = re.compile(r"（(?:[^（）]|（[^（）]*）)+）")
# 「（記載なし）」「（該当なし）」など、実際の議事録に現れてよい短い丸括弧語は除外する。
_INSTRUCTION_MIN_CHARS = 12


def _leaked_instructions(structure: str, minutes_md: str) -> list[str]:
    """structure（使用テンプレート）の丸括弧指示文のうち、生成議事録にそのまま
    残っているものを返す。短い定型語（（記載なし）等）は対象外。"""
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
    """(予算内に切り詰めた frames_text, 一発生成できるか) を返す。

    テンプレート（structure）のサイズに依存するので、「おまかせ」モードで構造を
    差し替えたら、生成後の構造でこれを呼び直す必要がある（構造の入れ替えで
    最終リクエストが実コンテキスト長を超える PR #19 と同種の問題を防ぐ）。
    """
    skeleton = (
        _approx_tokens(system) + _approx_tokens(structure) + _approx_tokens(_MINUTES_INPUT)
    )
    # frames_text がプロンプトを食い尽くさないよう上限を設ける。ctx が分かるときは
    # 「1/3」を狙いつつ、応答予約・マージン・雛形を引いた残りを超えないようにする
    # （小さい ctx で下限 6000 を無理に確保して溢れるのを防ぐ）。
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
    """カスタム議事録テンプレート（構造のみ）を読む。

    template_path が空／None なら内蔵テンプレート。指定があっても、存在しない・
    読めない・空の場合はエラーで止めず内蔵にフォールバックし、on_warning で通知する。
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
    structure: str, meta: MinutesMeta, transcript: str, frames: str
) -> str:
    """テンプレート（構造）にメタ情報・入力を差し込んで完成プロンプトを返す。

    - 逐次 .replace ではなく、テンプレート文字列を1回だけ走査する一括置換
      （re.sub + コールバック）。置換後の値（transcript 等）は再走査しないので、
      文字起こし中に偶然 "{frames}" のような文字列があっても巻き込まれない。
      素の { }（JSON 例など）は _PLACEHOLDER_RE に載っていないので触らない。
    - 構造が {transcript} / {frames} を書いていない場合、「足りない方だけ」を末尾に補う。
    """
    body = _MINUTES_PREAMBLE + structure
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


_CHUNK_PROMPT = """次の会議の文字起こしの一部です。後で議事録にまとめるための素材として、
話題・発言の要点・数値・決定事項の候補・宿題の候補を、時刻を保ったまま日本語で箇条書きにしてください。
要約しすぎず、固有名詞・数字・日付・地名・持ち物名はそのまま残してください。
文字起こしに無い人名・日付・数値を補わないでください。推測は書かず、書かれていることだけを拾います。

--- 文字起こし（部分） ---
{chunk}
"""


# --- 「おまかせ」モード: 議事録の型を会議内容から自動生成する ----------------
_DEFAULT_STRUCTURE_SYSTEM = """あなたは議事録のフォーマット設計の専門家です。
渡された会議の内容（全文または要約）から、その会議に合った議事録の「型」（見出し構成）
だけを日本語で設計します。実際の議事録本文は書きません。

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

# {material} だけを差し込む（.format は使わない。プロンプト本文中の {title} などの
# リテラルを壊さないため str.replace で 1 箇所だけ置換する）。
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

# 自動生成された型が満たすべき必須プレースホルダー（すべてリテラルで含まれること）。
# 1つでも欠けたら「実値が混じった使い回せない型」とみなし内蔵にフォールバックする。
_REQUIRED_PLACEHOLDERS = ("{title}", "{datetime_hint}", "{duration_hint}")

_STRUCTURE_FILENAME = "structure_used.txt"
# 中間生成物（自動生成した型・チャンク要約）の作業用サブフォルダ。out_dir 直下の
# minutes.md（成果物）と名前がぶつからないよう work/ に置く。pipeline.py が eager
# mkdir する。表示・FS join ともここから derive する。
_WORK_DIR = "work"


def _structure_system_prompt() -> str:
    try:
        text = load_prompt("structure_ja.txt").strip()
        return text or _DEFAULT_STRUCTURE_SYSTEM
    except FileNotFoundError:
        return _DEFAULT_STRUCTURE_SYSTEM


def _structure_material_budget(ctx: int, response_tokens: int) -> int:
    """構造生成リクエストで「材料」（全文 or チャンク要約連結）に使えるトークン予算。

    ctx > 0 前提。system プロンプト＋ユーザープロンプト雛形＋応答予約＋マージンを
    引いた残り。負や 0 になり得る（極端に小さい ctx）。応答予約 response_tokens は
    議事録本文と同じ minutes_max_tokens を使う（推論モデルが"思考"で使い切って空応答に
    なるのを防ぐ。PR #19 で受け入れた上限をここでも共有する）。
    """
    skeleton = _approx_tokens(_structure_system_prompt()) + _approx_tokens(_STRUCTURE_PROMPT)
    return ctx - skeleton - response_tokens - _PROMPT_MARGIN_TOKENS


def _struct_fits_one_pass(
    text: str, ctx: int, trigger_chars: int, response_tokens: int
) -> bool:
    """会議全文をそのまま「型」生成の入力に使えるか（軽い予算に収まるか）。

    収まらなければ呼び出し側は既存のチャンク要約を材料にする（新たな全文読み込み
    パスを増やさない）。ctx 不明時は議事録一発生成と同じ文字数しきい値で判断する。
    """
    if ctx <= 0:
        return len(text) <= trigger_chars
    return _approx_tokens(text) <= _structure_material_budget(ctx, response_tokens)


def _fit_structure_material(material: str, ctx: int, response_tokens: int) -> str:
    """構造生成の材料が予算を超えるなら末尾を切り詰める（ctx 不明なら素通し）。

    チャンク要約を全部連結した material は、非常に長い会議だとそれでも大きすぎて
    構造生成リクエスト自体が溢れ得る。全文パス（_struct_fits_one_pass）と同じ予算に
    対してチェックし、超える分は末尾を落とす（見出し設計には冒頭〜中盤で足りる）。
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
    """統合ステップの実プロンプトが ctx に収まるよう merged_transcript を切り詰める。

    _structure_fits_minutes_skeleton() は transcript/frames をゼロと仮定した最低限の
    チェックなので、チャンク数が多い会議だと「構造単体は収まる」判定を通っても、
    実際の merged_transcript（全 partials 連結）＋frames を足すと統合リクエストが
    溢れることがある。ここで実際のトークン数で予算を取り、超える分は末尾を落とす。
    戻り値は (収まる merged_transcript, 切り詰めたか)。ctx 不明時は素通し。
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
    """生成された構造だけで統合ステップの最低限の予算を食い潰さないか。

    transcript も frames もゼロと仮定して、system＋構造＋入力節の雛形＋応答予約＋
    マージンが ctx に収まるか。収まらなければ「分割生成に切り替えても救えない」＝
    構造自体が大きすぎる（プレースホルダー欠落と同様、不正な構造として扱う）。
    ctx 不明（<=0）のときは判定しない（True。既存の文字数しきい値の経路に任せる）。
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
) -> str | None:
    """会議内容（全文または要約）から議事録の型を 1 回の chat で作る。

    max_tokens は議事録本文と同じ minutes_max_tokens を渡すこと（推論モデル耐性を
    メイン生成と揃える）。失敗（例外・空応答・必須プレースホルダー欠落・構造自体が
    大きすぎて統合ステップに収まらない）なら None を返す。呼び出し側は None のとき
    内蔵テンプレートにフォールバックする。
    """
    check_cancel(cancel_event)
    if on_progress:
        on_progress(
            0, 1,
            t("pmsg.struct_generating", language, model=model),
        )
    try:
        out = client.chat(
            system=_structure_system_prompt(),
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
) -> str:
    """型を自動生成し、成功したら out_dir に保存して返す。失敗時は fallback を返す。

    失敗して内蔵にフォールバックする場合、同じ out_dir に前回実行時の
    work/structure_used.txt が残っていると「今回使った型」と誤認される（気に入ったら
    templates/ にコピーする運用で無関係な型をコピーしてしまう）。消しておく。
    """
    generated = _generate_structure(
        client, material, model=model, max_tokens=max_tokens,
        minutes_system=minutes_system, ctx=ctx,
        on_progress=on_progress, cancel_event=cancel_event, language=language,
    )
    struct_relpath = f"{_WORK_DIR}/{_STRUCTURE_FILENAME}"  # 表示用（/ 区切り）
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


# work/minutes_partials.json のフォーマット版。チャンク境界のシグネチャを持つ。
_PARTIALS_FORMAT = 2


def _partials_path(out_dir: Path) -> Path:
    return out_dir / _WORK_DIR / "minutes_partials.json"


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
    _partials_path(out_dir).parent.mkdir(parents=True, exist_ok=True)
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
) -> list[str]:
    """未処理のチャンクを順に要約し、埋めた partials を返す。

    partials に既に入っている分（再開時の再利用、あるいは「おまかせ」モードで
    型生成の材料として先に走らせた分）はスキップする。すべて済んでいれば何もしない。
    1 つ終えるたびに out_path があれば work/minutes_partials.json を書き直す。
    max_tokens は呼び出し側の予算計算（fit_chunk_size）と同じ値を渡すこと。
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
            system="あなたは会議の記録を整理するアシスタントです。Markdown の箇条書きのみ出力します。",
            user=_CHUNK_PROMPT.format(chunk=chunk_text),
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
) -> str:
    """議事録の Markdown 文字列を返す。

    language: on_progress へ渡す進捗・警告メッセージの言語（"ja" / "en"、既定 "ja"）。
        議事録本文・システムプロンプトは対象外（別途 LLM 側）。

    cancel_event: セットされていれば、チャンク要約の合間（長い文字起こしの場合）で
        中断する。短いパス（1 回の chat 呼び出し）は呼び出し中に反応できない。
    out_dir: 指定すると、長い文字起こしのチャンク要約を1つ終えるたびに
        `work/minutes_partials.json` として書き出す。タイムアウトや中断のあとの
        再実行では、reuse=True ならここから再開し、終わっているチャンクを
        summarize し直さない。
    reuse: False なら out_dir に部分要約が残っていても無視して最初から。
    context_tokens: 分かっていれば、ロード中モデルの実コンテキスト長（トークン）。
        一発生成のプロンプトがこれに収まらないと推定される場合は、文字数しきい値に
        関わらず分割生成にフォールバックする。None なら chunk_trigger_chars（文字）で判断。
    template_path: 議事録の「構造」を差し替えるカスタムテンプレートのパス。空／None は
        内蔵テンプレート。存在しない・読めない・空の場合は内蔵にフォールバックし警告する。
        システムプロンプト（捏造しない等のルール）はテンプレートに関わらず常に適用する。
        一発生成・分割生成の統合ステップの両方で同じテンプレートを使う。
    auto_structure: True なら「おまかせ」モード。会議内容から議事録の型（見出し構成）を
        LLM に 1 回だけ生成させ、それをテンプレートとして使う（template_path より優先）。
        トークン予算に注意し、全文が軽い予算に収まればそのまま、収まらなければ既存の
        チャンク要約を材料にする（新たな全文読み込みパスは増やさない）。生成した型は
        out_dir/work/structure_used.txt に保存する。生成に失敗（例外・空・必須プレースホルダー
        欠落）したら内蔵テンプレートにフォールバックし警告する。
    """
    check_cancel(cancel_event)
    system = _system_prompt()
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

    # 手動設定（config.toml の [ai] context_tokens）が 0 でなければそれを優先し、
    # 0 のときだけ自動検出値（pipeline が渡す context_tokens 引数）を使う。
    ctx = int(getattr(llm_config, "context_tokens", 0) or 0) or int(context_tokens or 0)
    # 議事録本文・チャンク要約の応答トークン上限。予算計算と実リクエストで同じ値を使う。
    minutes_max_tokens = min(
        int(getattr(llm_config, "max_tokens", _MINUTES_RESPONSE_TOKENS)),
        _MINUTES_RESPONSE_TOKENS,
    )

    def fit_chunk_size(frames_text_now: str, *, warn: bool) -> None:
        """個別チャンクのプロンプトも溢れないよう size_chars を絞る。"""
        nonlocal size_chars
        if ctx <= 0:
            return
        safe = ctx - minutes_max_tokens - _PROMPT_MARGIN_TOKENS - _approx_tokens(frames_text_now)
        if safe > 500:
            size_chars = min(size_chars, int(safe / _TOKENS_PER_CHAR))
        elif warn and on_progress:
            # frames を切り詰めてもチャンクを小さくできる余地がほぼ無い。
            # そのまま進めるが（実 chat は 400 + 原因ヒントを返す）、先に警告する。
            on_progress(0, 1, t("pmsg.ctx_too_small", language, ctx=ctx))

    # まず現時点の構造（内蔵 or ファイル）で予算を見積もる。「おまかせ」で構造を
    # 差し替えたら、生成後の構造でこれを計算し直す（下記）。
    frames_text, one_pass = _minutes_budget(
        system, structure, full_transcript, frames_text_raw,
        ctx=ctx, minutes_max_tokens=minutes_max_tokens, trigger_chars=trigger_chars,
    )
    fit_chunk_size(frames_text, warn=True)

    # --- 長い場合の状態（チャンク分割・部分要約）。必要になった時点で一度だけ用意する。
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
                out_path, size_chars=size_chars, num_segments=len(segments)
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
        # 予算計算（fit_chunk_size）と実リクエストで同じ max_tokens を使う。
        # _MINUTES_RESPONSE_TOKENS(5000) 頭打ちなら要約には十分で、予算とも一致。
        nonlocal partials
        partials = _summarize_chunks(
            chunks, client, llm_config,
            partials=partials, out_path=out_path,
            size_chars=size_chars, num_segments=len(segments),
            max_tokens=minutes_max_tokens,
            total_steps=total_steps, on_progress=on_progress,
            cancel_event=cancel_event, language=language,
        )
        return partials

    if auto_structure:
        # 全文が型生成の軽い予算に収まればそのまま、収まらなければ既存のチャンク要約を
        # 材料にする（生の全文を再度読ませるパスは増やさない）。どちらの材料も
        # 構造生成リクエストの予算に収まるよう、超える分は末尾を落とす。
        if _struct_fits_one_pass(full_transcript, ctx, trigger_chars, minutes_max_tokens):
            material = full_transcript
            pre_summarized = False
        else:
            ensure_long_state()
            material = _fit_structure_material(
                "\n\n".join(summarize()), ctx, minutes_max_tokens
            )
            pre_summarized = True
        # フォールバック先は必ず「内蔵」（仕様・警告文と一致させる。ファイル指定時も内蔵）。
        # 応答予約は議事録本文と同じ minutes_max_tokens（推論モデル耐性を揃える）。
        # 生成構造が大きすぎて統合ステップに収まらない場合も内蔵にフォールバックする
        # （分割生成への切り替えでは救えないため）。
        structure = _resolve_auto_structure(
            client, material, _MINUTES_STRUCTURE, out_dir,
            model=llm_config.llm_model, max_tokens=minutes_max_tokens,
            minutes_system=system, ctx=ctx,
            on_progress=on_progress, cancel_event=cancel_event, language=language,
        )
        # 構造生成（重い LLM 呼び出し）の後、次の議事録生成に進む前に中断を拾う。
        check_cancel(cancel_event)
        # 生成後の構造サイズで予算を計算し直す（構造の入れ替えで最終リクエストが
        # コンテキスト長を超えないように）。既にチャンク分割済みなら分割設定は据え置く。
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
        user = _fill_minutes_template(structure, meta, full_transcript, frames_text)
        t0 = time.monotonic()
        md = client.chat(system, user, max_tokens=minutes_max_tokens)
        elapsed = format_elapsed(time.monotonic() - t0, language)
        if on_progress:
            on_progress(1, 1, t("pmsg.minutes_generated", language, elapsed=elapsed))
        md = md.strip() + "\n"
        _warn_leaked_instructions(structure, md, on_progress, language)
        return md

    # --- 長い場合: チャンク要約 -> 統合 ---
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
    user = _fill_minutes_template(structure, meta, merged_transcript, frames_text)
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
