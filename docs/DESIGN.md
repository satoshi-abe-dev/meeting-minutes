# 設計の判断と理由

このドキュメントは「なぜこの形にしたか」を残すもの。実装の説明は
[`architecture.md`](architecture.md) を参照。

---

## 1. なぜ「完全ローカル」なのか

- **判断**: 音声・映像・文字起こし・要約のすべてをマシン内で処理。外部ドメインへの
  接続コードは持たない（LLM/VLMは`localhost`のローカルサーバーへ送るのみ）
- **理由**: 想定ユーザーは、会議に顧客情報・未公開情報・個人情報が含まれ、クラウド
  のSTT/生成AIに録音を送れない現場。「精度が少し落ちても外に出ないこと」が
  機能要件そのもの
- **トレードオフ**:
  - ローカルLLMは日本語要約の品質がクラウド最上位モデルに劣る
  - `large-v3`の文字起こしはMacではCPU実行で遅い
  - 導入側にそれなりのRAMが要る

  これらは「外部送信しない」と引き換えに受け入れる前提。

---

## 2. パイプラインの継ぎ目：`pipeline.run()` + `Deps`

- **判断**: 全工程を `pipeline.run(video_path, config, on_progress, *, deps)` の1関数に
  集約。各工程の実装は `Deps` という dataclass の関数フィールドとして注入し、既定は
  本物、テストではダミーを渡す
- **理由**:
  - GUI・CLI・テストが同じエントリを共有でき、工程の並び順や出力パスの組み立てが
    1か所にしかない
  - ffmpegもLLMサーバーも用意せずに「工程が正しい順で呼ばれるか」「進捗コールバックの
    中身」「`output/<動画名>/` の組み立て」を検証できる（`tests/test_pipeline.py`）
  - dataclassの `__init__` が各関数をインスタンス属性へ代入するので、関数を直接
    デフォルトに置いても記述子（bound method）化されず、素直に差し替えられる
- **不採用にした案**:
  - モック無しの結合テストだけ: ffmpegとローカルLLMが必須になり、CIに載せづらく
    失敗の切り分けもしにくい
  - 各工程を抽象基底クラス＋サブクラスで差し替え: 工程は今のところ関数1個で足り、
    クラス階層はオーバースペック。関数フィールドのdataclassで十分

---

## 3. 進捗通知：`on_progress(stage, current, total, message)`

- **判断**: 全工程が同じシグネチャのコールバックを呼ぶ。UIの知識は持たせない
- **理由**: CLIはこれをテキスト行に、GUIは `queue.Queue` に載せてUIスレッドへ渡す、と
  受け手を差し替えられる。tkinterはワーカースレッドから直接ウィジェットを触れないため、
  「進捗はデータとして渡し、UI反映はUIスレッド側でやる」という分離が必要だった
- **トレードオフ**: `total` は正確な値ではなく見積もり（例: 文字起こしのセグメント数は
  `動画長 ÷ 4秒`）。進捗バーは「だいたい」の表示になる。正確さより「止まって見えない」
  ことを優先した

---

## 4. LLM/VLM を OpenAI 互換 API で抽象化

- **判断**: `llm_client.py` はOpenAI互換の `/v1/chat/completions` を `httpx` で直接叩く。
  接続先は `config.ai.base_url` の1か所だけ
- **理由**:
  - LM Studio・Ollama・その他のOpenAI互換サーバーを、`base_url` の変更で交換できる。
    特定のGUIアプリにロックインされない
  - `chat`（テキスト）と `describe_image`（画像をdata URLで送る）の2メソッドで足り、
    レスポンスは `choices[0].message.content` を読む。`openai` パッケージを足すほどの
    複雑さが無い。依存は薄いほどよい
- **不採用にした案**: `openai` パッケージ経由。動くが、この用途では依存とバージョン
  制約が増えるだけで得るものが少ない

**タイムアウトは「サーバーが死んでいる」と「応答が長い」を区別する:**

- 不具合: 議事録の最終統合（部分要約より出力トークン数が多い）が既定タイムアウト
  180秒を超えて失敗する
