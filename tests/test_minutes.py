"""Tests for the minutes module (stubs the LLM rather than calling it)."""

from __future__ import annotations

import json
import threading

import pytest

from meeting_minutes.model.cancel import PipelineCancelled
from meeting_minutes.model.config import AIConfig
from meeting_minutes.model.minutes import (
    _DEFAULT_SYSTEM,
    _MINUTES_RESPONSE_TOKENS,
    _MINUTES_STRUCTURE,
    MinutesMeta,
    _fill_minutes_template,
    _leaked_instructions,
    _save_partials,
    _split_segments,
    generate_minutes,
    load_minutes_structure,
)
from meeting_minutes.model.transcribe import Segment
from meeting_minutes.model.vision import FrameNote


class FakeClient:
    """A stub compatible with LLMClient.chat. Records its calls."""

    def __init__(self, reply: str = "# 議事録\n\n本文"):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return self.reply


def _segments(n: int, text: str = "発言") -> list[Segment]:
    return [Segment(start=i * 3.0, end=i * 3.0 + 3.0, text=f"{text}{i}") for i in range(n)]


@pytest.fixture
def chunking_config() -> AIConfig:
    """An AIConfig with a lowered threshold, to reliably exercise the chunk-summarization (long meeting) path.

    In normal use, one-shot generation is preferred whenever it fits the
    context, so the default threshold is large. In tests, chunk_trigger_chars
    / chunk_size_chars are lowered to verify the split-generation path.
    """
    return AIConfig(chunk_trigger_chars=2000, chunk_size_chars=1000)


def test_approx_tokens_ratio_matches_measured_qwen_japanese():
    """_approx_tokens estimates on the safe side at 0.8, based on measured
    behavior (the Qwen2.5 tokenizer sees ~0.5-0.8 tok/char for Japanese)."""
    from meeting_minutes.model.minutes import _approx_tokens

    assert _approx_tokens("あ" * 100) == 81
    assert _approx_tokens("") == 1


def test_split_segments_respects_size():
    segs = _segments(50, text="あ" * 100)  # ~100 chars per segment
    chunks = _split_segments(segs, size_chars=1000)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 50
    # order is preserved
    flat = [s for c in chunks for s in c]
    assert flat == segs


def test_generate_minutes_short_path_single_call():
    client = FakeClient(reply="# 議事録: テスト\n\n## 決定事項\n- なし\n")
    notes = [FrameNote(timestamp=12.0, path="frames/frame_0001.jpg", description="表題スライド")]
    meta = MinutesMeta(title="テスト会議", duration_hint="約 5 分")

    md = generate_minutes(
        _segments(5),
        notes,
        client,
        AIConfig(),
        meta,
    )

    assert md.startswith("# 議事録: テスト")
    assert md.endswith("\n")
    assert len(client.calls) == 1
    # both the transcript and the frame description are in the template
    user = client.calls[0]["user"]
    assert "テスト会議" in user
    assert "発言0" in user
    assert "表題スライド" in user


def test_generate_minutes_short_path_messages_translated_when_language_en():
    client = FakeClient(reply="# Minutes\n\n## Decisions\n- none\n")
    progress: list[tuple] = []
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="Meeting"),
        on_progress=lambda c, t, m: progress.append((c, t, m)),
        language="en",
    )
    joined = "\n".join(m for _c, _t, m in progress)
    assert "Generating the minutes" in joined
    assert "Minutes generated" in joined
    assert "議事録を生成" not in joined
    assert "応答を待っています" not in joined


def test_generate_minutes_default_minutes_language_unchanged():
    """Omitting minutes_language (or passing "ja" explicitly) produces the
    exact same system prompt as before this feature existed."""
    a = FakeClient(reply="x")
    b = FakeClient(reply="x")
    generate_minutes(_segments(3), [], a, AIConfig(), MinutesMeta(title="会議"))
    generate_minutes(
        _segments(3), [], b, AIConfig(), MinutesMeta(title="会議"),
        minutes_language="ja",
    )
    assert a.calls[0]["system"] == b.calls[0]["system"]
    assert "{lang}" not in a.calls[0]["system"]  # placeholder always resolved


def test_generate_minutes_minutes_language_reaches_system_prompt():
    client = FakeClient(reply="x")
    generate_minutes(
        _segments(3), [], client, AIConfig(), MinutesMeta(title="会議"),
        minutes_language="English",
    )
    system = client.calls[0]["system"]
    assert "English" in system
    assert "日本語で正確な議事録を作成します" not in system


def test_chunk_prompt_reflects_minutes_language(chunking_config):
    """Per-chunk summaries (used for long transcripts) are written directly
    in minutes_language, avoiding a ja-then-target round-trip."""
    client = RoutingFakeClient(chunk="- summary bullet")
    long_segs = _segments(40, text="議題について長い発言をする" * 5)
    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="mtg"),
        minutes_language="English",
    )
    chunk_call = client.chunk_calls()[0]
    assert "Englishで箇条書き" in chunk_call["user"]


def test_routing_fake_client_markers_survive_minutes_language(chunking_config):
    """Regression guard: the literal Japanese marker substrings
    RoutingFakeClient keys off must survive minutes_language substitution
    unchanged."""
    client = RoutingFakeClient(structure=_GEN_STRUCTURE, chunk="- x")
    long_segs = _segments(40, text="議題について長い発言をする" * 5)
    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="mtg"),
        auto_structure=True, minutes_language="English",
    )
    assert client.struct_calls()
    assert client.chunk_calls()


