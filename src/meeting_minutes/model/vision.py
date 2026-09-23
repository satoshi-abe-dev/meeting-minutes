"""Have the local VLM describe extracted frames, as supporting material for the minutes.

For each frame, turns "the slide's heading, bullet points, and the gist of
any figures/tables" into short text. If one frame fails, the whole run
doesn't stop — just that frame is skipped.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from meeting_minutes.i18n import DEFAULT_LANGUAGE, format_elapsed, t

from .cancel import check_cancel
from .config import load_prompt
from .frames import Frame
from .llm_client import LLMClient, LLMConnectionError

# Progress callback: (frames done, total frames, latest message)
ProgressFn = Callable[[int, int, str], None]

_DEFAULT_PROMPT = (
    "この画像は会議中の画面（スライドや画面共有）のスクリーンショットです。"
    "書かれている見出し・箇条書き・数値・図表の要点を、日本語で簡潔に箇条書きしてください。"
    "画面に意味のある情報が無い場合は「特筆事項なし」とだけ答えてください。"
)


@dataclass
class FrameNote:
    """The analysis result for one frame."""

    timestamp: float
    path: str  # path relative to out_dir (absolute if that's not possible)
    description: str


def _hhmmss(seconds: float) -> str:
    seconds = max(0, round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _load_prompt_text() -> str:
    try:
        return load_prompt("frame_describe_ja.txt").strip() or _DEFAULT_PROMPT
    except FileNotFoundError:
        return _DEFAULT_PROMPT


def _load_frame_notes(out_dir: Path) -> list[FrameNote]:
    """Read back frames/frame_notes.json as written by save_frame_notes (for resuming)."""
    path = out_dir / "frames" / "frame_notes.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    try:
        return [
            FrameNote(
                timestamp=float(d["timestamp"]),
                path=str(d["path"]),
                description=str(d["description"]),
            )
            for d in data
        ]
    except (KeyError, TypeError, ValueError):
        return []


def describe_frames(
    frames: list[Frame],
    client: LLMClient,
    out_dir: str | Path,
    *,
    on_progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
    reuse: bool = True,
    language: str = DEFAULT_LANGUAGE,
) -> list[FrameNote]:
    """Run every frame through the VLM and return a list of FrameNote.

    cancel_event: if set, cancels before starting the next frame (this is
        between VLM calls, of which there can be up to 60, so it's the
        point where the Stop button is most responsive).
    reuse: if True (the default) and a previous frames/frame_notes.json
        remains in out_dir, it's read back and those frames aren't
        re-analyzed. frames/frame_notes.json is rewritten after each frame
        finishes, so a crash partway through can resume from where it left
        off.
    """
    out_dir = Path(out_dir)
    prompt = _load_prompt_text()
    total = len(frames)

    notes: list[FrameNote] = []
    if reuse:
        existing = _load_frame_notes(out_dir)
        if 0 < len(existing) <= total:
            notes = existing
            if on_progress is not None:
                on_progress(
                    len(notes), total,
                    t("pmsg.vis_reuse", language, n=len(notes), total=total),
                )

    for i in range(len(notes) + 1, total + 1):
        check_cancel(cancel_event)
        frame = frames[i - 1]
        rel = _rel_or_abs(frame.path, out_dir)
        if on_progress is not None:
            on_progress(
                i - 1, total,
                t("pmsg.vis_frame_analyzing", language, ts=_hhmmss(frame.timestamp)),
            )
        t0 = time.monotonic()
        try:
            desc = client.describe_image(frame.path, prompt)
        except LLMConnectionError:
            # If the connection itself is dead, there's no point continuing, so abort
            raise
        except Exception as exc:
            desc = f"(解析失敗: {exc})"
        elapsed = format_elapsed(time.monotonic() - t0, language)
        notes.append(
            FrameNote(timestamp=frame.timestamp, path=rel, description=desc)
        )
        # save after every frame, so a partial run can be resumed
        save_frame_notes(notes, out_dir)
        if on_progress is not None:
            on_progress(
                i, total,
                t("pmsg.vis_frame_done", language,
                  ts=_hhmmss(frame.timestamp), elapsed=elapsed),
            )

    return notes


def save_frame_notes(notes: list[FrameNote], out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    path = out_dir / "frames" / "frame_notes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(n) for n in notes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def notes_to_text(notes: list[FrameNote]) -> str:
    """Turn notes into plain text with timestamps, in a form that's easy to hand to the LLM."""
    blocks = []
    for n in notes:
        blocks.append(f"[{_hhmmss(n.timestamp)}] {n.description.strip()}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _rel_or_abs(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
