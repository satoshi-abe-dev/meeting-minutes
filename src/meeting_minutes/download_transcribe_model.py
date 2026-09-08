"""文字起こしモデルをローカルに取得する（アプリ実行時はこれが唯一の取得口）。

セットアップ（scripts/setup.sh）から呼ばれる。**アプリ実行時（cli.py / gui.py）は
オフライン強制のため Whisper モデルを自動ダウンロードしない**ので、事前取得は必須。
手動でも実行できる:

    python src/meeting_minutes/download_transcribe_model.py
    python src/meeting_minutes/download_transcribe_model.py --config config.toml
（開発者向けに `python -m meeting_minutes.download_transcribe_model` も可）

解決後のバックエンド（mlx / faster-whisper）に対応する Whisper モデルだけを取得する。
LM Studio の LLM / VLM は対象外（LM Studio 側で各自ダウンロードする）。
モデルは HuggingFace の共有キャッシュ（~/.cache/huggingface/hub/、HF_HOME で変更可）に入り、
以後はオフラインで使える。取得済みなら何もせず終了する（冪等）。

このスクリプトは cli.py / gui.py と違い HF_HUB_OFFLINE を立てない（ここは取得する側）。
別プロセス・別エントリポイントなので、それらの設定の影響も受けない。
"""

from __future__ import annotations

import argparse
import os
import sys

# `python src/meeting_minutes/download_transcribe_model.py` のようにファイル指定で直接起動されると
# __package__ が未設定で相対 import（from .config …）が使えない。src レイアウトの
# パッケージ親 = src/（このファイルの 2 つ上）を sys.path に足し、絶対 import で書く。
# import 済みモジュール（tests / `-m` / scripts）としては絶対 import でも問題なく解決する。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_minutes.model.config import load_config
from meeting_minutes.model.transcribe import _mlx_model_repo, resolve_backend


def download_transcribe_model(config) -> str:
    """設定に対応する Whisper モデルを取得し、取得対象の識別子を返す。"""
    backend = resolve_backend(config.transcribe)
    model_name = config.transcribe.model

    if backend == "mlx":
        repo = _mlx_model_repo(model_name)
        print(f"[download] mlx モデルを取得: {repo}", flush=True)
        from huggingface_hub import snapshot_download

        snapshot_download(repo)
        return repo

    # faster-whisper: モデルの生成でダウンロードが起きる
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