- 原因: サーバーの不調ではなく、単に大きいモデルの生成に時間がかかっただけ
- 対処: `httpx.TimeoutException`（`RequestError` の一種）を先にcatchし、「サーバーを
  起動してください」ではなく「`timeout` を増やしてください」という専用メッセージを
  返すようにした。あわせて既定 `timeout` を180→600秒に引き上げた

**推論（reasoning）モデルは議事録生成に使わない（前提条件）:**

- 不具合: `qwen/qwen3.8-27b`（Qwen3系の推論モデル）で、チャンク要約が
  `### 部分 N` の見出しだけ・本文が空で保存される
- 原因: 推論モデルが見えない思考を `message.reasoning_content` に出力し、
  `max_tokens` をそこで使い切って可視の `message.content` が空のまま
  `finish_reason=length` で返ってくる（HTTPは200なので気づきにくい）
- 実測: 約5000文字のチャンクに思考4129文字、成功時433秒/チャンクと実用外
- 対応:
  - `llm_client` で「`content` 空 かつ `reasoning_content` あり」を検知し、「思考で
    max_tokensを使い切った。増やすかreasoningを下げて」という明示エラーに
  - チャンク要約の `max_tokens` を固定1500→`config.ai.max_tokens` に
  - `minutes._load_partials` は本文が空のエントリを無効化（壊れた
    `work/minutes_partials.json` を再実行時に自動で作り直す）
  - 既定 `max_tokens` を4096→8192に
- 根本原因は「重い推論モデルを議事録生成に使っている」ことなので、**非推論の
  Instruct系モデルを使うことを前提条件としてドキュメント化**した（`docs/models.md`）。
  `/no_think` を自動付与する案もあったが、Qwen3専用の書き方で他社の推論モデルには
  効かずモデルごとに切り方が違うためコードには入れない

---

## 5. 長い文字起こしの map-reduce（`minutes.py`）

- **判断**: 文字起こしテキストが閾値（`_CHUNK_TRIGGER_CHARS`）を超えたら、セグメントを
  チャンクに分割 → 各チャンクを箇条書きに要約 → その要約群＋フレーム要点で最終議事録を
  生成、という2段階に切り替える。短ければ1回の呼び出しで済ませる
- **理由**: 1時間の会議の文字起こしは、ローカルLLMの実用的なコンテキスト長に収まらない
  ことが多い。丸ごと渡して切り詰められるより、段階要約のほうが決定事項の取りこぼしが
  減る
- **トレードオフ**: LLM呼び出し回数が増え、時間がかかる。チャンク要約で細部が落ちる
  こともある。閾値は「収まるうちは1回で」を狙った経験的な値で、モデルを変えたら調整の
  余地がある

**閾値の更新（2026-09）:** 実運用で `_CHUNK_TRIGGER_CHARS=12000` は小さすぎ、1〜2時間級
でない普通の会議まで分割されて「要約の要約」から議事録が作られ、具体性が大きく落ちて
いた。LLMを32kコンテキストで運用する前提に合わせて既定を40000/15000に引き上げ、
**収まる限り一発生成**、非常に長い会議だけmap-reduceにフォールバックする方針にした。

**この前提は文書化と設定化が必要だった（2026-09、Issue #16）:** 「32kコンテキスト前提」
とコード内コメントに書いただけで `docs/models.md` に設定手順が無く、LM Studioで
Context Lengthを上げていないユーザーが一発生成時にHTTP 400（context length不足）を
踏んだ。対応:

- `docs/models.md` にContext Lengthを32768以上にする手順を明記
- `llm_client` がこの400を検出して原因の分かる日本語ヒントに変換
- 閾値を `config.toml` の `[ai] chunk_trigger_chars` / `chunk_size_chars` で
  調整可能にし、小さいコンテキストのモデルでも下げて分割モードで回せるように
  した（`minutes.py` の `_CHUNK_TRIGGER_CHARS` / `_CHUNK_SIZE_CHARS` はその既定値）

