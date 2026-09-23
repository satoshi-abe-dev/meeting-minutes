"""Fetch the transcription model locally (the only place the app fetches it from).

Called from setup (scripts/setup.sh). **At runtime (cli.py / gui.py), offline
mode is forced, so the Whisper model is never auto-downloaded**, which makes
fetching it beforehand mandatory. Can also be run manually:

    python src/meeting_minutes/download_transcribe_model.py
    python src/meeting_minutes/download_transcribe_model.py --config config.toml
(Developers can also use `python -m meeting_minutes.download_transcribe_model`.)

Fetches only the Whisper model matching the resolved backend (mlx /
faster-whisper). LM Studio's LLM/VLM are out of scope (fetched separately on
the LM Studio side). The model goes into HuggingFace's shared cache
(~/.cache/huggingface/hub/, changeable via HF_HOME) and is usable offline
after that. If already fetched, this exits doing nothing (idempotent).

Unlike cli.py / gui.py, this script does not set HF_HUB_OFFLINE (this is the
side that does the fetching). Being a separate process/entry point, it's
also unaffected by their setting.
"""

from __future__ import annotations

import argparse
import os
import sys

# Launching by file path leaves __package__ unset, breaking relative
# imports. Add src/ (package parent) to sys.path and use absolute imports
# instead (works fine as a module too — tests / -m / scripts).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_minutes.model.config import load_config
from meeting_minutes.model.transcribe import _mlx_model_repo, resolve_backend


def download_transcribe_model(config) -> str:
    """Fetch the Whisper model matching the config, and return the fetched target's identifier."""
    backend = resolve_backend(config.transcribe)
    model_name = config.transcribe.model

    if backend == "mlx":
        repo = _mlx_model_repo(model_name)
        print(f"[download] mlx モデルを取得: {repo}", flush=True)
        from huggingface_hub import snapshot_download

        snapshot_download(repo)
        return repo

    # faster-whisper: instantiating the model triggers the download
    print(f"[download] faster-whisper モデルを取得: {model_name}", flush=True)
    from faster_whisper import WhisperModel

    WhisperModel(
        model_name,
        device=config.transcribe.device or "auto",
        compute_type=config.transcribe.compute_type,
    )
    return model_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="文字起こしモデルをローカルに取得する（唯一の取得口。実行時は自動DLしない）"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="設定 TOML のパス（省略時: config.toml → config.example.toml）",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    try:
        target = download_transcribe_model(config)
    except Exception as exc:
        print(
            f"モデルの取得に失敗しました: {exc}\n"
            "ネットワーク接続と HF_HOME を確認し、"
            "`python src/meeting_minutes/download_transcribe_model.py` を"
            "再実行してください。アプリ実行時は自動ダウンロードしないため、"
            "モデルが無いと文字起こしは実行できません。",
            file=sys.stderr,
        )
        return 1

    print(f"[download] 完了: {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
