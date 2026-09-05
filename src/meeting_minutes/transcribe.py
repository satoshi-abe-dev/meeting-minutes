"""文字起こし（完全ローカル）。

2 つのバックエンドを持つ:
    - "faster-whisper": CTranslate2 実装。どの OS でも動くが Mac では CPU のみ。
    - "mlx": Apple Silicon の GPU を使う mlx-whisper。Mac では大幅に速い。

`config.backend` が "auto" のときは Apple Silicon かつ mlx-whisper が入っていれば
mlx、そうでなければ faster-whisper を使う。

初回実行時のみ、指定モデルを Hugging Face からダウンロードする
（以後はローカルキャッシュを使い、オフラインで動作する）。
"""

from __future__ import annotations

import importlib.util
import json
import platform
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from .cancel import check_cancel
from .config import TranscribeConfig

# 進捗コールバック: (完了セグメント数, おおよその総数, 直近テキスト)
ProgressFn = Callable[[int, int, str], None]


@dataclass
class Segment:
    """文字起こしの 1 区間。"""

    start: float  # 秒
    end: float  # 秒
    text: str


def _format_ts(seconds: float) -> str:
    """秒を [HH:MM:SS] 形式にする。"""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --- バックエンドの選択 --------------------------------------------------

def _mlx_available() -> bool:
    return importlib.util.find_spec("mlx_whisper") is not None


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def resolve_backend(config: TranscribeConfig) -> str:
    """config.backend を実際に使うバックエンド名に解決する。

    戻り値は "mlx" か "faster-whisper"。
    """
    backend = (config.backend or "auto").strip().lower()
    if backend in ("mlx", "faster-whisper"):
        return backend
    if backend == "auto":
        if _is_apple_silicon() and _mlx_available():
            return "mlx"
        return "faster-whisper"
    return "faster-whisper"  # 未知の値はフォールバック


# mlx-whisper のモデル名（サイズ名 → HF リポジトリ）
_MLX_MODEL_MAP = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def _mlx_model_repo(name: str) -> str:
    """サイズ名を mlx-whisper 用の HF リポジトリ名へ。"/" 入り・未知はそのまま。"""
    if "/" in name:
        return name
    return _MLX_MODEL_MAP.get(name.strip().lower(), name)