**文字数しきい値そのものが甘かった（2026-09、Issue #18）:** Context Lengthを32768に
正しく設定しても、既定 `_CHUNK_TRIGGER_CHARS=40000` のままだと一発生成が上限を超えて
再びHTTP 400になった。原因の切り分け:

- Qwen系トークナイザで日本語は約 **0.74トークン/文字**（実測。コメントの「1〜1.5」は
  過大）。40000字 ≈ 3万トークン
- 加えて **frames_text（フレーム解析の連結）が支配的**だった。実ケースで60枚 ≈
  1.8万トークン。transcriptより大きい
- `max_tokens=8192`（推論モデル対策の既定）をそのまま応答予約に使っていた

対応（`minutes.py` / `llm_client.py` / `pipeline.py`）:

- `pipeline` がLM Studioの `/api/v0/models` から **ロード中モデルの実コンテキスト
  長**（`loaded_context_length`）を取得し、`generate_minutes(context_tokens=...)` へ渡す
- `generate_minutes` は、実コンテキスト長が分かるときは
  `概算プロンプトトークン + 応答予約 + マージン <= context_tokens` で一発 / 分割を
  判断する（文字数しきい値は取れないときのフォールバックに降格。既定20000/12000）
- `frames_text` はコンテキストの約1/3（下限6000トークン）に切り詰める
- 議事録本文の応答予約は `max_tokens` ではなく `_MINUTES_RESPONSE_TOKENS`（5000）で
  頭打ちにする（型を埋めるタスクなので十分。推論モデルは非対象）
- 取れないときのため `[ai] context_tokens` で実値を直接指定もできる

実測に基づくテストを `test_minutes.py` / `test_llm_client.py` に追加。

**チャンク要約は1つ終えるたびに `work/minutes_partials.json` へ保存する:**

- 不具合: チャンク要約が2つとも終わったあとの最終統合（出力トークン数が多い）だけが
  タイムアウトする事例があった
- 問題点: チャンク要約自体は無事終わっているのに、`generate_minutes()` は1回の
  呼び出しなので失敗すれば全部やり直しだった
- 対処: `_save_partials()` で完了ぶんを都度ディスクに書き、`reuse=True`（既定）での
  再実行時は `_load_partials()` で読み戻し、終わっているチャンクを要約し直さない
- `out_dir` を渡さない呼び出し（テスト等）では、この永続化自体が無効になるだけで
  動作は変わらない

**保存ファイルにチャンク境界のシグネチャを持たせる（2026-09、Codexレビュー指摘）:**

- `work/minutes_partials.json` は `chunk_size_chars` と分割入力のセグメント総数を
  一緒に保存する（`format: 2`）
- 再開時にこれが現在の設定と一致しなければキャッシュを破棄して最初から要約し直す
- これが無いと、Context Length対策で `chunk_size_chars` を下げて再開したとき、旧境界の
  部分要約が新しいチャンク列に件数だけで採用され、議事録の内容がエラーなく重複・欠落
  していた
- メタ情報の無い旧形式（JSON配列）も境界を検証できないため採用しない

**議事録の「構造」を外部テンプレートで差し替え可能に（2026-09、Issue #21）:**

- お客様ごとの規定様式に対応するため、`_MINUTES_TEMPLATE` を「構造
  （`_MINUTES_STRUCTURE`）」と「入力セクション（`_INPUT_TRANSCRIPT` / `_INPUT_FRAMES`）」
  に分割
- `config.toml` の `[output] template_path` で構造を差し替える
  （`generate_minutes(template_path=...)` へは `pipeline` がconfigから渡す）
- 実行時の一時上書き経路は、当初GUIボタン＋CLI `--template` で付け → 一度削除し →
  最終的にGUIの「議事録フォーマット」ドロップダウン（`ttk.Combobox`）として復活させた
- 先頭固定項目が `config.toml` の設定（`内蔵（既定）` またはファイル名）、「ファイルを
  選択...」でその回だけ差し替え、先頭項目を選び直せば戻る。CLI側の一時上書きは
  復活させていない（`config.toml` で足りる）
- ユーザー向け用語は「内蔵（既定のフォーマット）」で統一し、ツールチップで「会議内容で
  動的に変わらない固定の構成」であることを補足する
