"""Tests for cli._make_reporter's same-stage throttling behavior.

No prior coverage existed for this function. Written while investigating two
rounds of Codex-flagged bugs on PR #182:

1. A naive "always show a stage's first/last message" tweak looked
   appealing but breaks throttling for transcription's estimated total
   (duration / 4), which is regularly exceeded or, when duration probing
   fails, absent entirely — see transcribe.py's _transcribe_faster_whisper,
   where the "total" reported to on_progress keeps being bumped up to match
   current once the estimate is exceeded, and _transcribe_mlx, whose total
   can likewise undershoot the real segment count.
2. Folding a one-off notice into an adjacent completion message doesn't
   help if that completion message can itself be suppressed — mlx's
   post-decode per-segment loop has no real per-iteration delay, so its
   many same-stage calls plus the pipeline's own completion message right
   after can all land inside one 0.5s window. Leading-edge-only throttling
   would then drop the completion message (and whatever notice was folded
   into it) forever.

_make_reporter() now remembers the most recently suppressed call and
flushes it as soon as the stage actually changes, so a stage's true last
word is delayed at most until the next stage's first message, never lost.
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


def test_suppressed_completion_message_is_flushed_on_stage_change(monkeypatch):
    """Reproduces the mlx scenario from the Codex review: many rapid
    same-stage segment ticks followed immediately (same 0.5s window) by
    that stage's own completion message — carrying a folded-in language
    notice, as pipeline.py now does. The completion message must still
    appear once the next stage begins, not be silently dropped."""
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [
        *[("transcribe", i, i, f"segment {i}") for i in range(1, 16)],
        (
            "transcribe", 15, 15,
            "Transcription done (15 segments) / Auto-detected the "
            "recording's language: en",
        ),
        ("frames", 0, 1, "Extracting frames"),
    ]
    out = _run(report, calls, fake_now)

    assert "Auto-detected the recording's language: en" in out
    assert "Extracting frames" in out
    # the flushed completion message prints before the next stage's message
    assert out.index("Auto-detected") < out.index("Extracting frames")


def test_pending_message_from_a_stage_with_no_further_calls_is_not_flushed(monkeypatch):
    """If the pipeline never reports another stage at all (e.g. the run
    ends without reaching "done" for some reason), a suppressed message has
    no later call to flush on — same limitation as today, not a regression."""
    fake_now = [0.0]
    monkeypatch.setattr("meeting_minutes.cli.time.monotonic", lambda: fake_now[0])
    report = _make_reporter()

    calls = [("vision", 1, 10, "frame 1"), ("vision", 10, 10, "frame 10 / done")]
    out = _run(report, calls, fake_now)

    assert "frame 1" in out
    assert "frame 10" not in out
