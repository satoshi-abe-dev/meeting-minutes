"""抽出済みフレームをローカル VLM で説明させ、議事録の裏取り素材にする。

各フレームについて「スライドの見出し・箇条書き・図表の要点」を短くテキスト化する。
1 枚失敗しても全体は止めず、その枚だけスキップする。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .cancel import check_cancel
from .config import load_prompt
from .frames import Frame
from .llm_client import LLMClient, LLMConnectionError

# 進捗コールバック: (完了枚数, 総枚数, 直近メッセージ)
ProgressFn = Callable[[int, int, str], None]

_DEFAULT_PROMPT = (
    "この画像は会議中の画面（スライドや画面共有）のスクリーンショットです。"
    "書かれている見出し・箇条書き・数値・図表の要点を、日本語で簡潔に箇条書きしてください。"
    "画面に意味のある情報が無い場合は「特筆事項なし」とだけ答えてください。"
)


@dataclass
class FrameNote:
    """フレーム 1 枚の解析結果。"""

    timestamp: float
    path: str  # out_dir からの相対パス（無理なら絶対）
    description: str


def _hhmmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _load_prompt_text() -> str:
    try:
        return load_prompt("frame_describe_ja.txt").strip() or _DEFAULT_PROMPT
    except FileNotFoundError:
        return _DEFAULT_PROMPT


def _format_elapsed(seconds: float) -> str:
    """処理にかかった時間の表示用（pipeline.py / minutes.py にも同名の複製がある）。"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分"


def _load_frame_notes(out_dir: Path) -> list[FrameNote]:
    """save_frame_notes が書いた frame_notes.json を読み戻す（再開用）。"""
    path = out_dir / "frame_notes.json"
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
) -> list[FrameNote]:
    """全フレームを VLM にかけて FrameNote のリストを返す。

    cancel_event: セットされていれば、次のフレームに取り掛かる前に中断する
        （最大60回ある VLM 呼び出しの合間なので、中断ボタンが一番効くポイント）。
    reuse: True（既定）なら out_dir に前回の frame_notes.json が残っていれば
        読み戻し、そこまでのフレームは解析し直さない。1 枚終えるたびに
        frame_notes.json を書き直すので、途中で落ちても続きから再開できる。
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
                    f"既存のフレーム解析を再利用（{len(notes)}/{total}）",
                )

    for i in range(len(notes) + 1, total + 1):
        check_cancel(cancel_event)
        frame = frames[i - 1]
        rel = _rel_or_abs(frame.path, out_dir)
        if on_progress is not None:
            on_progress(
                i - 1, total,
                f"{_hhmmss(frame.timestamp)} のフレームを解析中…応答を待っています",
            )
        t0 = time.monotonic()
        try:
            desc = client.describe_image(frame.path, prompt)
        except LLMConnectionError:
            # 接続そのものが死んでいる場合は続けても無駄なので中断
            raise
        except Exception as exc:  # noqa: BLE001 - 1 枚の失敗は握りつぶして続行
            desc = f"(解析失敗: {exc})"
        elapsed = _format_elapsed(time.monotonic() - t0)
        notes.append(
            FrameNote(timestamp=frame.timestamp, path=rel, description=desc)
        )
        save_frame_notes(notes, out_dir)  # 1枚ごとに保存し、途中再開できるようにする
        if on_progress is not None:
            on_progress(
                i, total,
                f"{_hhmmss(frame.timestamp)} のフレーム解析が完了（所要 {elapsed}）",
            )

    return notes


def save_frame_notes(notes: list[FrameNote], out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "frame_notes.json"
    path.write_text(
        json.dumps([asdict(n) for n in notes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def notes_to_text(notes: list[FrameNote]) -> str:
    """LLM に渡しやすい、時刻付きのプレーンテキストにする。"""
    blocks = []
    for n in notes:
        blocks.append(f"[{_hhmmss(n.timestamp)}] {n.description.strip()}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _rel_or_abs(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