- **システムプロンプト（`prompts/minutes_ja.txt` の捏造禁止等）はテンプレートに関わらず
  常に適用**。テンプレートは構造だけで、品質ルールを客先様式に巻き込ませない
- 差し込みは逐次 `.replace` でも `.format` でもなく、テンプレートを1回だけ走査する
  一括置換（`re.sub` + コールバック、`_fill_minutes_template`）。置換後の値
  （transcript等）は再走査しないので、文字起こし中に偶然 `{frames}` のような文字列が
  あっても巻き込まれない。既知プレースホルダー名でない素の `{ }`（JSON例など）は不変
- テンプレが `{transcript}` / `{frames}` を書いていなければ **足りない方だけ** を末尾に
  補う（`{transcript}` は書いたが `{frames}` を忘れてもフレーム情報が消えない）。客先は
  出力様式だけ書けばよい
- ファイルが無い・読めない・空 → エラーで止めず内蔵にフォールバックし `on_progress` で
  警告（`load_minutes_structure`）
- 一発生成・分割生成の統合ステップの両方で同じ `structure` を使う

**GUIのフォーマット選択はラジオ3択に（2026-09、Issue #34 PR-B）:**

- 課題: 上記の `ttk.Combobox` は「選択済み項目がハイライトされたままで選び直しにくい」
  「幅が中身に合わない」問題があった
- 対応: `ttk.Radiobutton` の排他3択（内蔵（既定）／ファイルを選択／おまかせ）に
  置き換えた
- `_start()` で `output.auto_structure` と `output.template_path` を **毎回** ラジオの
  状態から設定する（`config.toml` で `auto_structure=true` でもGUIの明示選択が優先。
  3ラジオが排他的に1状態を表す）

**「おまかせ」モード（2026-09、Issue #34）:** 動画の内容に合わせて見出し構成そのものを
LLMに提案させる3つ目の選択肢。`config.toml` の `[output] auto_structure`（優先順位
`auto_structure` > `template_path` > 内蔵）。

- **トークン予算を再燃させない**（Issue #16/#18/#19と同種の再発を防ぐ）。構造生成は
  1回だけ。文字起こし全文が「構造生成用の軽い予算」（応答予約は議事録本文と同じ
  `minutes_max_tokens`）に収まればそのまま材料に、収まらなければ **既存のmap-reduce
  チャンク要約を再利用**（`_summarize_chunks` は最大1周。新たな全文読み込みパスは
  足さない）。長いmaterialは `_fit_structure_material` で構造生成予算に切り詰める
- **生成物はプレースホルダーをリテラルのまま出力させる**（プロンプトで明示）。
  `output/<動画名>/work/structure_used.txt` に保存し、そのまま `templates/` にコピー
  して固定テンプレート化できる
- **フォールバックは常に内蔵**（`_MINUTES_STRUCTURE`。ファイル指定時も内蔵）。失敗
  条件: LLM例外／空応答／必須プレースホルダー（`{title}` `{datetime_hint}`
  `{duration_hint}`）欠落／構造単体で統合予算を超える
  （`_structure_fits_minutes_skeleton`。分割に切り替えても救えないため棄却）。
  失敗時は `work/structure_used.txt` を残さない（前回実行の残骸も消す）
- 構造を差し替えた **後** に予算（`one_pass` / `frames_text` / `size_chars`）を
  `_minutes_budget` で計算し直す。統合直前には実際の `merged_transcript` ＋ frames を
  含めた予算を確認し、超える分は `_fit_merged_transcript` で末尾を切り詰める
- 構造生成（重いLLM呼び出し）の直後にも `check_cancel` を入れる

**議事録は `minutes.md` と `minutes.docx` の両方を常に出力する（2026-09、Issue #13）:**
Wordで開きたい・そのまま配布したいという要望。GUIに選択機能は付けず、常時両方出す
（利用者が迷わない）。変換は新規モジュール `model/docx_export.py`。