def _mlx_model_cached(repo: str) -> bool:
    """mlx モデルが HuggingFace キャッシュに揃っているか（best-effort）。"""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(repo, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 - 判定不能はキャッシュ無し扱い
        return False


# --- 公開エントリ ------------------------------------------------------

def transcribe_wav(
    wav_path: str | Path,
    config: TranscribeConfig,
    *,
    on_progress: ProgressFn | None = None,
    total_hint: int | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Segment]:
    """wav を文字起こしして Segment のリストを返す。

    バックエンド（mlx / faster-whisper）は `resolve_backend(config)` で決まる。
    on_progress: 進捗コールバック。faster-whisper はセグメント確定ごと、mlx は
        完了後にまとめて呼ぶ（下記 _transcribe_mlx のコメント参照）。
    total_hint: 進捗表示用の総セグメント数の見込み（音声長 / 平均秒などから算出）。
    cancel_event: セットされていれば中断する。faster-whisper はセグメントの
        合間で反応するが、mlx は 1 回のブロッキング呼び出しなので呼び出し前
        にしか反応できない（下記 _transcribe_mlx 参照）。
    """
    if resolve_backend(config) == "mlx":
        return _transcribe_mlx(
            wav_path,
            config,
            on_progress=on_progress,
            total_hint=total_hint,
            cancel_event=cancel_event,
        )
    return _transcribe_faster_whisper(
        wav_path,
        config,
        on_progress=on_progress,
        total_hint=total_hint,
        cancel_event=cancel_event,
    )


def _transcribe_faster_whisper(
    wav_path: str | Path,
    config: TranscribeConfig,
    *,
    on_progress: ProgressFn | None = None,
    total_hint: int | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Segment]:
    # 重い依存なので関数内 import（テストでスタブしやすくもなる）
    from faster_whisper import WhisperModel

    if on_progress is not None:
        on_progress(
            0,
            total_hint or 0,
            "文字起こしモデルを準備中（未取得なら初回ダウンロード）…",
        )

    device = config.device or "auto"
    model = WhisperModel(
        config.model,
        device=device,
        compute_type=config.compute_type,
    )

    language = config.language.strip() or None
    raw_segments, _info = model.transcribe(
        str(wav_path),
        language=language,
        vad_filter=True,  # 無音区間を落として精度と速度を上げる
        # 直前の（誤った）出力を次の窓の文脈にしない。歌・BGM・雑音のある区間で
        # 同じ空耳フレーズを延々と繰り返す "repetition loop" 幻覚を防ぐ。
        condition_on_previous_text=False,
    )

    segments: list[Segment] = []
    approx_total = total_hint or 0
    for i, seg in enumerate(_iter_segments(raw_segments), start=1):
        # セグメントは遅延生成なので、ここで打ち切れば以降の生成も止まる。
        check_cancel(cancel_event)
        segments.append(seg)
        if approx_total and i > approx_total:
            approx_total = i
        if on_progress is not None:
            on_progress(i, approx_total or i, seg.text)
    return segments


def _transcribe_mlx(
    wav_path: str | Path,
    config: TranscribeConfig,
    *,
    on_progress: ProgressFn | None = None,
    total_hint: int | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Segment]:
    # mlx-whisper は結果を一括で返す（ジェネレータではない）ため、処理中の
    # 逐次進捗は出せない。開始時に 1 回説明を出し、完了後にセグメントを
    # 変換しながら on_progress を回す（進捗バーは 0 → 100 に飛ぶ）。
    import mlx_whisper

    repo = _mlx_model_repo(config.model)
    if on_progress is not None:
        if _mlx_model_cached(repo):
            msg = "mlx-whisper で文字起こし中（完了まで進捗は動きません）"
        else:
            msg = (
                f"文字起こしモデル {repo} をダウンロード中（初回のみ、約1.6GB）…"
                "その後 mlx-whisper で文字起こし"
            )
        on_progress(0, total_hint or 0, msg)

    # mlx-whisper はブロッキングの一括呼び出しなので、呼び出し中は中断に反応
    # できない。呼び出し前にだけチェックする。
    check_cancel(cancel_event)

    language = config.language.strip() or None
    result = mlx_whisper.transcribe(
        str(wav_path),
        path_or_hf_repo=repo,
        language=language,
        word_timestamps=False,
        # 直前の（誤った）出力を次の窓の文脈にしない。歌・BGM・雑音のある区間で
        # 同じ空耳フレーズを延々と繰り返す "repetition loop" 幻覚を防ぐ。
        # mlx-whisper には faster-whisper の vad_filter に相当する無音除去が無いため、
        # この対策の重要度がより高い。
        condition_on_previous_text=False,
    )

    raw = result.get("segments", []) if isinstance(result, dict) else []
    total = total_hint or len(raw)
    segments: list[Segment] = []
    for i, s in enumerate(raw, start=1):
        seg = Segment(
            start=float(s.get("start", 0.0)),
            end=float(s.get("end", 0.0)),
            text=(s.get("text") or "").strip(),
        )
        segments.append(seg)
        if on_progress is not None:
            on_progress(i, total or i, seg.text)
    return segments


def _iter_segments(raw: Iterable) -> Iterable[Segment]:
    for s in raw:
        yield Segment(start=float(s.start), end=float(s.end), text=(s.text or "").strip())


def save_transcript(segments: list[Segment], out_dir: str | Path) -> tuple[Path, Path]:
    """文字起こしを JSON とプレーンテキストの両方で保存する。

    戻り値は (json_path, txt_path)。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "transcript.json"
    txt_path = out_dir / "transcript.txt"

    json_path.write_text(
        json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    txt_path.write_text(transcript_to_text(segments), encoding="utf-8")
    return json_path, txt_path


def transcript_to_text(segments: list[Segment]) -> str:
    """[HH:MM:SS] 行頭タイムスタンプ付きの読みやすいテキストにする。"""
    lines = [f"[{_format_ts(s.start)}] {s.text}" for s in segments if s.text]
    return "\n".join(lines) + ("\n" if lines else "")


def load_transcript(out_dir: str | Path) -> list[Segment]:
    """save_transcript が書いた transcript.json を読み戻す（再開用）。"""
    path = Path(out_dir) / "transcript.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Segment(
            start=float(d["start"]), end=float(d["end"]), text=(d.get("text") or "")
        )
        for d in data
    ]