def test_structure_system_prompt_reflects_minutes_language(tmp_path):
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    generate_minutes(
        _segments(3), [], client, AIConfig(), MinutesMeta(title="会議"),
        auto_structure=True, out_dir=tmp_path, minutes_language="Korean",
    )
    struct_call = client.struct_calls()[0]
    assert "Korean" in struct_call["system"]


def test_generate_minutes_single_pass_under_char_fallback_threshold():
    """When context_tokens is unknown, judged by the character-count
    threshold. Under the default 20000 characters is one-shot generation."""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(180, text="議題について長い発言をする" * 3)  # under 10,000 characters < 20000
    generate_minutes(segs, [], client, AIConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) == 1
    assert client.calls[0]["user"].startswith("以下のテンプレートに沿って")


def test_generate_minutes_char_fallback_chunks_over_threshold():
    """When context_tokens is unknown and it exceeds 20000 characters, it splits."""
    client = FakeClient(reply="要約")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # over 25,000 characters > 20000
    generate_minutes(segs, [], client, AIConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) >= 2


def test_generate_minutes_context_tokens_allow_single_pass():
    """If the real context length is known and there's headroom, one-shot
    generation happens even with a higher character count."""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    # a length that would split under the character threshold
    segs = _segments(320, text="議題について長い発言をする" * 5)
    generate_minutes(
        segs, [], client, AIConfig(), MinutesMeta(title="会議"),
        context_tokens=32768,
    )
    assert len(client.calls) == 1


def test_generate_minutes_context_tokens_force_chunk_when_prompt_would_overflow():
    """Issue #18: even with a low character count, if it's estimated to exceed
    the real context length once frames are included, falls back to split generation."""
    client = FakeClient(reply="要約")
    segs = _segments(80, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの文字。" * 60)
        for i in range(40)
    ]
    generate_minutes(
        segs, notes, client, AIConfig(), MinutesMeta(title="会議"),
        context_tokens=4096,
    )
    assert len(client.calls) >= 2


def test_generate_minutes_frames_text_is_truncated_to_budget():
    """A huge frames_text is truncated at the token cap, so it doesn't eat up the whole prompt."""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(20, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの詳細な文字起こし。" * 80)
        for i in range(50)
    ]
    generate_minutes(
        segs, notes, client, AIConfig(), MinutesMeta(title="会議"),
        context_tokens=32768,
    )
    assert len(client.calls) == 1
    user = client.calls[0]["user"]
    assert "コンテキスト長の都合でここまで" in user  # the truncation marker


def test_generate_minutes_chunk_trigger_chars_from_config_controls_path():
    """Lowering [ai] chunk_trigger_chars puts even a short transcript on the split-generation path."""
    # a few hundred characters; with the default 40000 this would be one-shot
    segs = _segments(20, text="短い発言")
    single = FakeClient(reply="要約")
    generate_minutes(segs, [], single, AIConfig(), MinutesMeta(title="会議"))
    assert len(single.calls) == 1

    split = FakeClient(reply="要約")
    generate_minutes(
        segs, [], split,
        AIConfig(chunk_trigger_chars=50, chunk_size_chars=30),
        MinutesMeta(title="会議"),
    )
    assert len(split.calls) >= 2  # chunk summaries (multiple) + merge


def test_generate_minutes_long_path_maps_then_reduces(chunking_config):
    # make a transcript long enough that chunk summarization kicks in
    client = FakeClient(reply="部分要約 or 最終議事録")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")

    progress: list[tuple] = []
    md = generate_minutes(
        long_segs,
        [],
        client,
        chunking_config,
        meta,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
    )

    assert md.endswith("\n")
    # called 2+ times: chunk summaries (multiple) + the final merge (1)
    assert len(client.calls) >= 2
    assert progress  # progress was reported


def test_generate_minutes_long_path_messages_translated_when_language_en(chunking_config):
    client = FakeClient(reply="partial or final")
    long_segs = _segments(300, text="a long remark about the agenda " * 5)
    progress: list[tuple] = []
    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="Long meeting"),
        on_progress=lambda c, t, m: progress.append((c, t, m)),
        language="en",
    )
    joined = "\n".join(m for _c, _t, m in progress)
    assert "Partial summary" in joined
    assert "部分要約" not in joined
    assert "所要" not in joined


def test_generate_minutes_cancel_stops_chunk_loop(chunking_config):
    client = FakeClient(reply="部分要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()  # simulate the Stop button being pressed after chunk 1

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            long_segs,
            [],
            client,
            chunking_config,
            meta,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    # does not get as far as the merge (the final chat call)
    assert client.calls[-1]["user"].startswith("次の会議の文字起こしの一部です")


def test_generate_minutes_cancel_before_start_raises_immediately():
    client = FakeClient()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            _segments(3), [], client, AIConfig(), MinutesMeta(title="会議"),
            cancel_event=cancel_event,
        )
    assert client.calls == []


# --- Persisting and resuming partial summaries -----------------------------------------

def _save_partials_matching(
    tmp_path, entries, chunking_config, long_segs, *, minutes_language="ja"
):
    """Write a partial-summaries file with a signature that matches long_segs / chunking_config."""
    _save_partials(
        entries,
        tmp_path,
        size_chars=chunking_config.chunk_size_chars,
        num_segments=len(long_segs),
        num_chunks=len(_split_segments(long_segs, chunking_config.chunk_size_chars)),
        minutes_language=minutes_language,
    )