- **`pandoc` 等の外部バイナリは使わない**。`python-docx`（Pure Python）だけを依存に
  足す。連れてくる `lxml`・`typing_extensions` も全OSプリビルドwheelで、CIは3OSとも
  `pip install` で完結し、システム依存は増えない
- **汎用Markdownパーサーは入れない**。議事録が実際に取るMarkdownサブセット（ATX見出し
  / `- `・`* ` の箇条書き（ネスト）/ GFMパイプ表 / フェンス / `**bold**`・`` `code` ``）
  だけを行ベースの小さなstate machineで変換する。**未知の行は素の段落として落とす**
  ので、「おまかせ」やカスタムテンプレートで構成が変わっても壊れない。変換自体は
  例外を投げない
- **`.md` が主成果物**。`pipeline.py` は `save_minutes` の直後に `save_minutes_docx`
  を `try/except` で呼び、変換・書き出しが失敗しても警告（`pmsg.warn_docx_failed`）を
  出して続行する。`.docx` は `output/<動画名>/minutes.docx`（中間物ではないので
  `minutes/` 配下ではなくルート）

---

## 6. 文字起こしバックエンド（faster-whisper / mlx）

- **判断**:
  - `transcribe.py` は2実装を持ち、`config.backend` で選ぶ
  - `transcribe_wav()` は薄いディスパッチャで、`resolve_backend()` の結果に応じて
    `_transcribe_faster_whisper()` か `_transcribe_mlx()` を呼ぶ
  - シグネチャは据え置きなので `pipeline.Deps` も既存テストも無変更

2実装の違い:

- **faster-whisper（CTranslate2）** — どのOSでも動く。日本語精度が実用的で
  `vad_filter` が使える。ただし **Apple SiliconのGPUを使えずMacではCPU実行**。
  `large-v3` で1時間の会議に数十分〜1時間
- **mlx-whisper** — Apple GPUを使い、Macでは大幅に速い。`requirements.txt` に環境
  マーカー付きで入れてあり、Apple Silicon以外ではpipがスキップする

**`backend = "auto"`（既定）の解決規則**: `sys.platform == "darwin"` かつ
`platform.machine() == "arm64"` かつ `mlx_whisper` がimport可能なら `"mlx"`、それ以外は
`"faster-whisper"`。明示指定（`"mlx"` / `"faster-whisper"`）はそのまま従う。

**`model` の扱い:**

- ユーザーはサイズ名（`large-v3` / `large-v3-turbo` / `medium` / `small`）だけ指定する
- 各バックエンドが実体へ変換する（faster-whisperはそのまま、mlxは `_MLX_MODEL_MAP` で
  `mlx-community/whisper-<size>` へ）
- `/` を含む文字列はフルHFリポジトリ名として素通しする
- LLMの `model` / `vlm_model` を2キーに分けたのと違い、ここは「1論理名＋変換表」に
  した（ユーザーの選択肢を減らすため）

**mlxの割り切り:**

- mlx-whisperは結果を一括で返す（ジェネレータではない）ため、文字起こし中の逐次進捗が
  出せない
- 開始時に説明メッセージを1回出し、完了後にセグメントを変換しながら `on_progress` を
  回す（進捗バーは0→100に飛ぶ）
- 逐次表示が要るなら音声を自前でチャンク分割する必要があり、それは今回のスコープ外

**モデルは事前取得必須、実行時はオフライン強制:**

- Whisperモデル（既定 `large-v3-turbo`、約1.6GB）はリポジトリに含めずHuggingFaceの
  共有キャッシュ（`~/.cache/huggingface/hub/`）に入る。取得は **セットアップ時の
  1回だけ**
- `scripts/setup.sh` がvenv作成・依存導入に続けて
  `python src/meeting_minutes/download_transcribe_model.py` を呼ぶ
- この取得スクリプトは `resolve_backend()` の結果に合うモデルだけを落とし、取得済み
  なら何もしない（冪等）
- **取得は必須で、失敗したら `set -e` でセットアップ自体を失敗終了させる**（旧: 失敗を
  握りつぶし「初回実行時に自動DL」と案内していた）

アプリ実行時（`cli.py` / `gui.py`）は一切外部通信させない。二段構え:

