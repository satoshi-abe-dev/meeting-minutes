"""Tests for vision.describe_frames (a fake client is injected instead of calling the LLM).

out_dir always uses tmp_path (a real directory independent per test).
describe_frames does a real file write to frames/frame_notes.json (persisted
for resuming), so using a shared path would leak state between tests.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meeting_minutes.model.cancel import PipelineCancelled
from meeting_minutes.model.frames import Frame
from meeting_minutes.model.vision import describe_frames


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


def _frame_path_str(i: int) -> str:
    """The str form of the path `_frames` creates. Follows the OS's path
    separator (on Windows this becomes ``\\tmp\\frame_0.jpg``, so a literal
    comparison would fail)."""
    return str(Path(f"/tmp/frame_{i}.jpg"))


def test_describe_frames_calls_each_frame_in_order(tmp_path):
    client = FakeVisionClient(replies=["A", "B", "C"])
    notes = describe_frames(_frames(3), client, tmp_path)
    assert [n.description for n in notes] == ["A", "B", "C"]
    assert client.calls == [_frame_path_str(0), _frame_path_str(1), _frame_path_str(2)]


def test_describe_frames_reports_waiting_then_done_per_frame(tmp_path):
    client = FakeVisionClient(replies=["A"])
    events: list[tuple] = []
    describe_frames(
        _frames(1), client, tmp_path,
        on_progress=lambda c, t, m: events.append((c, t, m)),
    )
    assert len(events) == 2
    assert "waiting for a response" in events[0][2]
    assert events[0][0] == 0
    assert "analyzed" in events[1][2] and "elapsed" in events[1][2]
    assert events[1][0] == 1


def test_describe_frames_messages_translated_when_language_en(tmp_path):
    client = FakeVisionClient(replies=["A"])
    events: list[tuple] = []
    describe_frames(
        _frames(1), client, tmp_path,
        on_progress=lambda c, t, m: events.append((c, t, m)),
        language="en",
    )
    assert "waiting for a response" in events[0][2]
    assert "応答を待っています" not in events[0][2]
    assert "elapsed" in events[1][2]
    assert "所要" not in events[1][2]


def test_describe_frames_cancel_stops_before_next_frame(tmp_path):
    client = FakeVisionClient()
    cancel_event = threading.Event()

    def on_progress(cur, total, msg):
        if cur == 1:
            cancel_event.set()  # simulate the Stop button being pressed right after the first frame finishes

    with pytest.raises(PipelineCancelled):
        describe_frames(
            _frames(5),
            client,
            tmp_path,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    # stops before starting on the 2nd frame
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

    saved = json.loads((tmp_path / "frames" / "frame_notes.json").read_text(encoding="utf-8"))
    assert len(saved) == 1


def test_describe_frames_resumes_from_saved_notes(tmp_path):
    (tmp_path / "frames").mkdir(exist_ok=True)
    (tmp_path / "frames" / "frame_notes.json").write_text(
        json.dumps(
            [{"timestamp": 0.0, "path": "frame_0.jpg", "description": "既存の解析"}]
        ),
        encoding="utf-8",
    )
    client = FakeVisionClient(replies=["B", "C"])

    notes = describe_frames(_frames(3), client, tmp_path, reuse=True)

    assert [n.description for n in notes] == ["既存の解析", "B", "C"]
    # the 1st frame wasn't redone
    assert client.calls == [_frame_path_str(1), _frame_path_str(2)]


def test_describe_frames_fresh_ignores_saved_notes(tmp_path):
    (tmp_path / "frames").mkdir(exist_ok=True)
    (tmp_path / "frames" / "frame_notes.json").write_text(
        json.dumps(
            [{"timestamp": 0.0, "path": "frame_0.jpg", "description": "既存の解析"}]
        ),
        encoding="utf-8",
    )
    client = FakeVisionClient(replies=["A", "B", "C"])

    notes = describe_frames(_frames(3), client, tmp_path, reuse=False)

    assert [n.description for n in notes] == ["A", "B", "C"]
    assert len(client.calls) == 3
