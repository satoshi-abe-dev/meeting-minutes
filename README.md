# meeting-minutes

**打ち合わせの録画動画を渡すと、議事録（Markdown）が返ってくる。処理はすべて手元の PC で完結し、音声も文字起こしも要約も外部に送信しない。**

<!-- badges: CI バッジはフェーズ2で追加予定 -->

---

## 課題

会議のたびに、誰かが録画を見返して議事録を手で書いている。これを自動化したいが、
既存のクラウド文字起こし・要約サービスには乗せられない現場がある：

- 会議に **顧客情報・未公開の経営情報・人事情報・個人情報** が含まれる
- NDA・個人情報保護法・GDPR・社内規程・業界規制で、**録音データを外部送信できない**
- 「便利だが情報が外に出る」ツールは、そもそも導入審査を通らない

## これは何か

動画ファイルを 1 つ渡すと、次を **すべてローカルで** 実行して議事録を生成する CLI / GUI ツール。

1. 動画から音声を抽出（ffmpeg）
2. 時刻付きで文字起こし（faster-whisper。ローカル・オフライン）
3. 画面共有・スライドのフレームを抽出し、内容を要点化（ローカル VLM）
4. 文字起こし＋フレーム要点から議事録を生成（ローカル LLM）

外部ドメインへ接続するコードは無い。唯一の通信先は **自分で立てるローカル LLM サーバー**
（[LM Studio](https://lmstudio.ai/) など）への `localhost` 呼び出しのみ。モデルの
ダウンロードさえ済ませれば **Wi-Fi を切っても最後まで動く**（→ [`doc/privacy.md`](doc/privacy.md)）。

出力は `output/<動画名>/` に:

```
transcript/audio.wav                抽出した音声（文字起こしと同じフォルダーにまとめる）
transcript/transcript.json          時刻付き文字起こし（構造化）
transcript/transcript.txt           時刻付き文字起こし（読みやすいテキスト）
frames/*.jpg                        抽出フレーム
frames/frames.json                  抽出フレームの索引（途中再開に使う）
frames/frame_notes.json             フレームごとの解析結果（途中再開に使う）
minutes_partials.json               議事録のチャンク要約（長い文字起こしの場合。途中再開に使う）
minutes.md                          議事録（決定事項・宿題/担当・期限などの型で整理）
```

本プロジェクトは、Claude Code の複数セッション（実装担当・レビュー担当）を
役割分担・レビュー体制つきで協調運用して開発した（→ [開発体制](#開発体制ai協調開発)）。

## デモ

<!-- TODO(フェーズ2): 非機密の動画を処理し、GUI スクリーンショットと examples/<name>/minutes.md を追加してここから参照する。処理中の実会議の情報は載せない。 -->

_準備中。非機密のサンプル動画で生成した議事録とスクリーンショットを近日追加します。_

## 仕組み

```
動画 ─▶ 音声抽出 ─▶ 文字起こし ─▶ フレーム抽出 ─▶ フレーム解析(VLM) ─▶ 議事録生成(LLM) ─▶ minutes.md
       (ffmpeg)    (faster-whisper)   (ffmpeg)        (localhost)         (localhost)
```

`pipeline.run()` がこの順序・進捗通知・出力ディレクトリ管理を担当し、**GUI・CLI・テストは
すべて `run()` を呼ぶだけ**。各工程の実装は `pipeline.Deps` 経由で差し替えられる。
詳細は [`doc/architecture.md`](doc/architecture.md)。

## セットアップ（macOS / Apple Silicon）

```bash
brew install ffmpeg          # 音声・フレーム抽出に必要
bash scripts/setup.sh        # 仮想環境の作成・依存導入・文字起こしモデルの取得まで自動
cp config.example.toml config.toml
```

`scripts/setup.sh` が仮想環境（`.venv`）を作り、依存をインストールし、文字起こしモデルを
HuggingFace から取得する（初回のみ、以降オフライン）。利用者が打つのはこの 1 コマンドだけ。

別途、議事録・フレーム解析用に **LM Studio** を起動しておく（Settings → Local Models →
Local Model API で「Local API server」を ON、「Just-in-time model loading」も ON 推奨）。
**議事録生成の LLM は、推論（thinking）をしない／推論をオフにできる Instruct 系モデルを
選ぶこと**（推論モデルは極端に遅く、本文が空で返ることがある → [`doc/models.md`](doc/models.md)）。
詳しい手順・モデル選び・トラブルシューティングは [`doc/setup-mac.md`](doc/setup-mac.md) と
[`doc/models.md`](doc/models.md)。

## 使い方

```bash
python gui.py                       # GUI: 動画を選んで「議事録を作成」。進捗バーとログ表示
python cli.py 打ち合わせ.mp4         # CLI: 動作確認・自動化用
```

## 設計のポイント

- **1 つの継ぎ目（`pipeline.run` + `Deps`）** — UI と実処理を分離。ffmpeg も LLM も
  呼ばずに「工程順序・進捗・出力パス」をテストできる。
- **LLM/VLM を OpenAI 互換 API で抽象化** — `base_url` の差し替えだけで LM Studio /
  Ollama / 他に交換可能。`openai` パッケージには依存せず `httpx` 直叩き。
- **長い文字起こしの map-reduce** — 1 時間の会議はコンテキストに収まらないので、
  チャンク要約 → 統合の 2 段に切り替える。チャンク要約は `minutes_partials.json` に
  逐次保存され、タイムアウトや中断のあとの再実行では終わった分をやり直さない。
- **各工程の所要時間をログに表示** — 音声抽出・文字起こし・フレーム抽出・フレーム解析・
  チャンク要約1つずつ・議事録統合それぞれの完了メッセージに「所要 X」を添える。
- **フレーム解析も1枚単位で中間ファイル化** — `frames/frame_notes.json` を1枚終えるたびに
  書き直すので、途中で失敗しても解析済みのフレームはやり直さない。
- **LLM呼び出し中は「応答を待っています」を表示** — 文字起こし以外の待ち時間が
  読める工程（フレーム解析・議事録生成）では、使用モデル名とあわせて表示する。
- **文字起こしバックエンドを差し替え可能に** — `backend=auto` で Apple Silicon なら
  GPU を使う mlx-whisper、それ以外は faster-whisper。`config.toml` で固定もできる。
- **設定の多層化** — デフォルト < `config.toml` < 環境変数。TOML は標準 `tomllib` で依存ゼロ。

判断の理由とトレードオフは [`doc/DESIGN.md`](doc/DESIGN.md)、
想定質問と回答は [`doc/interview-qa.md`](doc/interview-qa.md)。

## 開発体制（AI協調開発）

本プロジェクトは Claude Code の複数セッションによる協調開発で実装した。

- **worker** — 実装・テスト・git 操作を担当
- **manager** — worker が発行した PR をレビューし、`main` へのマージを担当。
  機密混入（実会議の固有名詞）・`.gitignore` の除外設定・差分が意図した範囲内か・
  破壊的操作の有無を確認した上でマージする
- 役割分担・禁止事項・レビュー基準は
  [`doc/ai-workflow/SESSION_RULES.md`](doc/ai-workflow/SESSION_RULES.md) に明文化
- **manager は worker の自己申告を鵜呑みにしない** — 全 PR で pytest・機密 grep を
  manager 自身が再実行し、diff を直接確認した上でマージする
- **セッション間で会話コンテキストは共有されない** — manager は worker の試行錯誤の
  過程を見ず、最終的な diff と報告のみからレビューする
- **権限境界は実際に機能した** — 追跡ファイルの削除など本人の直接確認が必要な操作では、
  worker は manager 経由の伝達だけでは実行せず、本人への確認を待って保留した実例がある
- **異なるベンダーのモデルによる独立レビューも組み込んだ** — Claude（manager）の判断に加え、
  OpenAI Codex（`codex exec review`）による独立コードレビューを全 PR のマージ前チェックに
  追加し、実際に運用している

実会議データを扱うプロジェクトの性質上、push・PR前に追跡ファイルへの機密混入を
grep で確認する手順を徹底している。

## テスト

```bash
pip install -r requirements-dev.txt
pytest        # 17 本。ffmpeg 実行の統合テストを含む（ffmpeg / LLM が無い環境では自動 skip）
```

## 既知の限界と次の一手

| 限界 | メモ |
| --- | --- |
| 文字起こしが遅い場合 | `backend=auto` で Apple Silicon は GPU の mlx-whisper を使う。`backend=faster-whisper` に固定すると Mac では CPU のみで `large-v3` は 1 時間会議に数十分。`medium` / `large-v3-turbo` でさらに短縮 |
| mlx では進捗が動かない | mlx-whisper は結果を一括で返すため、文字起こし中の進捗バーは 0 のまま完了時に一気に進む（faster-whisper は逐次更新） |
| 歌・BGM区間の誤認識 | 同一フレーズを延々と繰り返す幻覚は `condition_on_previous_text=False` で対策済み。ただし単発の空耳（1行だけ変な内容）はWhisper系全般の既知の癖で残りうる。歌・拍手が多い会議は文字起こしの目視確認を推奨 |
| 話者分離なし | 「誰が話したか」のラベルは付かない（`参加者` 表記） |
| 推論モデルは議事録生成に不向き | Qwen3 / DeepSeek-R1 等の推論（thinking）モデルは見えない「思考」にトークンと時間を大量消費し、極端に遅い／本文が空で返る。**非推論の Instruct 系を使うこと**。空応答時は「思考で max_tokens を使い切った」というエラーで気づける |
| 途中から再開 | 文字起こし・フレーム抽出・**フレーム解析（1枚単位）**・**議事録のチャンク要約（1つ単位）**のすべてで、既存の中間ファイルを再利用して**やり直した分だけ再実行**（`--fresh` / GUI のチェックで無効化）。処理開始前に LLM サーバーとモデルの疎通を確認して**早めに失敗**する |
| 中断は完全には即時でない | GUI の「中断」はステージ／フレーム解析の1枚ごと／議事録のチャンクごとの節目で反応する。ただし **mlx-whisper の文字起こし呼び出し中** と **ffmpeg 実行中** は途中で打ち切れない（その工程が終わるまで待つ） |
| GUI から設定変更不可 | `config.toml` を直接編集する |

CI・lint/型チェック・中間結果の再利用・LLM 落ち時のリトライは次フェーズで対応予定。

## 設定

`config.toml`（無ければ `config.example.toml`）で調整。主な項目:

- `[llm] base_url` — ローカルサーバーの接続先（Ollama なら `http://localhost:11434/v1`）
- `[llm] model` / `[llm] vlm_model` — ロード済みモデル名
- `[transcribe] backend` — `auto` / `mlx`（Apple GPU）/ `faster-whisper`
- `[transcribe] model` — 既定 `large-v3-turbo`（速い・高精度）/ `large-v3` / `medium` / `small`
- `[frames] interval_sec` / `scene_threshold` / `max_frames` — フレーム抽出の粒度と上限
- `[output] template_path` — 議事録の様式（見出し構成）を差し替えるカスタムテンプレート（下記）

各値は環境変数（`MM_LLM_MODEL` など）でも上書き可能。

### 議事録テンプレートを差し替える

お客様ごとの規定様式に合わせたいときは、議事録の**構造**（見出し・項目）を外部ファイルで
指定できる。**「捏造しない」等の品質ルールはテンプレートに関わらず常に適用される**
（テンプレートはあくまで構造）。一発生成・分割生成のどちらでも同じテンプレートが使われる。

- `config.toml` の `[output] template_path` にパスを書くと、毎回そのテンプレートが使われる
  （GUI の設定表示にも「議事録テンプレート」として表示される）
- ファイルが見つからない・読めない・空の場合は、**内蔵テンプレートにフォールバックし警告**する

テンプレートで使えるプレースホルダー:

| プレースホルダー | 差し込まれる内容 |
| --- | --- |
| `{title}` | 動画ファイル名（拡張子なし） |
| `{datetime_hint}` | 日時のヒント（不明なら「（記載なし）」） |
| `{duration_hint}` | 記録時間のヒント（「約 N 分」等） |
| `{transcript}` | 文字起こし全文（**省略可**。テンプレートに書かなければ、その分だけ末尾に自動追加される） |
| `{frames}` | フレーム解析結果（時刻付き。**省略可**。`{transcript}` とは独立に、書かなかった方だけ補われる） |

雛形は [`prompts/minutes_template_example.txt`](prompts/minutes_template_example.txt)（内蔵と同一）。
これをコピーして見出しを書き換えるのが早い。