1. **環境変数** — エントリポイントがHuggingFace系ライブラリのimportより前に
   `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` を `setdefault` する。`__init__.py`
   ではなく `cli.py` / `gui.py` に置く（取得スクリプト自身が同じパッケージをimport
   するため、`__init__.py` に置くと取得スクリプト自身までオフラインになりモデルを
   取得できなくなる）。`setdefault` なので明示的に `0` を指定した利用者の意図は
   尊重する
2. **環境変数に依存しない実行時ガード** — 各バックエンドが実行前にキャッシュ有無を
   明示チェックし、未取得なら自動ダウンロードせず `ModelNotAvailableError`（i18n
   済みメッセージ）で停止する。CLI・GUIとも例外を捕捉して1行で表示するので、
   スタックトレースは画面に出ない。なお `config.model` はサイズ名／HF repo idだけで
   なく **ローカルのモデルディレクトリのパス** も取れる（エアギャップ配布向け）。両
   ガードとも実在するディレクトリならHF解決をスキップする

LM Studio側のLLM/VLMは別管理なので、モデル取得スクリプト・オフライン強制いずれの
対象外。

**空耳の繰り返し（repetition loop）対策:**

- 不具合: 歌やBGMを含む区間で、faster-whisper・mlx-whisper共通の既定
  `condition_on_previous_text=True`（直前の窓の出力を次の窓の文脈にする）が災いし、
  同じ空耳フレーズを何十行も繰り返す幻覚が発生した
- 対処: 両バックエンドで `condition_on_previous_text=False` を明示指定（前の窓の
  誤りに引きずられなくなる）

**残る限界:**

- 完全な無音や歌唱区間で単発の幻覚（Whisper系モデル定番の「ご視聴ありがとうございました」
  等）が1行だけ混じることはある
- `no_speech_threshold` / `logprob_threshold` の既定値では防ぎきれないWhisper全般の
  既知の癖で、"大量に同じ行が続くループ" は防げても "たまに1行だけ変な行" はゼロに
  できない
- 歌・拍手・BGMが多い会議は文字起こしの目視確認を推奨する

---

## 7. 設定の多層化（`config.py`）

- **判断**: 優先順位はデフォルト値 < `config.toml`（無ければ `config.example.toml`）<
  環境変数（`MM_` プレフィックス）。TOMLの未知キーは黙って無視する
- **理由**:
  - チーム配布時は `config.toml` を各自が編集、CIや一時的な上書きは環境変数、という
    使い分けができる
  - 未知キー無視で、設定ファイルが将来のバージョンと前方互換になる
  - TOMLは標準ライブラリ `tomllib`（Python 3.11+）で読めるので依存を増やさない

---

## 8. なぜ tkinter か

- **判断**: GUIはtkinter
- **理由**: CPython同梱で追加依存ゼロ。非エンジニアへ「cloneして
  `python src/meeting_minutes/gui.py`」で配布しやすい。同じ作業場の別プロジェクトと
  同じスタックで、学習コストも共有できる
- **不採用にした案**: Web UI（Flask＋ブラウザ等）。サーバープロセスとポートの管理、
  ブラウザ前提の説明が増える。ローカル完結・単体配布という方針と噛み合わない

---

## 8.5 主なリファクタリング履歴（2026-09）

MVP構成への移行に伴う内部構造の整理。挙動は変えていない（リファクタのみ）。

- **Model-View-Presenter化**（Issue #49）— `gui.py`1クラスに混在していたウィジェット
  構築・画面ロジック・実処理起動を、`view/`（Tkinter実装）・`presenter/`（画面ロジック、
  tkinter非依存）・`model/`（実処理）に分割
- **エントリポイントをsrc/配下へ**（Issue #51）— `gui.py`等をリポジトリ直下から
  `src/meeting_minutes/`へ移動。ファイル指定起動・`-m`起動の両方に対応（非エンジニア
  向けの「cloneして1コマンド」の手軽さを優先し、主たる案内はファイル指定形式）
