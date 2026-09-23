"""Tests for cli._make_reporter's throttling behavior.

Regression coverage for a bug found via Codex review on PR #182: one-off
milestone messages sharing a stage with an adjacent message (e.g. a
detected/extraction/minutes-language notice right before or after an
existing same-stage message) were silently dropped by the same-stage
0.5-second throttle meant for rapid fine-grained progress (per-frame,
per-segment, per-chunk ticks).
"""

from __future__ import annotations

import contextlib
import io

from meeting_minutes.cli import _make_reporter


def _run(report, calls, fake_now):
    """Feed (stage, current, total, message) tuples to report, with a fake
    clock that advances by a tiny amount (well under the 0.5s window) for
    each call, simulating messages arriving in rapid succession."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for stage, current, total, message in calls:
            fake_now[0] += 0.01
            report(stage, current, total, message)
    return buf.getvalue()


def test_throttles_rapid_mid_progress_ticks_within_same_stage(monkeypatch):
    """The original purpose: many fast per-frame/per-segment ticks in the
    middle of a stage (0 < current < total) must still be throttled to
    avoid flooding the log."""
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [("vision", i, 10, f"frame {i}") for i in range(1, 9)]  # 1..8 of 10
    out = _run(report, calls, fake_now)

    # only the first of the rapid mid-progress ticks gets through
    assert out.count("frame ") == 1
    assert "frame 1" in out


def test_does_not_throttle_first_and_last_message_of_a_stage(monkeypatch):
    """current=0 (stage start) and current=total (stage end) always print,
    even back-to-back with another same-stage message, since these carry
    one-off milestone announcements (e.g. detected-language notices) that
    must never be silently dropped."""
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [
        ("transcribe", 2, 2, "Transcription done (2 segments)"),
        ("transcribe", 2, 2, "Auto-detected the recording's language: en"),
        ("vision", 0, 5, "Extracting frame descriptions in: en"),
        ("vision", 0, 5, "Analyzing frames (model: qwen2-vl-7b-instruct)"),
        ("minutes", 0, 0, "Writing the minutes in: ja"),
        ("minutes", 0, 0, "Generating the minutes..."),
    ]
    out = _run(report, calls, fake_now)

    for _stage, _cur, _tot, message in calls:
        assert message in out


def test_mid_progress_ticks_right_after_a_boundary_message_still_throttle(monkeypatch):
    """A boundary message (current=0) still starts the throttle window, so
    genuine mid-progress ticks that immediately follow are throttled
    normally (the fix doesn't accidentally disable throttling for the rest
    of the stage) — only the boundary message itself is exempt."""
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [
        ("vision", 0, 10, "Analyzing frames (model: x)"),
        ("vision", 1, 10, "frame 1"),
        ("vision", 2, 10, "frame 2"),
        ("vision", 3, 10, "frame 3"),
    ]
    out = _run(report, calls, fake_now)

    assert "Analyzing frames" in out
    assert "frame " not in out  # all within the 0.5s window, correctly throttled


def test_done_stage_is_never_throttled(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [("done", 1, 1, "first"), ("done", 1, 1, "second")]
    out = _run(report, calls, fake_now)

    assert "first" in out
    assert "second" in out


def test_different_stages_are_never_throttled_against_each_other(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [("audio", 1, 1, "audio done"), ("transcribe", 1, 5, "mid-progress")]
    out = _run(report, calls, fake_now)

    assert "audio done" in out
    assert "mid-progress" in out
