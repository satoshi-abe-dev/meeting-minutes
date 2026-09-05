# セットアップ手順（macOS / Apple Silicon）

## 1. ffmpeg

```bash
brew install ffmpeg
which ffmpeg ffprobe   # パスが出れば OK
```

## 2. Python 環境 + モデル取得（ワンコマンド）

Python 3.11 以上（動作確認は 3.14 系）。

```bash
cd meeting-minutes
bash scripts/setup.sh
```

`scripts/setup.sh` が次をまとめて行う:

1. 仮想環境 `.venv` を作成
2. `pip install -r requirements.txt`
   - Apple Silicon の Mac では環境マーカーにより **mlx-whisper も一緒に入る**
     （GPU を使う文字起こしバックエンド）。Intel Mac / Linux では自動スキップされ
     faster-whisper だけになる。
3. `python prefetch.py` で **文字起こしモデルを事前ダウンロード**
   （`~/.cache/huggingface/hub/` に約 1.6GB、初回のみ。以降オフライン）。
   ダウンロードに失敗しても止まらず、初回の文字起こし実行時に自動取得される。

`config.toml` の `[transcribe] backend` は既定 `auto`（Apple Silicon なら mlx、
他は faster-whisper）。`mlx` / `faster-whisper` に固定もできる。既定モデルは
`large-v3-turbo`。

### 手動でやる場合（setup.sh を使わない）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python prefetch.py            # モデル取得（任意。省略しても初回実行時に自動DL）
```

### モデルの置き場所と配布

モデルは **利用者のホームの共有キャッシュ**（`~/.cache/huggingface/hub/`、`HF_HOME`
で変更可）に入る。リポジトリには含めないので、**各利用者の初回セットアップ時に一度だけ
ダウンロード**が発生する（`scripts/setup.sh` がそれを済ませる）。エアギャップ環境では
`HF_HOME` を社内ミラー/共有ストレージに向けるか、キャッシュを配布イメージに含める。

## 3. ローカル LLM サーバー（LM Studio）

画面構成は LM Studio のバージョンで変わります。ここでは新しい UI（「Bionic」系。
サイドバーが Settings / Integrations / Devices / Local Models に分かれているもの）を
前提にします。旧 UI では「Developer」タブに同等の設定があります。

1. [LM Studio](https://lmstudio.ai/) をインストールして起動。
2. モデルを 2 つ用意する（推奨は [`models.md`](models.md)）:
   - テキスト LLM（議事録生成用）
   - VLM（vision 対応。フレーム解析用）

   入手は左メニュー **Local Models → Explore** から検索してダウンロード。
   ダウンロード済みは **Local Models → Library** で確認できます。
3. ローカル API サーバーを起動する:
   - **Settings**（設定画面）を開く → 左メニュー **Local Models → Local Model API**
   - **「Local API server」** を ON（表示が **Running** になる）
   - 同じ画面の **Base URL** が `http://localhost:1234/v1`。これを `config.toml` の
     `base_url` に使う（ポートを変えたら合わせる）。
   - **「Just-in-time model loading」を ON** にしておくと、API リクエストで指定した
     モデルを LM Studio が自動でロードします。事前ロード不要になり、LLM と VLM を
     1 つずつ切り替えて使う（メモリ節約）運用と相性が良い。
   - ブラウザから叩くわけではないので **CORS は OFF のままで良い**。
4. `config.toml` に書くモデル ID を確認する:
   - **Local Models → Library** に表示されるモデルキー（例: `qwen2.5-7b-instruct`）を
     そのまま `model` / `vlm_model` に書く。
   - または `curl http://localhost:1234/v1/models` が返す `id` を見る。
   - Just-in-time loading を OFF にする場合は、使うモデルを先に
     **Local Models → Loaded Instances** でロードしておく。

> Ollama を使う場合: `ollama serve` が動いていれば `http://localhost:11434/v1` で
> OpenAI 互換 API が使えます。`config.toml` の `base_url` を変更してください。

## 4. 設定ファイル

```bash
cp config.example.toml config.toml
```

`config.toml` を開き、最低限これらを環境に合わせる:

```toml
[llm]
base_url = "http://localhost:1234/v1"
model = "<LM Studio でロードしたテキスト LLM の ID>"
vlm_model = "<LM Studio でロードした VLM の ID>"

[transcribe]
backend = "auto"          # Apple Silicon なら mlx（GPU）。"faster-whisper" に固定も可
model = "large-v3-turbo"  # 既定。精度優先なら "large-v3"、軽さ優先なら "medium" / "small"
```

## 5. 動作確認

短い動画（スライド提示のある 1〜2 分程度）で試します。

```bash
# CLI
python cli.py sample.mp4

# GUI
python gui.py
```

`output/sample/minutes.md` が生成されれば成功です。

## うまくいかないとき

| 症状 | 対処 |
| --- | --- |
| `ffmpeg が見つかりません` | `brew install ffmpeg`。venv を抜けても PATH は必要。 |
| `ローカル LLM サーバーに接続できません` | Settings → Local Model API の「Local API server」が **Running** か、`base_url` が合っているか確認。 |
| `model_not_found` / モデル未ロード | 「Just-in-time model loading」を ON にするか、Loaded Instances で該当モデルをロード。`config.toml` の名前が Library のモデルキーと一致しているか確認。 |
| `詳細: timed out`（議事録生成の途中で失敗） | サーバーは動いていて応答生成が長いだけ。特に議事録の最終統合は出力が長くタイムアウトしやすい。`config.toml` の `[llm] timeout` を増やす（既定600秒。大きいモデルはさらに）。 |
| 議事録生成が極端に遅い／「思考で max_tokens を使い切った」エラー／部分要約が空 | 使用中の LLM が推論（thinking）モデルの可能性。LM Studio で reasoning を OFF にするか、非推論の **Instruct 系モデル**に変更する（→ [`models.md`](models.md)）。`max_tokens` を増やしても速度問題は残る。 |
| 議事録が英語になる | `config.toml` の `[transcribe] language = "ja"`。LLM 側にも日本語対応モデルを使う。 |
| 文字起こしが遅い | Apple Silicon なら `[transcribe] backend = "auto"`（または `"mlx"`）で GPU を使う。`mlx-whisper` が入っているか（`pip show mlx-whisper`）確認。さらに `model` を `large-v3-turbo` / `medium` に。 |
| mlx で進捗バーが動かない | 仕様。mlx-whisper は結果を一括で返すため、完了まで 0 のまま。GUI のログに「mlx-whisper で文字起こし中」と出ていれば動作中。 |
| モデルのダウンロードに失敗する | ネット接続と `HF_HOME` を確認し `python prefetch.py` を再実行。未取得でも初回の文字起こし時に自動DLされる。 |
| フレームが多すぎる／少なすぎる | `[frames] interval_sec`・`scene_threshold`・`max_frames` を調整。 |
