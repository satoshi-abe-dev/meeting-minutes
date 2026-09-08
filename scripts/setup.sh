#!/usr/bin/env bash
# 一発セットアップ: 仮想環境の作成 → 依存インストール → 文字起こしモデルの取得。
# 利用者が打つのはこのスクリプトの実行 1 つだけ。
#
#   bash scripts/setup.sh
#
# このスクリプトはインターネットに接続して Whisper モデル（HuggingFace）を
# ダウンロードする。取得は必須で、失敗したらセットアップ自体を失敗終了させる
# （アプリ実行時はオフライン強制のため自動ダウンロードしない。ここで取り切る）。
#
# 別途必要なもの:
#   - ffmpeg（brew install ffmpeg）
#   - LM Studio でローカルサーバーを起動し、LLM / VLM をロード（docs/setup-mac.md）

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
# 失敗したら set -e でセットアップ自体を失敗終了させる（握りつぶさない）。
./.venv/bin/python src/meeting_minutes/download_transcribe_model.py

cat <<'DONE'

==> 完了

次のように起動します:

    source .venv/bin/activate
    python src/meeting_minutes/gui.py            # GUI
    python src/meeting_minutes/cli.py 動画.mp4    # CLI

DONE