def test_generate_minutes_persists_partials_on_cancel(tmp_path, chunking_config):
    client = FakeClient(reply="部分要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            long_segs, [], client, chunking_config, meta,
            on_progress=on_progress, cancel_event=cancel_event, out_dir=tmp_path,
        )

    saved = json.loads((tmp_path / "work" / "minutes_partials.json").read_text(encoding="utf-8"))
    assert saved["format"] == 2
    assert saved["chunk_size_chars"] == chunking_config.chunk_size_chars
    assert saved["num_segments"] == len(long_segs)
    assert len(saved["partials"]) == 1
    assert saved["partials"][0].startswith("### 部分 1")


def test_generate_minutes_resumes_from_saved_partials(tmp_path, chunking_config):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n既存の要約"], chunking_config, long_segs)
    client = FakeClient(reply="最終議事録")
    meta = MinutesMeta(title="長い会議")

    md = generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=True
    )

    assert md.strip() == "最終議事録"
    # the first chunk was not re-summarized
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert all("既存の要約" not in c["user"] for c in chunk_calls)
    # the merge (final) call includes the reused partial summary
    assert "既存の要約" in client.calls[-1]["user"]


def test_generate_minutes_fresh_ignores_saved_partials(tmp_path, chunking_config):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n既存の要約"], chunking_config, long_segs)
    client = FakeClient(reply="要約")
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=False
    )

    # reuse=False, so existing partial summaries aren't used; re-summarizes from scratch
    assert "既存の要約" not in client.calls[-1]["user"]


def test_generate_minutes_ignores_empty_bodied_partials(tmp_path, chunking_config):
    """A partial summary with only a heading and an empty body (a sign the
    LLM returned an empty response) is treated as invalid."""
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n"], chunking_config, long_segs)  # no body
    client = FakeClient(reply="要約")
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=True
    )

    # the empty part 1 is redone (a chunk call happens)
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert len(chunk_calls) >= 1


def test_generate_minutes_discards_partials_when_chunk_size_changed(tmp_path):
    """Resuming after changing chunk_size_chars doesn't use partial summaries from the old chunk boundaries.

    Guards against the docs/models_ja.md "lower chunk_size_chars if you can't
    raise the Context Length" procedure (interrupt -> change config -> resume)
    silently duplicating or dropping content.
    """
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials(
        ["### 部分 1\n古い境界の要約"], tmp_path,
        size_chars=1000, num_segments=len(long_segs), num_chunks=5,
    )
    client = FakeClient(reply="新しい要約")
    cfg = AIConfig(chunk_trigger_chars=500, chunk_size_chars=400)  # change the size

    generate_minutes(
        long_segs, [], client, cfg, MinutesMeta(title="会議"),
        out_dir=tmp_path, reuse=True,
    )

    # the old summary is discarded and doesn't appear in the final merge either
    assert "古い境界の要約" not in client.calls[-1]["user"]


def test_generate_minutes_discards_legacy_list_format_partials(tmp_path, chunking_config):
    """The old format (a JSON array) with no metadata can't have its boundaries verified, so it isn't used."""
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "minutes_partials.json").write_text(
        json.dumps(["### 部分 1\n旧形式の要約"]), encoding="utf-8"
    )
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    client = FakeClient(reply="要約")

    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="会議"),
        out_dir=tmp_path, reuse=True,
    )

    assert "旧形式の要約" not in client.calls[-1]["user"]


def test_generate_minutes_discards_partials_when_minutes_language_changed(
    tmp_path, chunking_config
):
    """Resuming with a different minutes_language must not splice old-language
    partial summaries together with newly-generated ones — otherwise the
    final minutes could mix languages."""
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(
        tmp_path, ["### 部分 1\n既存の日本語要約"], chunking_config, long_segs,
        minutes_language="ja",
    )
    client = FakeClient(reply="new summary")

    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="mtg"),
        out_dir=tmp_path, reuse=True, minutes_language="English",
    )

    # the ja-language partial is discarded, not reused alongside English ones
    assert "既存の日本語要約" not in client.calls[-1]["user"]


def test_generate_minutes_reuses_partials_when_minutes_language_matches(
    tmp_path, chunking_config
):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(
        tmp_path, ["### 部分 1\nexisting summary"], chunking_config, long_segs,
        minutes_language="English",
    )
    client = FakeClient(reply="final")

    md = generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="mtg"),
        out_dir=tmp_path, reuse=True, minutes_language="English",
    )

    assert md.strip() == "final"
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert all("existing summary" not in c["user"] for c in chunk_calls)
    assert "existing summary" in client.calls[-1]["user"]


def test_generate_minutes_pre_feature_partials_treated_as_ja(tmp_path, chunking_config):
    """Partials saved before minutes_language existed have no
    "minutes_language" key in the JSON. They must be treated as implicitly
    "ja" (the pre-feature hardcoded behavior): reused when the current run
    is also "ja" (the default), discarded otherwise."""
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "minutes_partials.json").write_text(
        json.dumps(
            {
                "format": 2,
                "chunk_size_chars": chunking_config.chunk_size_chars,
                "num_segments": len(long_segs),
                "num_chunks": len(
                    _split_segments(long_segs, chunking_config.chunk_size_chars)
                ),
                "partials": ["### 部分 1\n旧仕様の要約"],
                # no "minutes_language" key: simulates a pre-feature file
            }
        ),
        encoding="utf-8",
    )

    # default (ja) run: reused
    client_ja = FakeClient(reply="final")
    generate_minutes(
        long_segs, [], client_ja, chunking_config, MinutesMeta(title="mtg"),
        out_dir=tmp_path, reuse=True,
    )
    assert "旧仕様の要約" in client_ja.calls[-1]["user"]


