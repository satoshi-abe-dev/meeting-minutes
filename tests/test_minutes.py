"""minutes モジュールのテスト（LLM は呼ばずにスタブ）。"""

from __future__ import annotations

import json
import threading

import pytest

from meeting_minutes.cancel import PipelineCancelled
from meeting_minutes.config import LLMConfig
from meeting_minutes.minutes import (
    MinutesMeta,
    _save_partials,
    _split_segments,
    generate_minutes,
)
from meeting_minutes.transcribe import Segment
from meeting_minutes.vision import FrameNote


class FakeClient:
    """LLMClient.chat 互換のスタブ。呼び出しを記録する。"""

    def __init__(self, reply: str = "# 議事録\n\n本文"):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return self.reply


def _segments(n: int, text: str = "発言") -> list[Segment]:
    return [Segment(start=i * 3.0, end=i * 3.0 + 3.0, text=f"{text}{i}") for i in range(n)]


@pytest.fixture
def chunking_config() -> LLMConfig:
    """分割要約（長い会議）の経路を必ず通すための、しきい値を下げた LLMConfig。

    通常運用ではコンテキストに収まる限り一発生成を優先するため既定のしきい値は大きい。
    テストでは chunk_trigger_chars / chunk_size_chars を小さくして分割経路を検証する。
    """
    return LLMConfig(chunk_trigger_chars=2000, chunk_size_chars=1000)


def test_approx_tokens_ratio_matches_measured_qwen_japanese():
    """_approx_tokens は実測（Qwen2.5 tokenizer で日本語 ~0.5〜0.8 tok/字）に基づき
    安全側 0.8 で見積もる。"""
    from meeting_minutes.minutes import _approx_tokens

    assert _approx_tokens("あ" * 100) == 81
    assert _approx_tokens("") == 1


def test_split_segments_respects_size():
    segs = _segments(50, text="あ" * 100)  # 1 セグメント約 100 文字
    chunks = _split_segments(segs, size_chars=1000)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 50
    # 順序が保たれている
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
        LLMConfig(),
        meta,
    )

    assert md.startswith("# 議事録: テスト")
    assert md.endswith("\n")
    assert len(client.calls) == 1
    # テンプレートに文字起こしとフレーム説明が両方入っている
    user = client.calls[0]["user"]
    assert "テスト会議" in user
    assert "発言0" in user
    assert "表題スライド" in user


def test_generate_minutes_single_pass_under_char_fallback_threshold():
    """context_tokens 不明時は文字数しきい値で判断。既定 20000 字未満は一発生成。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(180, text="議題について長い発言をする" * 3)  # 1万字弱 < 20000
    generate_minutes(segs, [], client, LLMConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) == 1
    assert client.calls[0]["user"].startswith("以下のテンプレートに沿って")


def test_generate_minutes_char_fallback_chunks_over_threshold():
    """context_tokens 不明で 20000 字を超えると分割する。"""
    client = FakeClient(reply="要約")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # 2.5万字超 > 20000
    generate_minutes(segs, [], client, LLMConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) >= 2


def test_generate_minutes_context_tokens_allow_single_pass():
    """実コンテキスト長が分かっていて余裕があれば、文字数が多めでも一発生成。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # char 閾値なら分割される長さ
    generate_minutes(
        segs, [], client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=32768,
    )
    assert len(client.calls) == 1


def test_generate_minutes_context_tokens_force_chunk_when_prompt_would_overflow():
    """Issue #18: 文字数は少なくても、frames 込みで実コンテキスト長を超えると
    推定される場合は分割生成にフォールバックする。"""
    client = FakeClient(reply="要約")
    segs = _segments(80, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの文字。" * 60)
        for i in range(40)
    ]
    generate_minutes(
        segs, notes, client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=4096,
    )
    assert len(client.calls) >= 2


