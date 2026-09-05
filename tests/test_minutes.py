"""minutes モジュールのテスト（LLM は呼ばずにスタブ）。"""

from __future__ import annotations

import json
import threading

import pytest

from meeting_minutes.cancel import PipelineCancelled
from meeting_minutes.config import LLMConfig
from meeting_minutes.minutes import (
    MinutesMeta,
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
def force_chunking(monkeypatch):
    """分割要約（長い会議）の経路を、実際のトリガー定数に依存せずに検証するための小さいしきい値。

    通常運用ではコンテキストに収まる限り一発生成を優先するためしきい値は大きい。
    ここではテスト用に下げて、必ずチャンク要約 -> 統合 の経路を通す。
    """
    monkeypatch.setattr("meeting_minutes.minutes._CHUNK_TRIGGER_CHARS", 2000)
    monkeypatch.setattr("meeting_minutes.minutes._CHUNK_SIZE_CHARS", 1000)


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


def test_generate_minutes_single_pass_for_moderately_long_transcript():
    """しきい値を上げたので、約 2 万字程度なら分割せず一発生成する（旧実装では分割された）。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(300, text="議題について長い発言をする" * 5)
    generate_minutes(segs, [], client, LLMConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) == 1
    assert client.calls[0]["user"].startswith("以下のテンプレートに沿って")


def test_generate_minutes_long_path_maps_then_reduces(force_chunking):
    # チャンク要約が走るよう十分長い文字起こしを作る
    client = FakeClient(reply="部分要約 or 最終議事録")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")

    progress: list[tuple] = []
    md = generate_minutes(
        long_segs,
        [],
        client,
        LLMConfig(),
        meta,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
    )

    assert md.endswith("\n")
    # チャンク要約(複数) + 最終統合(1) で 2 回以上呼ばれる
    assert len(client.calls) >= 2
    assert progress  # 進捗が通知されている


def test_generate_minutes_cancel_stops_chunk_loop(force_chunking):
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
            LLMConfig(),
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

def test_generate_minutes_persists_partials_on_cancel(tmp_path, force_chunking):
    client = FakeClient(reply="部分要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            long_segs, [], client, LLMConfig(), meta,
            on_progress=on_progress, cancel_event=cancel_event, out_dir=tmp_path,
        )

    saved = json.loads((tmp_path / "minutes_partials.json").read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0].startswith("### 部分 1")


def test_generate_minutes_resumes_from_saved_partials(tmp_path, force_chunking):
    (tmp_path / "minutes_partials.json").write_text(
        json.dumps(["### 部分 1\n既存の要約"]), encoding="utf-8"
    )
    client = FakeClient(reply="最終議事録")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")

    md = generate_minutes(
        long_segs, [], client, LLMConfig(), meta, out_dir=tmp_path, reuse=True
    )

    assert md.strip() == "最終議事録"
    # 1つ目のチャンクは要約し直していない
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert all("既存の要約" not in c["user"] for c in chunk_calls)
    # 統合（最終）呼び出しには再利用した部分要約が含まれる
    assert "既存の要約" in client.calls[-1]["user"]


def test_generate_minutes_fresh_ignores_saved_partials(tmp_path, force_chunking):
    (tmp_path / "minutes_partials.json").write_text(
        json.dumps(["### 部分 1\n既存の要約"]), encoding="utf-8"
    )
    client = FakeClient(reply="要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, LLMConfig(), meta, out_dir=tmp_path, reuse=False
    )

    # reuse=False なので既存の部分要約は使われず、最初から要約し直す
    assert "既存の要約" not in client.calls[-1]["user"]


def test_generate_minutes_ignores_empty_bodied_partials(tmp_path, force_chunking):
    """見出しだけで本文が空の部分要約（LLMが空応答を返した形跡）は無効として扱う。"""
    (tmp_path / "minutes_partials.json").write_text(
        json.dumps(["### 部分 1\n"]), encoding="utf-8"  # 本文なし
    )
    client = FakeClient(reply="要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, LLMConfig(), meta, out_dir=tmp_path, reuse=True
    )

    # 空だった部分1はやり直されている（チャンク呼び出しが発生している）
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert len(chunk_calls) >= 1


def test_generate_minutes_chunk_uses_llm_config_max_tokens(tmp_path, force_chunking):
    """チャンク要約の max_tokens は固定値ではなく llm_config.max_tokens を使う。"""
    client = FakeClient(reply="要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    llm_config = LLMConfig(max_tokens=12345)

    generate_minutes(long_segs, [], client, llm_config, meta, out_dir=tmp_path)

    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert chunk_calls
    assert all(c["kwargs"]["max_tokens"] == 12345 for c in chunk_calls)
