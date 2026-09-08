# 推奨モデルと必要スペックの目安

環境（Mac のメモリ、Apple Silicon の世代）に合わせて選んでください。ここに挙げるのは
2025 年時点で入手しやすく日本語をそれなりに扱えるものの例です。**モデル名は LM Studio で
ロードしたときに表示される ID に合わせて `config.toml` に書く** 必要があります。

## 文字起こし

`model` はサイズ名で書く（`large-v3-turbo` / `large-v3` / `medium` / `small`）。
実体への変換はバックエンド（下記）が行う（`mlx` は `mlx-community/whisper-<size>` に、
`faster-whisper` はそのまま）。フル HF リポジトリ名を書けばそのまま使う。

| モデル | 目安 | 用途 |
| --- | --- | --- |
| `large-v3-turbo` | 約 1.6GB | **既定**。large-v3 に近い精度で高速。mlx で特に有効。 |
| `large-v3` | ディスク約 1.5〜3GB | 精度を最優先したいとき。 |
| `medium` | 約 1.5GB | 速度と精度のバランス。 |
| `small` / `base` | 数百 MB | 下書き・動作確認・非力なマシン。 |
| 4bit 量子化（`mlx-community/whisper-large-v3-mlx-4bit` 等） | 約 0.5GB | 初回DLを軽くしたいとき。日本語精度はわずかに落ちる可能性。 |

バックエンドは 2 つ。`config.toml` の `[transcribe] backend` で選ぶ（既定 `auto`）。

| backend | 実行先 | 速度 | 備考 |
| --- | --- | --- | --- |
| `mlx` | Apple Silicon の GPU | 速い | Mac 専用（`mlx-whisper`）。既定の `auto` は Apple Silicon でこれを選ぶ |
| `faster-whisper` | CPU（Mac の場合） | 遅い | どの OS でも動く。`auto` のフォールバック |

faster-whisper のとき、`compute_type` は CPU なら `int8`、`device = "auto"` で任せる。
これらは `mlx` バックエンドでは無視される。

### モデルの取得と置き場所

- モデルは `~/.cache/huggingface/hub/`（`HF_HOME` で変更可）に入る。リポジトリには
  含めない。
- `scripts/setup.sh`（内部で `python src/meeting_minutes/prefetch.py`）がセットアップ時に既定モデルを
  取得する。取得済みなら何もしない（冪等）。取得は必須で、失敗すると setup.sh は
  エラー終了する。
- アプリ実行時（`cli.py` / `gui.py`）はオフライン強制のため、モデルが未取得でも
  自動ダウンロードされない。未取得のまま文字起こしを始めると「文字起こしモデル
  （…）がローカルにありません」で停止する。`bash scripts/setup.sh` か
  `python src/meeting_minutes/prefetch.py` で取得すること。
- **利用者ごとに 1 台につき一度だけ**、セットアップ時にダウンロードが発生する。

## フレーム解析（VLM）

| 例 | 必要メモリ（目安） | 備考 |
| --- | --- | --- |
| `qwen2-vl-7b-instruct` | 10〜16GB | 日本語のスライド文字にも比較的強い。 |
| `qwen2-vl-2b-instruct` | 6〜8GB | 軽量。要点だけ拾えれば十分な場合。 |

VLM を使わず OCR だけで済ませたい要望が出たら、`vision.describe_frames` を
差し替える形で対応できます（現状は VLM 前提）。

## 議事録生成（テキスト LLM）

### モデル選びの前提（重要）

**推論（thinking / reasoning）をしないモデル、または推論をオフにできるモデルを
選んでください。** 推論モデルをそのまま使うと、見えない「思考」に大量のトークンと
時間を使い、**極端に遅くなる／議事録の本文が空で返る**ことがあります
（実測: 推論モデルで 1 チャンクあたり 7 分超、しかも本文が空）。

- Qwen3 系（`qwen3-*`）: LM Studio の推論設定で reasoning を OFF にできれば可。
  できない構成なら避ける。
