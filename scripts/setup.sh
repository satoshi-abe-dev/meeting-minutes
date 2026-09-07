#!/usr/bin/env bash
# 一発セットアップ: 仮想環境の作成 → 依存インストール → 文字起こしモデルの取得。
# 利用者が打つのはこのスクリプトの実行 1 つだけ。
#
#   bash scripts/setup.sh
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

echo "==> 文字起こしモデルを取得（初回のみ。数分かかることがあります）"
if ! ./.venv/bin/python src/meeting_minutes/prefetch.py; then
  echo "  モデルの事前取得に失敗しました。"
  echo "  初回の文字起こし実行時に自動ダウンロードされるため、そのまま進めても構いません。"
fi

cat <<'DONE'

==> 完了

次のように起動します:

    source .venv/bin/activate
    python src/meeting_minutes/gui.py            # GUI
    python src/meeting_minutes/cli.py 動画.mp4    # CLI

DONE
