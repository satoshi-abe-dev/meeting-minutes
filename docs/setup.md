# セットアップ手順

対応 OS: **macOS / Windows / Linux**。文字起こしは Apple Silicon の Mac では GPU 加速の
mlx-whisper、それ以外（Windows / Linux / Intel Mac）では CPU の faster-whisper を使う
（`config.toml` の `[transcribe] backend` は既定 `auto` で自動選択）。

> 開発・実機検証は macOS 中心。Windows / Linux は依存関係（`requirements.txt` の環境
> マーカーで mlx を自動スキップ）と CI（`ubuntu-latest` / `windows-latest` / `macos-latest`
> の 3 OS マトリクスで全テスト pass）レベルでは対応済みだが、フルパイプラインの実機
> 確認は未。

## 1. ffmpeg

| OS | インストール |
| --- | --- |
| macOS | `brew install ffmpeg` |
| Windows | `winget install ffmpeg`（`choco install ffmpeg` / `scoop install ffmpeg` も可。手動 zip 展開なら `bin/` を PATH に通す） |
| Linux | `sudo apt install ffmpeg`（Debian / Ubuntu。他ディストリは各パッケージマネージャ） |

インストール後、`ffmpeg -version` でパスが通っていることを確認する。

## 2. Python 環境 + モデル取得

Python 3.11 以上（動作確認は 3.14 系）。

**macOS / Linux** はワンコマンド:

```bash
cd meeting-minutes
bash scripts/setup.sh
```

**Windows**: WSL（Linux 環境）なら上の macOS / Linux 手順がそのまま使える。
ネイティブ Windows は `scripts/setup.sh` が使えない（中で `./.venv/bin/pip` など
POSIX パスをハードコードしており、`python -m venv` が作る `.venv\Scripts\` と噛み
合わない）ので、下記「### 手動でやる場合」の Windows（PowerShell）手順を使う。

`scripts/setup.sh`（および手動手順）が行うこと:

1. 仮想環境 `.venv` を作成
2. `pip install -r requirements.txt`
   - Apple Silicon の Mac では環境マーカーにより **mlx-whisper も一緒に入る**
     （GPU を使う文字起こしバックエンド）。Windows / Linux / Intel Mac では自動
     スキップされ faster-whisper だけになる。
3. `python src/meeting_minutes/download_transcribe_model.py` で **文字起こしモデルを取得**
   （`~/.cache/huggingface/hub/` に約 1.6GB、初回のみ。以降オフライン）。
   **この取得は必須**。失敗すると `scripts/setup.sh` はエラー終了する。アプリ実行時
   （`cli.py` / `gui.py`）は `HF_HUB_OFFLINE` で外部通信を止めるので、モデルが
   未取得でも自動ダウンロードはされず、文字起こしがエラーで停止する。

`config.toml` の `[transcribe] backend` は既定 `auto`: Apple Silicon の Mac は mlx、
**Windows / Linux / Intel Mac は faster-whisper（CPU のみ・遅め。`medium` /
`large-v3-turbo` を推奨）**。`mlx` / `faster-whisper` に固定もできる。既定モデルは
`large-v3-turbo`。

### 手動でやる場合（setup.sh を使わない）

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/meeting_minutes/download_transcribe_model.py   # モデル取得（必須。実行時は自動DLしない）
```

**Windows（PowerShell。venv の有効化は不要 — `.venv\Scripts\python` を直接呼ぶ）:**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python src\meeting_minutes\download_transcribe_model.py
```

> venv を有効化して短いコマンドで使いたい場合は `.venv\Scripts\Activate.ps1`。
> 実行ポリシーで弾かれるときは、そのセッションだけ許可する:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`
> （`cmd.exe` なら `.venv\Scripts\activate.bat`、こちらは実行ポリシーの影響を受けない）。

### モデルの置き場所と配布