def test_generate_minutes_pre_feature_partials_discarded_for_non_ja(tmp_path, chunking_config):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "minutes_partials.json").write_text(
        json.dumps(
            {
                "format": 2,
                "chunk_size_chars": chunking_config.chunk_size_chars,
                "num_segments": len(long_segs),
                "num_chunks": len(
                    _split_segments(long_segs, chunking_config.chunk_size_chars)
                ),
                "partials": ["### 部分 1\n旧仕様の要約"],
            }
        ),
        encoding="utf-8",
    )

    client_en = FakeClient(reply="final")
    generate_minutes(
        long_segs, [], client_en, chunking_config, MinutesMeta(title="mtg"),
        out_dir=tmp_path, reuse=True, minutes_language="English",
    )
    assert "旧仕様の要約" not in client_en.calls[-1]["user"]


def test_generate_minutes_chunk_max_tokens_matches_budget_cap(tmp_path):
    """A chunk summary's real max_tokens matches the value used in the
    budget calculation (capped at _MINUTES_RESPONSE_TOKENS).

    If this drifts, the safe_chunk_tokens calculation and the real request
    disagree, and a chunk could exceed the context (a Codex finding).
    """
    from meeting_minutes.model.minutes import _MINUTES_RESPONSE_TOKENS

    client = FakeClient(reply="要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    llm_config = AIConfig(max_tokens=12345, chunk_trigger_chars=2000, chunk_size_chars=1000)

    generate_minutes(long_segs, [], client, llm_config, meta, out_dir=tmp_path)

    calls = client.calls
    chunk_calls = [c for c in calls if c["user"].startswith("次の会議の文字起こしの一部です")]
    assert chunk_calls
    expected = min(12345, _MINUTES_RESPONSE_TOKENS)
    assert all(c["kwargs"]["max_tokens"] == expected for c in chunk_calls)
    # the final merge uses the same cap
    assert calls[-1]["kwargs"]["max_tokens"] == expected


def test_generate_minutes_manual_context_tokens_beats_autodetect():
    """If config's [ai] context_tokens is non-zero, it takes priority over the auto-detected value."""
    client = FakeClient(reply="要約")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # ~25,000 characters
    # set a small manual value (4096). Even if auto-detection returns 200000, manual wins and it splits.
    cfg = AIConfig(context_tokens=4096)
    generate_minutes(
        segs, [], client, cfg, MinutesMeta(title="会議"), context_tokens=200000
    )
    assert len(client.calls) >= 2  # the manual 4096 took effect and it split


def test_generate_minutes_small_context_does_not_force_oversized_frames_budget():
    """Even with a small ctx, the frames budget doesn't force the 6000 floor — it's capped by ctx instead."""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(20, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの文字。" * 40)
        for i in range(30)
    ]
    # with ctx=4096, one-shot generation is impossible so it splits, but frames
    # truncation keeps frames_text itself from exceeding 4096 on its own.
    generate_minutes(
        segs, notes, client, AIConfig(), MinutesMeta(title="会議"),
        context_tokens=4096,
    )
    from meeting_minutes.model.minutes import _approx_tokens
    for c in client.calls:
        # no real request's prompt is eating up ctx from frames alone
        assert _approx_tokens(c["user"]) < 4096 * 3  # rough check: not blowing up


# --- Custom minutes template (Issue #21) --------------------------

def test_load_minutes_structure_builtin_when_empty():
    assert load_minutes_structure("") is _MINUTES_STRUCTURE
    assert load_minutes_structure(None) is _MINUTES_STRUCTURE


def test_load_minutes_structure_reads_custom_file(tmp_path):
    p = tmp_path / "tpl.txt"
    p.write_text("# 顧客様式\n\n## 決定\n## 宿題\n", encoding="utf-8")
    assert load_minutes_structure(str(p)) == "# 顧客様式\n\n## 決定\n## 宿題"


def test_load_minutes_structure_missing_file_falls_back_with_warning(tmp_path):
    warnings: list[str] = []
    got = load_minutes_structure(str(tmp_path / "nope.txt"), on_warning=warnings.append)
    assert got is _MINUTES_STRUCTURE
    assert warnings and "using the built-in one" in warnings[0]


def test_load_minutes_structure_empty_file_falls_back_with_warning(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n", encoding="utf-8")
    warnings: list[str] = []
    assert load_minutes_structure(str(p), on_warning=warnings.append) is _MINUTES_STRUCTURE
    assert warnings and "is empty" in warnings[0]


def test_load_minutes_structure_warning_translated_when_language_en(tmp_path):
    warnings: list[str] = []
    load_minutes_structure(
        str(tmp_path / "nope.txt"), on_warning=warnings.append, language="en"
    )
    assert warnings and "using the built-in one" in warnings[0]
    assert "内蔵テンプレート" not in warnings[0]


def test_fill_minutes_template_appends_input_section_when_missing():
    out = _fill_minutes_template(
        "# 様式\n## 決定事項", MinutesMeta(title="会議X"), "文字起こし本文", "フレーム本文"
    )
    assert "# 様式" in out
    assert "会議X" not in out or "{title}" not in out  # {title} は様式に無いだけ
    assert "文字起こし本文" in out and "フレーム本文" in out
    assert "## 入力: 文字起こし" in out  # 自動で足された


def test_fill_minutes_template_respects_own_placeholders_and_stray_braces():
    tpl = "# {title} 議事録\nJSON例: {\"a\": 1}\n## 本文\n{transcript}\n資料:{frames}"
    out = _fill_minutes_template(tpl, MinutesMeta(title="定例"), "T!", "F!")
    assert out.count("## 入力: 文字起こし") == 0  # {transcript} を持つので入力節は足さない
    assert "# 定例 議事録" in out
    assert 'JSON例: {"a": 1}' in out  # 素の { } は壊れない
    assert "T!" in out and "F!" in out


def test_generate_minutes_uses_custom_template_single_pass(tmp_path):
    tpl = tmp_path / "cust.txt"
    tpl.write_text("# お客様フォーマット\n## 合意事項\n## 次アクション\n", encoding="utf-8")
    client = FakeClient(reply="# 議事録\n本文\n")
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        template_path=str(tpl),
    )
    assert len(client.calls) == 1
    assert "お客様フォーマット" in client.calls[0]["user"]
    assert "## 合意事項" in client.calls[0]["user"]
    # the built-in template's headings don't appear
    assert "## 宿題・アクションアイテム" not in client.calls[0]["user"]


def test_generate_minutes_uses_custom_template_in_merge_step(chunking_config):
    tpl_text = "# 客先様式\n## 決めたこと\n{transcript}\n{frames}\n"
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, tpl_text.encode("utf-8"))
    os.close(fd)
    try:
        client = FakeClient(reply="部分/最終")
        long_segs = _segments(300, text="議題について長い発言をする" * 5)
        generate_minutes(
            long_segs, [], client, chunking_config, MinutesMeta(title="長い会議"),
            template_path=path,
        )
        # the merge (final chat) uses the client's format
        assert "客先様式" in client.calls[-1]["user"]
    finally:
        os.unlink(path)