- **Model層をmodel/に集約**（Issue #53）— 実処理10モジュールを`view/`+`presenter/`+
  `model/`の対称構成へ
- **GUIの表示言語ja/en対応**（Issue #55）— GUIの画面文言のみ切替可能に（文字起こし
  言語・LLMプロンプト・議事録内容は対象外）。起動時に1回だけ選択

---

## 9. テスト戦略

- **純粋ロジックは普通の単体テスト** — フレーム間引き（`_thin_by_gap` / `_cap_count`）、
  チャンク分割（`_split_segments`）、設定解決（`load_config`）は外部依存なしで検証
- **外部プロセス依存はskip可能に** — ffmpegを実際に呼ぶテストは `needs_ffmpeg`
  マーカー＋`skipif` で、ffmpegが無い環境では飛ばす
- **LLMはスタブ注入** — `minutes` / `pipeline` のテストはフェイクのクライアントや
  `Deps` を渡し、ネットワークもモデルも要らない
- **GUIロジックはFakeView注入** — `MainPresenter` のテスト（`test_gui_presenter.py`）は
  `MainView` の偽実装を渡し、tkinterを一切起動せず進捗率計算・フォーマット3択の解決・
  ボタン状態遷移を検証する（Issue #49、8.5節）
- ffmpegがある環境では、生成したテストクリップから実際にフレームを抜く統合テストも
  走らせる（`-vsync` → `-fps_mode` の回帰もここで防いでいる）

---

## 9.5 起動前チェックと途中再開

**きっかけ:**

- 1時間の会議で文字起こしに数分かけたあと、フレーム解析の段階でLM Studioが
  「No models loaded」を返して全部が無駄になった
- 長時間処理なのに「失敗が遅い」「失敗するとゼロからやり直し」が痛い

**起動前チェック（`pipeline.run` の `"preflight"` 段階）:**

- 音声抽出より前に、`client.preflight([config.ai.llm_model, config.ai.vlm_model])` を
  呼ぶ
- 各モデルへ `max_tokens=1` の極小リクエストを投げ、1つでも失敗したら **その場で
  中断** する。数分待たされる前に「モデルをロードして」と分かる
- 副次的にLM StudioのJust-in-timeロードを前倒しで起こす
- `llm_client` は400応答の本文に `No models loaded` / `model_not_found` /
  `"param": "model"` を見つけたら、「JITをONにする / Loaded Instancesでロード /
  モデル名をLibraryと一致させる」ヒントを添える

**途中再開（`run(..., reuse=True)`、既定ON）:** 中間ファイルをすべて同じ `reuse`
フラグで再利用し、**終わっている分だけスキップして残りだけ実行** する。

- `transcript.json` があれば `load_transcript` で読み戻し、文字起こしをやり直さない
- `frames/frames.json` があれば `load_frames` で読み戻し、フレーム抽出をやり直さない
- `frames/frame_notes.json` があれば、そこまで解析済みのフレームは **1枚単位で**
  スキップする（`vision.describe_frames` が起動時に読み戻し、1枚終えるたびに書き
  直す）。当初はVLM失敗直後の作り直しを狙って再利用しない設計だったが、60枚全部を
  毎回やり直すコストの方が大きいと分かり、他の中間ファイルと同じ「1単位ごとに
  永続化して再開」方式に揃えた
- `work/minutes_partials.json`（チャンク要約、5章参照）があれば、終わっている
  チャンクは要約し直さない

最初からやり直したいときはCLI `--fresh` / GUIのチェックボックスで `reuse=False`
（このときはすべての中間ファイルを無視して最初から書き直す）。

**各工程・各チャンク・各フレームの所要時間を表示する:**

- 「今どのくらい待てばいいか」が長時間処理では重要
- `pipeline.py` / `minutes.py` / `vision.py` それぞれに `_format_elapsed()` を置き、
  `time.monotonic()` で実測した時間を完了メッセージに埋め込む（例:「文字起こし完了
  （444区間、所要12分34秒）」）
- 時間はメッセージ文字列に含めて渡す設計にしてあり、ログの表示側（GUI/CLI）は
  触っていない。進捗イベントの形（stage, current, total, message）も変えていない