モデルは **利用者のホームの共有キャッシュ**（`~/.cache/huggingface/hub/`、Windows は
`%USERPROFILE%\.cache\huggingface\hub\`。`HF_HOME` で変更可）に入る。リポジトリには
含めないので、**各利用者の初回セットアップ時に一度だけ
ダウンロード**が発生する（`scripts/setup.sh` がそれを済ませる）。アプリ実行時は
オフライン強制なので、このセットアップ時の取得が唯一のダウンロード機会。エアギャップ
環境では `HF_HOME` を社内ミラー/共有ストレージに向けるか、キャッシュを配布イメージに
含めたうえで、セットアップ時にモデルが揃っている状態にしておく。

## 3. ローカル LLM サーバー（LM Studio）

画面構成は LM Studio のバージョンで変わります。ここでは新しい UI（「Bionic」系。
サイドバーが Settings / Integrations / Devices / Local Models に分かれているもの）を
前提にします。旧 UI では「Developer」タブに同等の設定があります。

1. [LM Studio](https://lmstudio.ai/) をインストールして起動。
2. モデルを 2 つ用意する（詳しい選び方は [`models.md`](models.md)）:
   - テキスト LLM（議事録生成用）
   - VLM（フレーム解析用。vision / 画像対応のモデル。LM Studio の **Explore** で
     vision（画像対応）バッジが付くもの、または名前に `-vl` / `vision` を含むものを選ぶ）

   **迷ったら**: 16GB Mac ならテキスト LLM `qwen2.5-7b-instruct` ＋ VLM
   `qwen2-vl-2b-instruct`、24GB 以上なら VLM を `qwen2-vl-7b-instruct` に。
   メモリ別の一覧は [`models.md`](models.md) の「メモリ別のおすすめ構成」。

   入手は左メニュー **Local Models → Explore** から検索してダウンロード。
   ダウンロード済みは **Local Models → Library** で確認できます。
3. **テキスト LLM は Context Length を 32768 以上にしてロードする**（重要）:
   - LM Studio はモデル読み込み時に Context Length を指定しないと小さい既定値で
     読み込む。そのままだと議事録生成が **HTTP 400（context length 不足）で失敗**する。
   - モデルの読み込み設定 → **Context Length** を `32768` 以上にしてロードする。
     すでにロード済みなら一度 Eject して設定し直す。
   - **Just-in-time model loading** を使う場合も、そのモデルの読み込み設定
     （デフォルトの Context Length）を 32768 以上にしておく（一度手動でロードして
     設定するか、モデル設定の既定値を変更する）。
   - 詳細と、それでも収まらない場合の `config.toml` 側の対処は
     [`models.md`](models.md) の「コンテキスト長の設定（重要）」を参照。
4. ローカル API サーバーを起動する:
   - **Settings**（設定画面）を開く → 左メニュー **Local Models → Local Model API**
   - **「Local API server」** を ON（表示が **Running** になる）
   - 同じ画面の **Base URL** が `http://localhost:1234/v1`。これを `config.toml` の
     `base_url` に使う（ポートを変えたら合わせる）。
   - **「Just-in-time model loading」を ON** にしておくと、API リクエストで指定した
     モデルを LM Studio が自動でロードします。事前ロード不要になり、LLM と VLM を
     1 つずつ切り替えて使う（メモリ節約）運用と相性が良い。
   - ブラウザから叩くわけではないので **CORS は OFF のままで良い**。
5. `config.toml` に書くモデル ID を確認する:
   - **Local Models → Library** に表示されるモデルキー（例: `qwen2.5-7b-instruct`）を
     そのまま `model` / `vlm_model` に書く。
   - または `curl http://localhost:1234/v1/models` が返す `id` を見る。
   - Just-in-time loading を OFF にする場合は、使うモデルを先に
     **Local Models → Loaded Instances** でロードしておく。

> Ollama を使う場合: `ollama serve` が動いていれば `http://localhost:11434/v1` で
> OpenAI 互換 API が使えます。`config.toml` の `base_url` を変更してください。

## 4. 設定ファイル

**macOS / Linux:**

```bash
cp config.example.toml config.toml
```

**Windows:**