def test_generate_minutes_custom_template_missing_still_produces_minutes(tmp_path):
    """Even if the template isn't found, don't stop with an error — generate
    with the built-in template and warn."""
    msgs: list[str] = []
    client = FakeClient(reply="# 議事録\n本文\n")
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        template_path=str(tmp_path / "missing.txt"),
        on_progress=lambda c, t, m: msgs.append(m),
    )
    assert len(client.calls) == 1
    assert "## 宿題・アクションアイテム" in client.calls[0]["user"]  # built-in template
    assert any("template" in m and "built-in" in m for m in msgs)


def test_generate_minutes_system_prompt_unchanged_by_custom_template(tmp_path):
    """Changing the template doesn't change the system prompt (the anti-fabrication rules)."""
    tpl = tmp_path / "t.txt"
    tpl.write_text("# 様式\n## 本文\n", encoding="utf-8")
    a = FakeClient(reply="x")
    b = FakeClient(reply="x")
    generate_minutes(_segments(3), [], a, AIConfig(), MinutesMeta(title="会議"))
    generate_minutes(_segments(3), [], b, AIConfig(), MinutesMeta(title="会議"),
                     template_path=str(tpl))
    assert a.calls[0]["system"] == b.calls[0]["system"]


def test_example_template_file_matches_builtin_structure():
    """templates/minutes_template_example.txt is identical to the built-in
    template (it's meant as a starting point)."""
    from meeting_minutes.model.config import REPO_ROOT

    p = REPO_ROOT / "templates" / "minutes_template_example.txt"
    assert load_minutes_structure(str(p)) == _MINUTES_STRUCTURE.strip()


def test_fill_minutes_template_appends_only_missing_frames_section():
    """Even if {transcript} is written but {frames} is forgotten, the frame
    information isn't lost (Codex finding 1)."""
    tpl = "# 様式\n## 本文\n{transcript}\n"  # no {frames}
    out = _fill_minutes_template(tpl, MinutesMeta(title="会議"), "文字起こし本文", "フレーム解析本文")
    assert "文字起こし本文" in out
    assert "フレーム解析本文" in out  # filled in individually
    assert "## 入力: 画面キャプチャの説明" in out
    # transcript is already in the structure, so don't duplicate it
    assert out.count("## 入力: 文字起こし") == 0


def test_fill_minutes_template_no_contamination_from_replaced_values():
    """A substituted value containing another placeholder's string doesn't
    get caught up in it (Codex finding 2)."""
    tpl = "# {title}\n## 本文\n{transcript}\n## 資料\n{frames}\n"
    tricky_transcript = "田中: このスライドの {frames} という表記について質問です"
    out = _fill_minutes_template(
        tpl, MinutesMeta(title="定例"), tricky_transcript, "実際のフレーム解析結果"
    )
    # "{frames}" inside the transcript is left as-is (not overwritten by the frame analysis result)
    assert "{frames} という表記について" in out
    # only the real {frames} placeholder becomes the frame analysis result
    assert "実際のフレーム解析結果" in out
    assert out.count("実際のフレーム解析結果") == 1


def test_fill_minutes_template_leaves_unknown_braces_untouched():
    tpl = "# {title}\n設定例: {timeout: 600}\n{transcript}\n{frames}"
    out = _fill_minutes_template(tpl, MinutesMeta(title="X"), "T", "F")
    assert "設定例: {timeout: 600}" in out  # a { } that isn't a known placeholder name is left unchanged


# --- Auto mode: auto-generate the structure from the meeting content (Issue #34) --------------

class RoutingFakeClient:
    """A stub that switches its reply based on the call content (structure
    generation / chunk summary / minutes body)."""

    def __init__(
        self,
        *,
        structure: str | None = None,
        chunk: str = "- 箇条書きの要点",
        minutes: str = "# 議事録\n本文\n",
        raise_on_structure: bool = False,
    ):
        self.structure = structure
        self.chunk = chunk
        self.minutes = minutes
        self.raise_on_structure = raise_on_structure
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        if "議事録の「型」" in user:
            if self.raise_on_structure:
                raise RuntimeError("LLM 500")
            return self.structure or ""
        if user.startswith("次の会議の文字起こしの一部です"):
            return self.chunk
        return self.minutes

    def struct_calls(self) -> list[dict]:
        return [c for c in self.calls if "議事録の「型」" in c["user"]]

    def chunk_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if c["user"].startswith("次の会議の文字起こしの一部です")
        ]


