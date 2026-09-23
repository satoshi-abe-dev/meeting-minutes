#!/usr/bin/env bash
# One-shot setup: create the virtualenv -> install dependencies -> fetch
# the transcription model.
# The only thing the user runs is this one script.
#
#   bash scripts/setup.sh
#
# This script connects to the internet to download the Whisper model
# (HuggingFace). Fetching it is mandatory; if it fails, setup itself exits
# with a failure (at runtime, offline mode is forced so it never
# auto-downloads — this is the only place it gets fetched).
#
# Also needed separately:
#   - ffmpeg (brew install ffmpeg)
#   - Start a local server in LM Studio and load the LLM/VLM (docs/setup_ja.md)

set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

echo "==> 仮想環境 .venv を作成"
"$PY" -m venv .venv

echo "==> 依存をインストール"
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> 文字起こしモデルを取得（インターネットに接続します。数分かかることがあります）"
echo "    この手順は HuggingFace から Whisper モデルをダウンロードします。"
echo "    アプリ実行時はオフライン強制のため、ここでの取得が必須です。"
# If this fails, set -e exits setup itself with a failure (not swallowed).
./.venv/bin/python src/meeting_minutes/download_transcribe_model.py

cat <<'DONE'

==> 完了

次のように起動します:

    source .venv/bin/activate
    python src/meeting_minutes/gui.py            # GUI
    python src/meeting_minutes/cli.py 動画.mp4    # CLI

DONE