```powershell
copy config.example.toml config.toml
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

> テキスト LLM は §3 のとおり **Context Length を 32768 以上**にしてロード済みか確認する
> （未設定だと議事録生成が HTTP 400 で失敗する）。

> `python src/meeting_minutes/download_transcribe_model.py` は `config.toml`（無ければ
> `config.example.toml`）の `[transcribe] backend` / `model` を見て、その組み合わせの
> Whisper モデルを取得する。§2 の `setup.sh` は §4 の前に走るので既定
> （`large-v3-turbo`）は取得済み。**§4 で `model` / `backend` を既定から変えたら
> `python src/meeting_minutes/download_transcribe_model.py` を再実行**して取り直す
> （アプリ実行時は自動ダウンロードしない）。

## 5. 動作確認

短い動画（スライド提示のある 1〜2 分程度）で試します。

**macOS / Linux**（venv 有効化済み。未有効化なら `.venv/bin/python` を明示）:

```bash
python src/meeting_minutes/cli.py sample.mp4   # CLI
python src/meeting_minutes/gui.py              # GUI
```

**Windows**（venv 未有効化なら `.venv\Scripts\python` を明示）:

```powershell
.venv\Scripts\python src\meeting_minutes\cli.py sample.mp4
.venv\Scripts\python src\meeting_minutes\gui.py
```

`output/sample/minutes.md` が生成されれば成功です。venv を有効化しているか、
上のように venv 内の Python を明示すれば、どの OS でも同じコマンド構成で動きます
（Python はパス区切りに `/` も `\` も受けます。venv 未有効化のまま素の `python` で
呼ぶとグローバル環境が使われ ImportError になります）。

> 開発者向けには `python -m meeting_minutes.cli sample.mp4` / `-m meeting_minutes.gui`
> でも起動できます（その場合は `cd src` するか `PYTHONPATH=src` を設定してください）。

## うまくいかないとき

| 症状 | 対処 |
| --- | --- |
| `ffmpeg が見つかりません` | §1 の OS 別インストールを参照（`brew` / `winget` / `apt` など）。venv を抜けても PATH は必要。 |
| `ローカル LLM サーバーに接続できません` | Settings → Local Model API の「Local API server」が **Running** か、`base_url` が合っているか確認。 |
| `model_not_found` / モデル未ロード | 「Just-in-time model loading」を ON にするか、Loaded Instances で該当モデルをロード。`config.toml` の名前が Library のモデルキーと一致しているか確認。 |
| `詳細: timed out`（議事録生成の途中で失敗） | サーバーは動いていて応答生成が長いだけ。特に議事録の最終統合は出力が長くタイムアウトしやすい。`config.toml` の `[llm] timeout` を増やす（既定600秒。大きいモデルはさらに）。 |
| 議事録生成の開始直後に `HTTP 400`（`context length` 不足）で失敗 | テキスト LLM の Context Length が小さい。LM Studio で **32768 以上**にしてロードし直す（→ §3、[`models.md`](models.md)「コンテキスト長の設定（重要）」）。`timeout` 超過や推論モデルの空応答とは別の症状。 |
| 議事録生成が極端に遅い／「思考で max_tokens を使い切った」エラー／部分要約が空 | 使用中の LLM が推論（thinking）モデルの可能性。LM Studio で reasoning を OFF にするか、非推論の **Instruct 系モデル**に変更する（→ [`models.md`](models.md)）。`max_tokens` を増やしても速度問題は残る。 |
| 議事録が英語になる | `config.toml` の `[transcribe] language = "ja"`。LLM 側にも日本語対応モデルを使う。 |
| 文字起こしが遅い | Apple Silicon なら `[transcribe] backend = "auto"`（または `"mlx"`）で GPU を使う。`mlx-whisper` が入っているか（`pip show mlx-whisper`）確認。さらに `model` を `large-v3-turbo` / `medium` に。 |
| mlx で進捗バーが動かない | 仕様。mlx-whisper は結果を一括で返すため、完了まで 0 のまま。GUI のログに「mlx-whisper で文字起こし中」と出ていれば動作中。 |
| モデルのダウンロードに失敗する | ネット接続と `HF_HOME` を確認し `python src/meeting_minutes/download_transcribe_model.py` を再実行。アプリ実行時は自動DLしないので、ここで取り切る必要がある。 |
| 実行時に「文字起こしモデル（…）がローカルにありません」 | 事前取得が済んでいない。`bash scripts/setup.sh` か `python src/meeting_minutes/download_transcribe_model.py` を実行。`config.toml` の `[transcribe] model` / `backend` を途中で変えた場合も、その組み合わせのモデルを取り直す。 |
| フレームが多すぎる／少なすぎる | `[frames] interval_sec`・`scene_threshold`・`max_frames` を調整。 |