_GEN_STRUCTURE = (
    "# 議事録: {title}\n\n"
    "- 日時: {datetime_hint}\n"
    "- 記録時間: {duration_hint}\n\n"
    "## スケジュール\n"
    "（旅程を時系列で。文字起こし・資料に明示的に出てくることだけ書く。推測で補わない）\n\n"
    "## 決定事項\n（「〜する」と明言されたものだけ。無ければ「（該当なし）」）\n\n"
    "## 次アクション\n（誰が・いつまでに・何を、と明言された場合のみ）\n"
)


def test_auto_structure_single_pass_uses_full_transcript_and_saves(tmp_path):
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    segs = _segments(5, text="旅行の説明")

    generate_minutes(
        segs, [], client, AIConfig(), MinutesMeta(title="旅行説明会"),
        out_dir=tmp_path, auto_structure=True,
    )

    # just 2 calls: structure generation -> the minutes body
    assert len(client.calls) == 2
    struct_call = client.calls[0]
    assert "旅行の説明0" in struct_call["user"]  # uses the full text as-is for material
    # the response reservation is minutes_max_tokens, same as the minutes
    # body (matches reasoning-model resilience)
    assert struct_call["kwargs"]["max_tokens"] == client.calls[1]["kwargs"]["max_tokens"]
    assert struct_call["kwargs"]["max_tokens"] == min(
        AIConfig().max_tokens, _MINUTES_RESPONSE_TOKENS
    )

    # the generated structure is used in the final minutes prompt; the built-in one is not used
    minutes_user = client.calls[1]["user"]
    assert "## スケジュール" in minutes_user
    assert "## 宿題・アクションアイテム" not in minutes_user

    # the saved structure keeps its placeholders as literals (so it can be dropped into templates/ as-is)
    saved = (tmp_path / "work" / "structure_used.txt").read_text(encoding="utf-8")
    assert "{title}" in saved
    assert "{datetime_hint}" in saved
    assert "{duration_hint}" in saved
    assert "旅行説明会" not in saved  # not filled with the real value
    # whereas the final prompt has it substituted with the real value
    assert "旅行説明会" in minutes_user


def test_auto_structure_large_transcript_reuses_chunk_summaries(tmp_path, chunking_config):
    client = RoutingFakeClient(structure=_GEN_STRUCTURE, chunk="- 部分要点")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    n_chunks = len(_split_segments(long_segs, chunking_config.chunk_size_chars))

    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="長い会議"),
        out_dir=tmp_path, auto_structure=True,
    )

    assert len(client.struct_calls()) == 1
    material = client.struct_calls()[0]["user"]
    # the material is the chunk summaries, not the raw full text (no new full-text-read path is added)
    assert "### 部分 1" in material
    assert "議題について長い発言をする" not in material
    # chunk summarization happens only once (not run twice for structure generation)
    assert len(client.chunk_calls()) == n_chunks
    # the generated structure is in the final merge
    assert "## 次アクション" in client.calls[-1]["user"]
    assert (tmp_path / "work" / "structure_used.txt").is_file()


def test_auto_structure_fallback_on_missing_placeholder(tmp_path):
    # {duration_hint} is missing -> not reusable, so falls back to the built-in template
    bad = "# 議事録: {title}\n- 日時: {datetime_hint}\n## 議論\n（略）\n"
    msgs: list[str] = []
    client = RoutingFakeClient(structure=bad)

    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]  # built-in template
    assert any("missing placeholders" in m and "built-in" in m for m in msgs)
    assert not (tmp_path / "work" / "structure_used.txt").exists()  # 失敗時は保存しない


def test_auto_structure_fallback_on_llm_error(tmp_path):
    msgs: list[str] = []
    client = RoutingFakeClient(raise_on_structure=True)

    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]
    assert any("auto-generating the minutes structure failed" in m for m in msgs)
    assert not (tmp_path / "work" / "structure_used.txt").exists()


def test_auto_structure_prompt_instructs_literal_placeholders():
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        auto_structure=True,
    )
    struct_user = client.calls[0]["user"]
    # substituting in the material doesn't break the prompt's instructions/placeholder examples
    assert "{title}" in struct_user
    # the "don't write this" instruction's literal isn't broken by .replace
    assert "{transcript}" in struct_user
    assert "実際の値で" in struct_user


def test_auto_structure_takes_precedence_over_template_path(tmp_path):
    tpl = tmp_path / "cust.txt"
    tpl.write_text("# 客先様式\n## 合意事項\n", encoding="utf-8")
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)

    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, template_path=str(tpl),
    )

    minutes_user = client.calls[-1]["user"]
    assert "## スケジュール" in minutes_user  # the auto-generated structure
    assert "客先様式" not in minutes_user     # template_path is not used


def test_auto_structure_without_out_dir_still_generates():
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        auto_structure=True,  # no out_dir -> doesn't save, but still generates
    )
    assert "## スケジュール" in client.calls[-1]["user"]