**使用モデル名と「応答を待っています」も同じメッセージに埋め込む:**

- LLM/VLMを呼ぶ直前のメッセージ（preflight・文字起こし開始・フレーム解析中・部分要約・
  議事録統合）すべてに、使っているモデル名と「応答を待っています」を添える
- ねらいは2つ: 実行中のGUIの設定パネルを見なくても今どのモデルが動いているか分かる
  こと、そしてブロッキングなHTTP呼び出し中（数秒〜数分、ストリーミングはしていない）に
  画面が固まって見えないようにすること
- 所要時間の表示と同じ理由で、これもメッセージ文字列に含めて実現しており、進捗
  イベントの形は変えていない

---

## 9.6 中断（キャンセル）の設計

- **判断**: GUIに「中断」ボタンを追加。実装は **協調的キャンセル**（cooperative
  cancellation）— 共有の `threading.Event` を長い処理の節目でチェックし、立っていたら
  `PipelineCancelled` を送出して巻き戻す方式
- **理由**: Pythonのスレッドは外部から強制停止できない（`Thread.kill()` は存在
  しない）。ネイティブに安全なのは「処理側が自分から見て安全なタイミングで抜ける」
  やり方だけ。`on_progress` を各関数に引き回しているのと同じパターンで `cancel_event`
  も引き回すことで、既存の構造を壊さずに追加できた

**`cancel.py` を独立モジュールにした理由:**

- `PipelineCancelled` を `pipeline.py` に置くと、`pipeline.py` がimportしている
  `transcribe.py` / `vision.py` / `minutes.py` がそれを使うために `pipeline` を逆
  importする形になり循環importになる
- 依存ゼロの `cancel.py` を切り出して全員がそこからimportする構成にした

**チェックを入れた場所（効く/効かない）:**

| 場所 | 反応の速さ |
| --- | --- |
| 各ステージの開始前 | ほぼ即時 |
| フレーム解析ループ（VLM 呼び出しの合間） | 1フレーム分。体感で一番効く（最大60回ある） |
| 議事録のチャンク要約ループ | 1チャンク分 |
| faster-whisper のセグメント生成ループ | 1セグメント分（遅延生成なので途中で打ち切れば以降の生成も止まる） |
| mlx-whisper の文字起こし呼び出し中 | **反応しない**（結果を一括で返す1回のブロッキング呼び出し。呼び出し前のみチェック） |
| ffmpeg サブプロセス実行中 | **反応しない**（`subprocess.run` で待つだけ。Popen 化すれば中断可能だが今回は見送り） |

**中断時に残るもの:**

- `finally` でLLMクライアントは必ず閉じる
- `transcript/transcript.json` / `frames/frames.json` に加え、**フレーム解析の
  途中経過（`frames/frame_notes.json`）も1枚ごとに保存済み**なので、次回実行時
  （9.5の再開機構）は解析済みのフレームからやり直さない
- 同様に議事録のチャンク要約（`work/minutes_partials.json`）も終えた分から再開する

---

## 10. 既知の弱点（正直な棚卸し）

| 弱点 | 現状 | 次にやるなら |
| --- | --- | --- |
| ~~再開不可~~（対応済み） | 文字起こし・フレーム抽出・フレーム解析（1枚単位）・チャンク要約（1つ単位）すべてで再計算をスキップ（`reuse=True` 既定）。処理前に `client.preflight()` で LLM 疎通を確認し早期失敗 | LLM 落ち時の自動リトライ |
| 話者分離なし | すべて「参加者」表記 | pyannote 等の話者分離を任意工程で追加 |
| フレーム解析は VLM 前提 | OCR だけの軽量経路が無い | `vision.describe_frames` を差し替え可能にし OCR バックエンドを追加 |
| 進捗の総数が推定 | 見た目が「だいたい」 | 事前に軽い解析パスを入れて実数に近づける |
| GUI から設定不可 | `config.toml` 直編集 | GUI に設定パネルを追加 |
| エラー回復が薄い | LLM 落ちは中断 | リトライ／明示的な再開ガイド |