def test_generate_minutes_frames_text_is_truncated_to_budget():
    """巨大な frames_text は上限トークンで切り詰められ、プロンプトを食い尽くさない。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(20, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの詳細な文字起こし。" * 80)
        for i in range(50)
    ]
    generate_minutes(
        segs, notes, client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=32768,
    )
    assert len(client.calls) == 1
    user = client.calls[0]["user"]
    assert "コンテキスト長の都合でここまで" in user  # 切り詰めマーカー


def test_generate_minutes_chunk_trigger_chars_from_config_controls_path():
    """[llm] chunk_trigger_chars を下げると、短い文字起こしでも分割経路になる。"""
    segs = _segments(20, text="短い発言")  # 数百文字。既定 40000 なら一発生成
    single = FakeClient(reply="要約")
    generate_minutes(segs, [], single, LLMConfig(), MinutesMeta(title="会議"))
    assert len(single.calls) == 1

    split = FakeClient(reply="要約")
    generate_minutes(
        segs, [], split,
        LLMConfig(chunk_trigger_chars=50, chunk_size_chars=30),
        MinutesMeta(title="会議"),
    )
    assert len(split.calls) >= 2  # チャンク要約(複数) + 統合


def test_generate_minutes_long_path_maps_then_reduces(chunking_config):
    # チャンク要約が走るよう十分長い文字起こしを作る
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
    # チャンク要約(複数) + 最終統合(1) で 2 回以上呼ばれる
    assert len(client.calls) >= 2
    assert progress  # 進捗が通知されている


def test_generate_minutes_cancel_stops_chunk_loop(chunking_config):
    client = FakeClient(reply="部分要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()  # 1 チャンク目の後に中断ボタンが押された想定

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

    # 統合（最終 chat 呼び出し）までは進まない
    assert client.calls[-1]["user"].startswith("次の会議の文字起こしの一部です")


def test_generate_minutes_cancel_before_start_raises_immediately():
    client = FakeClient()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            _segments(3), [], client, LLMConfig(), MinutesMeta(title="会議"),
            cancel_event=cancel_event,
        )
    assert client.calls == []


# --- 部分要約の永続化と再開 -----------------------------------------

def _save_partials_matching(tmp_path, entries, chunking_config, long_segs):
    """long_segs / chunking_config と整合するシグネチャで部分要約ファイルを書く。"""
    _save_partials(
        entries,
        tmp_path,
        size_chars=chunking_config.chunk_size_chars,
        num_segments=len(long_segs),
        num_chunks=len(_split_segments(long_segs, chunking_config.chunk_size_chars)),
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

    saved = json.loads((tmp_path / "minutes_partials.json").read_text(encoding="utf-8"))
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
    # 1つ目のチャンクは要約し直していない
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert all("既存の要約" not in c["user"] for c in chunk_calls)
    # 統合（最終）呼び出しには再利用した部分要約が含まれる
    assert "既存の要約" in client.calls[-1]["user"]


def test_generate_minutes_fresh_ignores_saved_partials(tmp_path, chunking_config):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n既存の要約"], chunking_config, long_segs)
    client = FakeClient(reply="要約")
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=False
    )

    # reuse=False なので既存の部分要約は使われず、最初から要約し直す
    assert "既存の要約" not in client.calls[-1]["user"]


def test_generate_minutes_ignores_empty_bodied_partials(tmp_path, chunking_config):
    """見出しだけで本文が空の部分要約（LLMが空応答を返した形跡）は無効として扱う。"""
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n"], chunking_config, long_segs)  # 本文なし
    client = FakeClient(reply="要約")
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=True
    )

    # 空だった部分1はやり直されている（チャンク呼び出しが発生している）
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert len(chunk_calls) >= 1


def test_generate_minutes_discards_partials_when_chunk_size_changed(tmp_path):
    """chunk_size_chars を変えて再開すると、旧チャンク境界の部分要約は使わない。

    doc/models.md の「Context Length を上げられないなら chunk_size_chars を下げる」
    手順（中断→config変更→再開）で、内容がサイレントに重複・欠落しないことを保証する。
    """
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials(
        ["### 部分 1\n古い境界の要約"], tmp_path,
        size_chars=1000, num_segments=len(long_segs), num_chunks=5,
    )
    client = FakeClient(reply="新しい要約")
    cfg = LLMConfig(chunk_trigger_chars=500, chunk_size_chars=400)  # size を変更

    generate_minutes(
        long_segs, [], client, cfg, MinutesMeta(title="会議"),
        out_dir=tmp_path, reuse=True,
    )

    # 旧要約は捨てられ、最終統合にも現れない
    assert "古い境界の要約" not in client.calls[-1]["user"]


def test_generate_minutes_discards_legacy_list_format_partials(tmp_path, chunking_config):
    """メタ情報の無い旧形式（JSON 配列）の部分要約は境界を検証できないので使わない。"""
    (tmp_path / "minutes_partials.json").write_text(
        json.dumps(["### 部分 1\n旧形式の要約"]), encoding="utf-8"
    )
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    client = FakeClient(reply="要約")

    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="会議"),
        out_dir=tmp_path, reuse=True,
    )

    assert "旧形式の要約" not in client.calls[-1]["user"]


def test_generate_minutes_chunk_max_tokens_matches_budget_cap(tmp_path):
    """チャンク要約の実 max_tokens は予算計算と同じ値（_MINUTES_RESPONSE_TOKENS 頭打ち）。

    ここがズレると safe_chunk_tokens の計算と実リクエストが食い違い、チャンクが
    コンテキストを超え得る（Codex 指摘）。
    """
    from meeting_minutes.minutes import _MINUTES_RESPONSE_TOKENS

    client = FakeClient(reply="要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    llm_config = LLMConfig(max_tokens=12345, chunk_trigger_chars=2000, chunk_size_chars=1000)

    generate_minutes(long_segs, [], client, llm_config, meta, out_dir=tmp_path)

    calls = client.calls
    chunk_calls = [c for c in calls if c["user"].startswith("次の会議の文字起こしの一部です")]
    assert chunk_calls
    expected = min(12345, _MINUTES_RESPONSE_TOKENS)
    assert all(c["kwargs"]["max_tokens"] == expected for c in chunk_calls)
    # 最終統合も同じ上限
    assert calls[-1]["kwargs"]["max_tokens"] == expected


def test_generate_minutes_manual_context_tokens_beats_autodetect():
    """config の [llm] context_tokens が 0 でなければ、自動検出値より優先される。"""
    client = FakeClient(reply="要約")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # 約2.5万字
    # 手動 4096（小さい）を設定。自動検出で 200000 が来ても手動が勝ち、分割になる。
    cfg = LLMConfig(context_tokens=4096)
    generate_minutes(
        segs, [], client, cfg, MinutesMeta(title="会議"), context_tokens=200000
    )
    assert len(client.calls) >= 2  # 手動 4096 が効いて分割された


def test_generate_minutes_small_context_does_not_force_oversized_frames_budget():
    """ctx が小さくても frames 予算は下限 6000 を無理に確保せず ctx で頭打ちする。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(20, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの文字。" * 40)
        for i in range(30)
    ]
    # ctx=4096 では一発生成は無理なので分割になるが、frames の切り詰めで
    # frames_text 自体が 4096 を単独で超えることはない。
    generate_minutes(
        segs, notes, client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=4096,
    )
    from meeting_minutes.minutes import _approx_tokens
    for c in client.calls:
        # どの実リクエストのプロンプトも、frames だけで ctx を食い尽くしていない
        assert _approx_tokens(c["user"]) < 4096 * 3  # ざっくり: 暴走していない