- DeepSeek-R1 系: 推論を無効化できないので避ける。
- **推奨は素直な Instruct 系**（`qwen2.5-7b-instruct` など）。
- 空応答になった場合は「思考で max_tokens を使い切った」というエラーが出るので、
  そこで気づけます（`config.toml` の `max_tokens` を増やしても速度問題は残ります）。

### サイズの目安

| クラス | 例 | 必要メモリ（4bit 量子化の目安） |
| --- | --- | --- |
| 7〜8B | `qwen2.5-7b-instruct`、`llm-jp-3-7.2b-instruct`、`elyza-japanese-llama` 系 | 8〜12GB |
| 14B 前後 | `qwen2.5-14b-instruct` | 12〜20GB |
| 32B 前後 | `qwen2.5-32b-instruct` | 24GB 以上 |

議事録は「決まった型を埋める」タスクなので、7〜8B でも実用になります。決定事項の
取りこぼしや表記の乱れが気になる場合は 14B 以上を検討してください。

### コンテキスト長の設定（重要）

（[`setup-mac.md`](setup-mac.md) §3 でこの設定を促しています。ここはその詳細版です。）

議事録生成は、文字起こし全体＋フレーム要点をできるだけ **1 回のリクエスト**で
LLM に渡す（分割すると「要約の要約」になり具体性が落ちるため）。
LM Studio は **モデルをロードするときに Context Length を明示しないと小さい既定値**
で読み込むため、そのままだと次のエラーで議事録生成が失敗します:

```
LLM サーバーがエラーを返しました (HTTP 400):
{"error":"The number of tokens to keep from the initial prompt is greater than
the context length. Try to load the model with a larger context length,
or provide a shorter input"}
```

**対処: LM Studio でこの LLM をロードする際、Context Length を 32768 以上にする。**
モデル読み込みバー → 対象モデル → 設定の Context Length を 32768 以上にして
ロード。すでにロード済みなら一度 Eject してから設定し直す
（Qwen2.5-7B / 14B はいずれも 32k 以上に対応）。

**ツール側の自動調整:** このツールは LM Studio の `/api/v0/models` からロード中
モデルの実コンテキスト長を取得し、文字起こし＋フレーム要点＋応答予約が収まらないと
推定した場合は、自動で分割生成（チャンク要約 → 統合）に切り替えます。フレーム解析
結果が大きすぎる場合はコンテキストの約 1/3 までに切り詰めます。トークン数の見積もりは
Qwen 系トークナイザでの日本語の実測（**約 0.75 トークン/文字**、安全側に 0.8 で計算）
に基づきます。

**それでも収まらない／LM Studio 以外の基盤の場合:** `config.toml` の
`[llm] chunk_trigger_chars` と `chunk_size_chars` を小さくする（既定 20000 / 12000。
例: `chunk_trigger_chars = 8000` / `chunk_size_chars = 6000`）。実コンテキスト長が
自動取得できない基盤では、`[llm] context_tokens` に実値（例: 32768）を書くと
トークンベースの判定が効きます。

## メモリ別のおすすめ構成

| Mac のメモリ | 文字起こし（backend=auto なら mlx） | VLM | LLM |
| --- | --- | --- | --- |
| 16GB | `medium` or `large-v3-turbo` | 2B VLM（または VLM を使わない運用） | 7〜8B |
| 24GB | `large-v3-turbo` or `large-v3` | `qwen2-vl-7b` | 7〜8B |
| 32GB 以上 | `large-v3` | `qwen2-vl-7b` | 14B |

LLM と VLM を **同時にロードしておく** と切り替えが速いですが、その分メモリを食います。
1 つずつロードする運用なら、上表より少ないメモリでも回せます。

新しい LM Studio の **「Just-in-time model loading」を ON**（Settings → Local Model API）に
しておくと、API 呼び出し時に `config.toml` で指定したモデルを自動でロード／切り替えして
くれるため、工程ごとの手動ロードは不要です。メモリが厳しい環境ではこれを使い、
VLM と LLM を必要なときだけ入れ替える運用が楽です。