def test_auto_structure_failure_falls_back_to_builtin_not_file_template(tmp_path):
    """Codex finding 2: on auto failure, always fall back to the built-in
    template, not the template_path file."""
    tpl = tmp_path / "cust.txt"
    tpl.write_text("# 客先様式だけ\n## 合意事項\n", encoding="utf-8")
    # missing a required placeholder -> fails
    client = RoutingFakeClient(structure="型らしきもの（プレースホルダー無し）")

    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, template_path=str(tpl),
    )

    minutes_user = client.calls[-1]["user"]
    assert "## 宿題・アクションアイテム" in minutes_user  # the built-in template
    assert "客先様式だけ" not in minutes_user  # does not fall back to the file template
    assert not (tmp_path / "work" / "structure_used.txt").exists()


def test_auto_structure_recomputes_budget_after_generation(tmp_path):
    """Codex finding 1: if the generated structure is large (but not
    rejected), recompute the budget and switch to split generation.

    A structure where system + structure + response reservation + margin fits
    within ctx (so it isn't rejected), but is large enough that adding the
    transcript no longer fits. After generation, re-evaluate one_pass and fall
    through to the chunk-summarization path.
    """
    biggish_structure = (
        "# 議事録: {title}\n- {datetime_hint} / {duration_hint}\n"
        + "## 追加の見出し\n（この見出しに書く内容の説明）\n" * 170
    )
    client = RoutingFakeClient(
        structure=biggish_structure, chunk="- 部分要点", minutes="# 議事録\n本文\n"
    )
    segs = _segments(90, text="そこそこの長さの発言をする" * 4)

    generate_minutes(
        segs, [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, context_tokens=13000,
    )

    # structure generation happens once, is not rejected (it's saved), and
    # gives up on one-shot generation, switching to split generation
    assert len(client.struct_calls()) == 1
    assert (tmp_path / "work" / "structure_used.txt").is_file()
    assert len(client.chunk_calls()) >= 1


def test_auto_structure_rejects_structure_too_large_for_merge(tmp_path):
    """Codex finding: if the generated structure itself is too large to fit
    even after merging, fall back to the built-in template.

    Even switching to split generation doesn't help, since the merge step's
    final request is system + the huge structure + partials + frames — no
    matter how small the partials are, it won't fit. Treated the same as a
    missing placeholder: an invalid structure that falls back to the built-in
    template.
    """
    ctx = 10000
    huge = (
        "# 議事録: {title}\n- {datetime_hint} / {duration_hint}\n"
        + "## 見出し\n（この見出しに書くことの説明を長めに書く）\n" * 400
    )
    msgs: list[str] = []
    client = RoutingFakeClient(structure=huge, chunk="- 要点")
    long_segs = _segments(200, text="議題の発言" * 3)

    generate_minutes(
        long_segs, [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, context_tokens=ctx,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]  # the built-in template
    assert not (tmp_path / "work" / "structure_used.txt").exists()  # rejected, so not saved
    assert any("too large" in m for m in msgs)


def test_auto_structure_no_size_reject_when_ctx_unknown(tmp_path):
    """When ctx is unknown, no structure-size judgment is made (deferred to
    the character-count-threshold path)."""
    big = (
        "# 議事録: {title}\n- {datetime_hint} / {duration_hint}\n"
        + "## 見出し\n（説明）\n" * 400
    )
    client = RoutingFakeClient(structure=big)
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,  # context_tokens not given
    )
    # not rejected for size reasons; the generated structure is used and saved
    assert (tmp_path / "work" / "structure_used.txt").is_file()
    assert "## 見出し" in client.calls[-1]["user"]


def test_auto_structure_truncates_oversized_chunk_summary_material(tmp_path):
    """Codex finding: the material with chunk summaries concatenated is also
    checked and truncated against the structure-generation budget."""
    from meeting_minutes.model.minutes import _approx_tokens

    ctx = 16000
    # concatenated, this exceeds the structure-generation budget
    big_summary = "・とても長い部分要約の行。" * 400
    client = RoutingFakeClient(structure=_GEN_STRUCTURE, chunk=big_summary)
    long_segs = _segments(400, text="議題について長い発言をする" * 5)

    generate_minutes(
        long_segs, [], client, AIConfig(), MinutesMeta(title="長い会議"),
        out_dir=tmp_path, auto_structure=True, context_tokens=ctx,
    )

    assert len(client.struct_calls()) == 1
    struct = client.struct_calls()[0]
    assert "コンテキスト長の都合で省略" in struct["user"]  # the tail was truncated
    # the structure-generation request (system + user + the real response reservation) fits within ctx
    total = (
        _approx_tokens(struct["system"])
        + _approx_tokens(struct["user"])
        + struct["kwargs"]["max_tokens"]
    )
    assert total <= ctx


def test_auto_structure_cancel_after_generation_stops_before_minutes(tmp_path):
    """Codex finding: catches a cancellation right after structure
    generation too, so it doesn't proceed to the heavy minutes generation."""
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if "Auto-generated the minutes structure" in msg:
            cancel_event.set()  # "cancel" right after structure generation finishes

    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    with pytest.raises(PipelineCancelled):
        generate_minutes(
            _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
            out_dir=tmp_path, auto_structure=True,
            on_progress=on_progress, cancel_event=cancel_event,
        )

    assert len(client.struct_calls()) == 1
    assert client.calls == client.struct_calls()  # no call for the minutes body


def test_auto_structure_response_reserve_follows_llm_max_tokens():
    """Codex finding: structure generation's response reservation follows
    minutes_max_tokens rather than being a fixed value.

    Even when max_tokens is lowered for a reasoning (thinking) model, the
    reservation uses the same criterion as the main minutes generation
    (min(llm_config.max_tokens, _MINUTES_RESPONSE_TOKENS)).
    """
    cfg = AIConfig(max_tokens=900)  # small, simulating a reasoning model
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)

    generate_minutes(
        _segments(5), [], client, cfg, MinutesMeta(title="会議"),
        auto_structure=True,
    )

    struct_mt = client.struct_calls()[0]["kwargs"]["max_tokens"]
    minutes_mt = client.calls[-1]["kwargs"]["max_tokens"]
    assert struct_mt == minutes_mt == min(900, _MINUTES_RESPONSE_TOKENS)


