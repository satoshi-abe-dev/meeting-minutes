#!/usr/bin/env bash
# One-shot setup: virtualenv -> deps -> transcription model. The only
# command the user runs.
#
#   bash scripts/setup.sh
#
# Downloads the Whisper model from HuggingFace (mandatory; fails setup if it
# can't). Runtime is offline-only, so this is the sole place it's fetched.
#
# Also needed separately: ffmpeg (brew install ffmpeg), and a running LM
# Studio server with the LLM/VLM loaded (docs/setup_ja.md).

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
