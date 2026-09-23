"""Tests for cli._make_reporter's same-stage throttling behavior.

No prior coverage existed for this function. Written while investigating a
Codex-flagged bug (PR #182): a naive "always show a stage's first/last
message" tweak looked appealing but breaks throttling for transcription's
estimated total (duration / 4), which is regularly exceeded or, when
duration probing fails, absent entirely — see transcribe.py's
_transcribe_faster_whisper, where the "total" reported to on_progress keeps
being bumped up to match current once the estimate is exceeded, and
_transcribe_mlx, whose total can likewise undershoot the real segment count.
Reported here as a straightforward regression suite for the existing,
unmodified throttle instead.
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


def test_throttles_rapid_ticks_within_same_stage(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [("vision", i, 10, f"frame {i}") for i in range(1, 9)]
    out = _run(report, calls, fake_now)

    # only the first of the rapid ticks gets through
    assert out.count("frame ") == 1
    assert "frame 1" in out


def test_throttles_even_when_current_exceeds_an_underestimated_total(monkeypatch):
    """transcribe.py's per-segment progress can report current > total once
    a rough duration-based estimate is exceeded (see _transcribe_mlx) or
    current == total on every tick when duration probing failed entirely
    (see _transcribe_faster_whisper, which bumps its total to match current
    once exceeded). Either way, these remain ordinary rapid progress ticks
    that must still be throttled, not one-off milestones."""
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [("transcribe", i, i, f"segment {i}") for i in range(1, 6)]
    out = _run(report, calls, fake_now)

    assert out.count("segment ") == 1
    assert "segment 1" in out


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


def test_a_message_past_the_throttle_window_still_prints(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report("vision", 1, 10, "frame 1")
        fake_now[0] += 0.6  # past the 0.5s window
        report("vision", 2, 10, "frame 2")
    out = buf.getvalue()

    assert "frame 1" in out
    assert "frame 2" in out