def test_generate_minutes_truncates_oversized_merged_transcript(tmp_path):
    """Codex finding: before assembling the merge step's real prompt
    (system+structure+merged_transcript+frames), check the budget against the
    real token count, dropping the tail past what fits."""
    from meeting_minutes.model.minutes import _PROMPT_MARGIN_TOKENS, _approx_tokens

    ctx = 12000
    # make each chunk summary's reply large -> the concatenated merged_transcript exceeds the merge budget
    client = FakeClient(reply="・" + "とても長い部分要約の一行。" * 500)
    long_segs = _segments(400, text="議題の発言" * 3)
    msgs: list[str] = []

    generate_minutes(
        long_segs, [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, context_tokens=ctx,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    merge_user = client.calls[-1]["user"]
    assert "以降はコンテキスト長の都合で省略" in merge_user  # the tail was truncated
    reserve = min(AIConfig().max_tokens, _MINUTES_RESPONSE_TOKENS)
    # the merge request (user + response reservation + margin) fits within ctx
    assert _approx_tokens(merge_user) + reserve + _PROMPT_MARGIN_TOKENS <= ctx
    assert any("tail of the merge input was truncated" in m for m in msgs)


def test_auto_structure_failure_removes_stale_structure_file(tmp_path):
    """Codex finding: when Auto mode fails and falls back to the built-in
    template, delete a leftover structure_used.txt from a previous run (to
    prevent an unrelated structure from being mistakenly copied)."""
    stale = tmp_path / "work" / "structure_used.txt"
    stale.parent.mkdir()
    stale.write_text("# 前回のおまかせ結果（今回とは無関係）\n", encoding="utf-8")

    client = RoutingFakeClient(structure="型らしきもの（プレースホルダー無し）")  # generation fails
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,
    )

    assert not stale.exists()  # doesn't leave behind an old structure that wasn't used this time
    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]  # generated with the built-in template


# --- Guarding against instruction-text leaks in the template (Issue #44) ----------------------------

def _has_instruction_leak_rule(text: str) -> bool:
    return "丸括弧" in text and "書き写" in text and "指示" in text


def test_default_system_forbids_writing_instruction_text():
    """_DEFAULT_SYSTEM has the rule "don't copy the parenthetical instruction text as-is"."""
    assert _has_instruction_leak_rule(_DEFAULT_SYSTEM)


def test_prompt_file_forbids_writing_instruction_text():
    """prompts/minutes_ja.txt has the same rule."""
    from meeting_minutes.model.config import REPO_ROOT

    text = (REPO_ROOT / "prompts" / "minutes_ja.txt").read_text(encoding="utf-8")
    assert _has_instruction_leak_rule(text)


def test_leaked_instructions_detects_verbatim_instruction():
    structure = (
        "# 概要\n（目的・行き先・期間・対象者など、冒頭で述べられた概要。"
        "無ければ「（記載なし）」）\n## 決定事項\n（「〜する」と明言されたものだけ）\n"
    )
    leaked_md = (
        "# 概要\n（目的・行き先・期間・対象者など、冒頭で述べられた概要。"
        "無ければ「（記載なし）」）\n## 決定事項\n- 出発は9時に決定\n"
    )
    leaks = _leaked_instructions(structure, leaked_md)
    assert leaks == ["（目的・行き先・期間・対象者など、冒頭で述べられた概要。無ければ「（記載なし）」）"]


def test_leaked_instructions_ignores_short_bracket_values_and_clean_output():
    """Short boilerplate phrases like "(not applicable)" / "(not stated)", or
    output where the instruction text was replaced with real content, are
    not false-positived."""
    clean_md = (
        "# 議事録: テスト\n## 目的・アジェンダ\n旅行の説明会。行き先は京都、2泊3日。\n"
        "## 決定事項\n（該当なし）\n## 資料（スライド）の内容\n（読み取れる資料なし）\n"
    )
    assert _leaked_instructions(_MINUTES_STRUCTURE, clean_md) == []


def test_generate_minutes_warns_when_template_instruction_leaks(tmp_path):
    """If the model copies the instruction text verbatim, warn via on_progress (a safety net)."""
    instr = "（目的・行き先・期間・対象者など、冒頭で述べられた概要。無ければ「（記載なし）」）"
    tpl = tmp_path / "cust.txt"
    tpl.write_text(f"# 概要\n{instr}\n## 決定事項\n（明言されたものだけ）\n", encoding="utf-8")
    client = FakeClient(reply=f"# 概要\n{instr}\n## 決定事項\n- 出発は9時に決定\n")
    msgs: list[str] = []
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        template_path=str(tpl), on_progress=lambda c, t, m: msgs.append(m),
    )
    assert any("instruction text" in m and "left verbatim" in m for m in msgs)


def test_generate_minutes_no_leak_warning_on_clean_output():
    client = FakeClient(reply="# 議事録: 会議\n## 決定事項\n- 出発は9時\n## 宿題\n（該当なし）\n")
    msgs: list[str] = []
    generate_minutes(
        _segments(5), [], client, AIConfig(), MinutesMeta(title="会議"),
        on_progress=lambda c, t, m: msgs.append(m),
    )
    assert not any("instruction text" in m and "left verbatim" in m for m in msgs)
