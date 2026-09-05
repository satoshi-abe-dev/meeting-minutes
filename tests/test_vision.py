"""vision.describe_frames のテスト（LLM は呼ばずフェイククライアント注入）。

out_dir は必ず tmp_path（テストごとに独立した実ディレクトリ）を使う。
describe_frames は frame_notes.json への実ファイル書き込み（再開用の永続化）を
行うため、共有パスを使うとテスト間で状態が漏れる。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meeting_minutes.cancel import PipelineCancelled
from meeting_minutes.frames import Frame
from meeting_minutes.vision import describe_frames


class FakeVisionClient:
    def __init__(self, replies: list[str] | None = None):
        self.calls: list[str] = []
        self._replies = replies

    def describe_image(self, path, prompt):
        self.calls.append(str(path))
        if self._replies:
            return self._replies[len(self.calls) - 1]
        return "特筆事項なし"


def _frames(n: int) -> list[Frame]:
    return [Frame(timestamp=float(i), path=Path(f"/tmp/frame_{i}.jpg")) for i in range(n)]


def test_describe_frames_calls_each_frame_in_order(tmp_path):
    client = FakeVisionClient(replies=["A", "B", "C"])
    notes = describe_frames(_frames(3), client, tmp_path)
    assert [n.description for n in notes] == ["A", "B", "C"]
    assert client.calls == ["/tmp/frame_0.jpg", "/tmp/frame_1.jpg", "/tmp/frame_2.jpg"]


def test_describe_frames_reports_waiting_then_done_per_frame(tmp_path):
    client = FakeVisionClient(replies=["A"])
    events: list[tuple] = []
    describe_frames(
        _frames(1), client, tmp_path,
        on_progress=lambda c, t, m: events.append((c, t, m)),
    )
    assert len(events) == 2
    assert "応答を待っています" in events[0][2]
    assert events[0][0] == 0
    assert "完了" in events[1][2] and "所要" in events[1][2]
    assert events[1][0] == 1


def test_describe_frames_cancel_stops_before_next_frame(tmp_path):
    client = FakeVisionClient()
    cancel_event = threading.Event()

    def on_progress(cur, total, msg):
        if cur == 1:
            cancel_event.set()  # 1 枚目が終わった直後に中断ボタンが押された想定

    with pytest.raises(PipelineCancelled):
        describe_frames(
            _frames(5),
            client,
            tmp_path,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    # 2 枚目に取り掛かる前に止まる
    assert len(client.calls) == 1


def test_describe_frames_persists_incrementally(tmp_path):
    client = FakeVisionClient()
    cancel_event = threading.Event()

    def on_progress(cur, total, msg):
        if cur == 1:
            cancel_event.set()

    with pytest.raises(PipelineCancelled):
        describe_frames(
            _frames(3), client, tmp_path,
            on_progress=on_progress, cancel_event=cancel_event,
        )

    saved = json.loads((tmp_path / "frame_notes.json").read_text(encoding="utf-8"))
    assert len(saved) == 1


def test_describe_frames_resumes_from_saved_notes(tmp_path):
    (tmp_path / "frame_notes.json").write_text(
        json.dumps(
            [{"timestamp": 0.0, "path": "frame_0.jpg", "description": "既存の解析"}]
        ),
        encoding="utf-8",
    )
    client = FakeVisionClient(replies=["B", "C"])

    notes = describe_frames(_frames(3), client, tmp_path, reuse=True)

    assert [n.description for n in notes] == ["既存の解析", "B", "C"]
    # 1 枚目はやり直していない
    assert client.calls == ["/tmp/frame_1.jpg", "/tmp/frame_2.jpg"]


def test_describe_frames_fresh_ignores_saved_notes(tmp_path):
    (tmp_path / "frame_notes.json").write_text(
        json.dumps(
            [{"timestamp": 0.0, "path": "frame_0.jpg", "description": "既存の解析"}]
        ),
        encoding="utf-8",
    )
    client = FakeVisionClient(replies=["A", "B", "C"])

    notes = describe_frames(_frames(3), client, tmp_path, reuse=False)

    assert [n.description for n in notes] == ["A", "B", "C"]
    assert len(client.calls) == 3
