# 設計の判断と理由

このドキュメントは「なぜこの形にしたか」を残すもの。実装の説明は
[`architecture.md`](architecture.md) を参照。

---

## 1. なぜ「完全ローカル」なのか

**判断:** 音声・映像・文字起こし・要約のすべてをマシン内で処理し、外部ドメインへ
接続するコードを持たない。LLM/VLM は自分で立てるローカルサーバーへ `localhost` で送るだけ。

**理由:** 想定ユーザーは、会議に顧客情報・未公開情報・個人情報が含まれ、クラウドの
STT や生成 AI に録音を送れない現場。ここでは「精度が少し落ちても、外に出ないこと」が
機能要件そのもの。

**トレードオフ:** ローカル LLM は日本語要約の品質がクラウド最上位モデルに劣る。
`large-v3` の文字起こしは Mac では CPU 実行で遅い。導入側にそれなりの RAM が要る。
これらは「外部送信しない」と引き換えに受け入れる前提。

---

## 2. パイプラインの継ぎ目：`pipeline.run()` + `Deps`

**判断:** 全工程を `pipeline.run(video_path, config, on_progress, *, deps)` の 1 関数に
集約。各工程の実装は `Deps` という dataclass の関数フィールドとして注入し、
既定は本物、テストではダミーを渡す。

**理由:**
- GUI・CLI・テストが同じエントリを共有でき、工程の並び順や出力パスの組み立てが
  1 か所にしかない。
- ffmpeg も LLM サーバーも用意せずに「工程が正しい順で呼ばれるか」「進捗コールバックの
  中身」「`output/<動画名>/` の組み立て」を検証できる（`tests/test_pipeline.py`）。
- dataclass の `__init__` が各関数をインスタンス属性へ代入するので、関数を直接
  デフォルトに置いても記述子（bound method）化されず、素直に差し替えられる。

**不採用にした案:**
- *モック無しの結合テストだけ*：ffmpeg とローカル LLM が必須になり、CI に載せづらく
  失敗の切り分けもしにくい。
- *各工程を抽象基底クラス＋サブクラスで差し替え*：工程は今のところ関数 1 個で足り、
  クラス階層はオーバースペック。関数フィールドの dataclass で十分。

---

## 3. 進捗通知：`on_progress(stage, current, total, message)`

**判断:** 全工程が同じシグネチャのコールバックを呼ぶ。UI の知識は持たせない。

**理由:** CLI はこれをテキスト行に、GUI は `queue.Queue` に載せて UI スレッドへ渡す、と
受け手を差し替えられる。tkinter はワーカースレッドから直接ウィジェットを触れないため、
「進捗はデータとして渡し、UI 反映は UI スレッド側でやる」という分離が必要だった。

**トレードオフ:** `total` は正確な値ではなく見積もり（例: 文字起こしのセグメント数は
`動画長 ÷ 4 秒`）。進捗バーは「だいたい」の表示になる。正確さより「止まって見えない」
ことを優先した。

---

## 4. LLM/VLM を OpenAI 互換 API で抽象化

**判断:** `llm_client.py` は OpenAI 互換の `/v1/chat/completions` を `httpx` で直接叩く。
接続先は `config.llm.base_url` の 1 か所だけ。

**理由:**
- LM Studio・Ollama・その他の OpenAI 互換サーバーを、`base_url` の変更だけで交換できる。
  特定の GUI アプリにロックインされない。
- `chat`（テキスト）と `describe_image`（画像を data URL で送る）の 2 メソッドしか
  要らず、レスポンスも `choices[0].message.content` を読むだけ。`openai` パッケージを
  足すほどの複雑さが無い。依存は薄いほどよい。

**不採用にした案:** `openai` パッケージ経由。動くが、この用途では依存とバージョン制約が
増えるだけで得るものが少ない。

**タイムアウトは「サーバーが死んでいる」と「応答が長い」を区別する:** 実際に、議事録の
最終統合（部分要約より出力トークン数が多い）が既定タイムアウト180秒を超えて失敗する
不具合があった。原因はサーバーの不調ではなく、単に大きいモデルの生成に時間がかかった
だけ。`httpx.TimeoutException`（`RequestError` の一種）を先に catch し、「サーバーを
起動してください」ではなく「`timeout` を増やしてください」という専用メッセージを返す
ようにした。あわせて既定 `timeout` を 180 → 600 秒に引き上げた。

**推論（reasoning）モデルは議事録生成に使わない（前提条件）:** `qwen/qwen3.8-27b`
（Qwen3 系の推論モデル）で、チャンク要約が `### 部分 N` の見出しだけ・本文が空で
保存される不具合が起きた。原因は、推論モデルが見えない思考を `message.reasoning_content`
に出力し、`max_tokens` をそこで使い切って可視の `message.content` が空のまま
`finish_reason=length` で返ってくること（HTTP は 200 なので気づきにくい）。実測で
約5000文字のチャンクに思考4129文字、成功時 433 秒/チャンクと実用外だった。対応:

- `llm_client` で「`content` 空 かつ `reasoning_content` あり」を検知し、
  「思考で max_tokens を使い切った。増やすか reasoning を下げて」という明示エラーに。
- チャンク要約の `max_tokens` を固定 1500 → `config.llm.max_tokens` に。
- `minutes._load_partials` は本文が空のエントリを無効化（壊れた
  `minutes/minutes_partials.json` を再実行時に自動で作り直す）。
- 既定 `max_tokens` を 4096 → 8192 に。

ただし根本原因は「重い推論モデルを議事録生成に使っている」ことなので、
**非推論の Instruct 系モデルを使うことを前提条件としてドキュメント化**した
（`docs/models.md`）。`/no_think` を自動付与する案もあったが、これは Qwen3 専用の
書き方で他社の推論モデルには効かず、モデルごとに切り方が違うためコードには入れない。

---

## 5. 長い文字起こしの map-reduce（`minutes.py`）

**判断:** 文字起こしテキストが閾値（`_CHUNK_TRIGGER_CHARS`）を超えたら、
セグメントをチャンクに分割 → 各チャンクを箇条書きに要約 → その要約群＋フレーム要点で
最終議事録を生成、という 2 段階に切り替える。短ければ 1 回の呼び出しで済ませる。

**理由:** 1 時間の会議の文字起こしは、ローカル LLM の実用的なコンテキスト長に収まらない
ことが多い。丸ごと渡して切り詰められるより、段階要約のほうが決定事項の取りこぼしが減る。

**トレードオフ:** LLM 呼び出し回数が増え、時間がかかる。チャンク要約で細部が
落ちることもある。閾値は「収まるうちは 1 回で」を狙った経験的な値で、モデルを変えたら
調整の余地がある。

**閾値の更新（2026-09）:** 実運用で `_CHUNK_TRIGGER_CHARS=12000` は小さすぎ、
1〜2 時間級でない普通の会議まで分割されて「要約の要約」から議事録が作られ、具体性が
大きく落ちていた。LLM を 32k コンテキストで運用する前提に合わせて既定を
40000 / 15000 に引き上げ、**収まる限り一発生成**、非常に長い会議だけ map-reduce に
フォールバックする方針にした。

**この前提は文書化と設定化が必要だった（2026-09、Issue #16）:** 「32k コンテキスト
前提」とコード内コメントに書いただけで `docs/models.md` に設定手順が無く、LM Studio で
Context Length を上げていないユーザーが一発生成時に HTTP 400（context length 不足）を
踏んだ。対応: (1) `docs/models.md` に Context Length を 32768 以上にする手順を明記、
(2) `llm_client` がこの 400 を検出して原因の分かる日本語ヒントに変換、
(3) 閾値を `config.toml` の `[llm] chunk_trigger_chars` / `chunk_size_chars` で
調整可能にし、小さいコンテキストのモデルでも下げて分割モードで回せるようにした
（`minutes.py` の `_CHUNK_TRIGGER_CHARS` / `_CHUNK_SIZE_CHARS` はその既定値）。

**文字数しきい値そのものが甘かった（2026-09、Issue #18）:** Context Length を
32768 に正しく設定しても、既定 `_CHUNK_TRIGGER_CHARS=40000` のままだと一発生成が
上限を超えて再び HTTP 400 になった。原因の切り分け:
- Qwen 系トークナイザで日本語は約 **0.74 トークン/文字**（実測。コメントの
  「1〜1.5」は過大）。40000 字 ≈ 3 万トークン。
- 加えて **frames_text（フレーム解析の連結）が支配的**だった。実ケースで 60 枚
  ≈ 1.8 万トークン。transcript より大きい。
- `max_tokens=8192`（推論モデル対策の既定）をそのまま応答予約に使っていた。

対応（`minutes.py` / `llm_client.py` / `pipeline.py`）:
- `pipeline` が LM Studio の `/api/v0/models` から **ロード中モデルの実コンテキスト
  長**（`loaded_context_length`）を取得し、`generate_minutes(context_tokens=...)` へ渡す。
- `generate_minutes` は、実コンテキスト長が分かるときは
  `概算プロンプトトークン + 応答予約 + マージン <= context_tokens` で一発 / 分割を
  判断する（文字数しきい値は取れないときのフォールバックに降格。既定 20000 / 12000）。
- `frames_text` はコンテキストの約 1/3（下限 6000 トークン）に切り詰める。
- 議事録本文の応答予約は `max_tokens` ではなく `_MINUTES_RESPONSE_TOKENS`（5000）で
  頭打ちにする（型を埋めるタスクなので十分。推論モデルは非対象）。
- 取れないときのため `[llm] context_tokens` で実値を直接指定もできる。
実測に基づくテストを `test_minutes.py` / `test_llm_client.py` に追加。

**チャンク要約は1つ終えるたびに `minutes/minutes_partials.json` へ保存する:** 実際に、
チャンク要約が2つとも終わったあとの最終統合（出力トークン数が多い）だけがタイムアウト
する事例があった。チャンク要約自体は無事終わっているのに、`generate_minutes()` は
1回の呼び出しなので失敗すれば全部やり直しだった。`_save_partials()` で完了ぶんを
都度ディスクに書き、`reuse=True`（既定）での再実行時は `_load_partials()` で読み戻し、
終わっているチャンクを要約し直さない。`out_dir` を渡さない呼び出し（テスト等）では
この永続化自体が無効になるだけで、動作は変わらない。

**保存ファイルにチャンク境界のシグネチャを持たせる（2026-09、Codex レビュー指摘）:**
`minutes/minutes_partials.json` は `chunk_size_chars` と分割入力のセグメント総数を一緒に
保存する（`format: 2`）。再開時にこれが現在の設定と一致しなければキャッシュを
破棄して最初から要約し直す。これが無いと、Context Length 対策で
`chunk_size_chars` を下げて再開したとき、旧境界の部分要約が新しいチャンク列に
件数だけで採用され、議事録の内容がエラーなく重複・欠落していた。メタ情報の無い
旧形式（JSON 配列）も境界を検証できないため採用しない。

**議事録の「構造」を外部テンプレートで差し替え可能に（2026-09、Issue #21）:** お客様
ごとの規定様式に対応するため、`_MINUTES_TEMPLATE` を「構造（`_MINUTES_STRUCTURE`）」と
「入力セクション（`_INPUT_TRANSCRIPT` / `_INPUT_FRAMES`）」に分割。`config.toml` の
`[output] template_path` で構造を差し替える（`generate_minutes(template_path=...)` へは
`pipeline` が config から渡す）。実行時の一時上書き経路は、当初 GUI ボタン
＋ CLI `--template` で付け → 一度削除し → 最終的に GUI の「議事録フォーマット」
ドロップダウン（`ttk.Combobox`）として復活させた。先頭固定項目が `config.toml` の設定
（`内蔵（既定）` またはファイル名）、「ファイルを選択...」でその回だけ差し替え、先頭項目を
選び直せば戻る。CLI 側の一時上書きは復活させていない（`config.toml` で足りる）。
ユーザー向け用語は「内蔵（既定のフォーマット）」で統一し、ツールチップで「会議内容で
動的に変わらない固定の構成」であることを補足する。要点:

- **システムプロンプト（`prompts/minutes_ja.txt` の捏造禁止等）はテンプレートに関わらず
  常に適用**。テンプレートは構造だけ。品質ルールを客先様式に巻き込ませない。
- 差し込みは逐次 `.replace` でも `.format` でもなく、テンプレートを1回だけ走査する
  一括置換（`re.sub` + コールバック、`_fill_minutes_template`）。置換後の値
  （transcript 等）は再走査しないので、文字起こし中に偶然 `{frames}` のような文字列が
  あっても巻き込まれない。既知プレースホルダー名でない素の `{ }`（JSON 例など）は不変。
- テンプレが `{transcript}` / `{frames}` を書いていなければ、**足りない方だけ**を末尾に
  補う（`{transcript}` は書いたが `{frames}` を忘れてもフレーム情報が消えない）。
  客先は出力様式だけ書けばよい。
- ファイルが無い・読めない・空 → エラーで止めず内蔵にフォールバックし `on_progress`
  で警告（`load_minutes_structure`）。
- 一発生成・分割生成の統合ステップの両方で同じ `structure` を使う。

**GUI のフォーマット選択はラジオ 3 択に（2026-09、Issue #34 PR-B）:** 上記の
`ttk.Combobox` は「選択済み項目がハイライトされたままで選び直しにくい」「幅が中身に
合わない」問題があり、`ttk.Radiobutton` の排他 3 択（内蔵（既定）／ファイルを選択／
おまかせ）に置き換えた。`_start()` で `output.auto_structure` と `output.template_path` を
**毎回**ラジオの状態から設定する（`config.toml` で `auto_structure=true` でも GUI の
明示選択が優先。3 ラジオが排他的に 1 状態を表す）。

**「おまかせ」モード（2026-09、Issue #34）:** 動画の内容に合わせて見出し構成そのものを
LLM に提案させる 3 つ目の選択肢。`config.toml` の `[output] auto_structure`（優先順位
`auto_structure` > `template_path` > 内蔵）。要点:

- **トークン予算を再燃させない**（Issue #16/#18/#19 と同種の再発を防ぐ）。構造生成は
  1 回だけ。文字起こし全文が「構造生成用の軽い予算」（応答予約は議事録本文と同じ
  `minutes_max_tokens`）に収まればそのまま材料に、収まらなければ **既存の map-reduce
  チャンク要約を再利用**（`_summarize_chunks` は最大 1 周。新たな全文読み込みパスは
  足さない）。長い material は `_fit_structure_material` で構造生成予算に切り詰めてから渡す。
- **生成物はプレースホルダーをリテラルのまま出力させる**（プロンプトで明示）。
  `output/<動画名>/minutes/structure_used.txt` に保存し、そのまま `templates/` にコピーして
  固定テンプレート化できる。
- **フォールバックは常に内蔵**（`_MINUTES_STRUCTURE`。ファイル指定時も内蔵）。失敗条件:
  LLM 例外／空応答／必須プレースホルダー（`{title}` `{datetime_hint}` `{duration_hint}`）
  欠落／構造単体で統合予算を超える（`_structure_fits_minutes_skeleton`。分割に
  切り替えても救えないため棄却）。失敗時は `minutes/structure_used.txt` を残さない（前回実行の
  残骸も消す）。
- 構造を差し替えた **後** に予算（`one_pass` / `frames_text` / `size_chars`）を
  `_minutes_budget` で計算し直す。統合直前には実際の `merged_transcript` ＋ frames を
  含めた予算を確認し、超える分は `_fit_merged_transcript` で末尾を切り詰める。
- 構造生成（重い LLM 呼び出し）の直後にも `check_cancel` を入れる。

**議事録は `minutes.md` と `minutes.docx` の両方を常に出力する（2026-09、Issue #13）:**
Word で開きたい・そのまま配布したいという要望。GUI に選択機能は付けず、常時両方
出す（利用者が迷わない）。変換は新規モジュール `model/docx_export.py`。

- **`pandoc` 等の外部バイナリは使わない**。`python-docx`（Pure Python。連れてくる
  `lxml` は全 OS プリビルド wheel、`typing_extensions` も）だけを依存に足す。CI は
  3 OS とも `pip install` で完結し、システム依存は増えない。
- **汎用 Markdown パーサーは入れない**。議事録が実際に取る Markdown サブセット
  （ATX 見出し / `- `・`* ` の箇条書き（ネスト）/ GFM パイプ表 / フェンス /
  `**bold**`・`` `code` ``）だけを行ベースの小さな state machine で変換する。
  「おまかせ」やカスタムテンプレートで構成が変わっても壊れないよう、**未知の行は
  素の段落として落とす**。変換自体は例外を投げない。
- **`.md` が主成果物**。`pipeline.py` は `save_minutes` の直後に `save_minutes_docx`
  を `try/except` で呼び、変換・書き出しが失敗しても警告（`pmsg.warn_docx_failed`）
  を出して続行する。`.docx` は `output/<動画名>/minutes.docx`（中間物ではないので
  `minutes/` 配下ではなくルート）。

---

## 6. 文字起こしバックエンド（faster-whisper / mlx）

**判断:** `transcribe.py` は 2 実装を持ち、`config.backend` で選ぶ。
`transcribe_wav()` は薄いディスパッチャで、`resolve_backend()` の結果に応じて
`_transcribe_faster_whisper()` か `_transcribe_mlx()` を呼ぶ。シグネチャは据え置き
なので `pipeline.Deps` も既存テストも無変更。

- **faster-whisper（CTranslate2）** — どの OS でも動く。日本語精度が実用的で
  `vad_filter` が使える。ただし **Apple Silicon の GPU を使えず Mac では CPU 実行**。
  `large-v3` で 1 時間の会議に数十分〜1 時間。
- **mlx-whisper** — Apple GPU を使い、Mac では大幅に速い。`requirements.txt` に
  環境マーカー付きで入れてあり、Apple Silicon 以外では pip がスキップする。

**`backend = "auto"`（既定）の解決規則:** `sys.platform == "darwin"` かつ
`platform.machine() == "arm64"` かつ `mlx_whisper` が import 可能なら `"mlx"`、
それ以外は `"faster-whisper"`。明示指定（`"mlx"` / `"faster-whisper"`）はそのまま従う。

**`model` の扱い:** ユーザーはサイズ名（`large-v3` / `large-v3-turbo` / `medium` /
`small`）だけ指定する。各バックエンドが実体へ変換する（faster-whisper はそのまま、
mlx は `_MLX_MODEL_MAP` で `mlx-community/whisper-<size>` へ）。`/` を含む文字列は
フル HF リポジトリ名として素通しする。LLM の `model` / `vlm_model` を 2 キーに
分けたのと違い、ここは「1 論理名＋変換表」にした（ユーザーの選択肢を減らすため）。

**mlx の割り切り:** mlx-whisper は結果を一括で返す（ジェネレータではない）ため、
文字起こし中の逐次進捗が出せない。開始時に説明メッセージを 1 回出し、完了後に
セグメントを変換しながら `on_progress` を回す（進捗バーは 0 → 100 に飛ぶ）。
逐次表示が要るなら音声を自前でチャンク分割する必要があり、それは今回のスコープ外。

**モデルは事前取得必須、実行時はオフライン強制:** Whisper モデル（既定
`large-v3-turbo`、約 1.6GB）はリポジトリに含めず HuggingFace の共有キャッシュ
（`~/.cache/huggingface/hub/`）に入る。取得は **セットアップ時の 1 回だけ**。
`scripts/setup.sh` が venv 作成・依存導入に続けて
`python src/meeting_minutes/download_transcribe_model.py`
（→ `meeting_minutes.download_transcribe_model`）を呼ぶ。この取得スクリプトは
`resolve_backend()` の結果に
合うモデルだけを落とし、取得済みなら何もしない（冪等）。**取得は必須で、失敗したら
`set -e` でセットアップ自体を失敗終了させる**（旧: 失敗を握りつぶし「初回実行時に
自動DL」と案内していた）。

アプリ実行時（`cli.py` / `gui.py`）は一切外部通信させない。二段構え:

1. **環境変数** — エントリポイントが HuggingFace 系ライブラリの import より前に
   `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` を `setdefault` する。`__init__.py`
   ではなく `cli.py` / `gui.py` に置く（`download_transcribe_model.py` が同じ
   パッケージを import するため、`__init__.py` に置くと取得スクリプト自身まで
   オフラインになりモデルを取得できなくなる）。`setdefault` なので社内ミラー等で明示的に `0` を指定した
   利用者の意図は尊重する。
2. **環境変数に依存しない実行時ガード** — `_transcribe_faster_whisper` は
   `WhisperModel(..., local_files_only=True)` を無条件で渡し、事前に
   `_faster_whisper_model_cached()`（`download_model(..., local_files_only=True)`）で
   キャッシュ有無を確認する。`_transcribe_mlx` は `mlx_whisper.transcribe` に
   相当引数が無いので `_mlx_model_cached()`（`snapshot_download(..., local_files_only=True)`）
   で明示チェックする。いずれも未取得なら自動ダウンロードせず
   `ModelNotAvailableError`（i18n 済みメッセージ）で停止する。CLI・GUI とも例外を
   捕捉して `str(exc)` を 1 行で表示するので、スタックトレースは画面に出ない。
   なお `config.model` はサイズ名／HF repo id だけでなく**ローカルのモデル
   ディレクトリのパス**も取れる（エアギャップ配布向け）。両ガードは先頭で
   `Path(...).is_dir()` を見て、実在するディレクトリなら HF 解決をスキップする。

LM Studio 側の LLM/VLM は別管理なので、モデル取得スクリプト・オフライン強制いずれの
対象外。

**空耳の繰り返し（repetition loop）対策:** 実際に起きた不具合。歌や BGM を含む区間で、
faster-whisper・mlx-whisper 共通の既定 `condition_on_previous_text=True`（直前の窓の
出力を次の窓の文脈にする）が災いし、同じ空耳フレーズを何十行も繰り返す幻覚が発生した。
両バックエンドで `condition_on_previous_text=False` を明示
指定して対処（前の窓の誤りに引きずられなくなる）。**残る限界:** 完全な無音や歌唱区間で
単発の幻覚（Whisper 系モデル定番の「ご視聴ありがとうございました」等）が1行だけ混じる
ことはある。`no_speech_threshold` / `logprob_threshold` の既定値では防ぎきれない
Whisper 全般の既知の癖で、"大量に同じ行が続くループ" は防げても "たまに1行だけ変な行"
はゼロにできない。歌・拍手・BGM が多い会議は文字起こしの目視確認を推奨する。

---

## 7. 設定の多層化（`config.py`）

**判断:** 優先順位は デフォルト値 < `config.toml`（無ければ `config.example.toml`）<
環境変数（`MM_` プレフィックス）。TOML の未知キーは黙って無視する。

**理由:**
- チーム配布時は `config.toml` を各自が編集、CI や一時的な上書きは環境変数、という
  使い分けができる。
- 未知キー無視で、設定ファイルが将来のバージョンと前方互換になる。
- TOML は標準ライブラリ `tomllib`（Python 3.11+）で読めるので依存を増やさない。

---

## 8. なぜ tkinter か

**判断:** GUI は tkinter。

**理由:** CPython 同梱で追加依存ゼロ。非エンジニアへ「clone して `python src/meeting_minutes/gui.py`」で
配布しやすい。同じ作業場の別プロジェクトと同じスタックで、学習コストも共有できる。

**不採用にした案:** Web UI（Flask + ブラウザ等）。サーバープロセスとポートの管理、
ブラウザ前提の説明が増える。ローカル完結・単体配布という方針と噛み合わない。

---

## 8.5 GUI の構成（Model-View-Presenter、2026-09、Issue #49）

**判断:** `gui.py` の 1 クラス（`App`）に混在していた「ウィジェット構築」「画面ロジック
（進捗率計算・フォーマット 3 択の解決・成功/失敗/中断の状態遷移）」「実処理の起動」を、
参考プロジェクト `tkinter-task-manager-mvp` と同じ Model-View-Presenter に分けた。
本プロジェクトはタブが無い単一画面なので、参考実装よりフラットな構成にしている。

```
src/meeting_minutes/
  gui.py           Model・View・Presenter を組み立てて起動するだけの薄いラッパー（8.6 節）
  view/
    contract.py    MainView（抽象クラス）— Presenter が依存する契約。tkinter を知らない
    tk_main_window.py  TkMainWindow（Tkinter 実装。旧 gui.py の _build_ui 相当。
                       ウィジェット構築・イベントバインド・ダイアログ・ウィンドウ配置のみ）
  presenter/
    main.py        MainPresenter（旧 App の画面ロジック一式。View 契約と
                   model.config / model.pipeline.run にだけ依存し tkinter を import しない）
  model/           実処理（pipeline / transcribe / frames / vision / minutes / … 8.7 節）
```

- **`view/__init__.py` は抽象 `MainView` だけ再エクスポート**する。Tk 実装
  （`tk_main_window`）をここで import すると、Presenter のテストが
  `meeting_minutes.view` を読むだけで tkinter を巻き込んでしまうため。
- **スレッド／`queue` の仕組みは維持**。ワーカースレッドは `on_progress` で
  `queue.Queue` にイベントを積み、UI スレッドは `MainView.schedule(100ms, ...)`
  （＝ `root.after` の薄いラッパー）で定期的に取り出す。`root.after` を直接叩かず
  必ず View 経由にすることで、Presenter が tkinter に触れない状態を保つ。
- **`pipeline.run` は Presenter に注入**（`MainPresenter(view, config, run_pipeline=...)`）。
  既定は本物、テストではフェイク。
- **リファクタのみ**。見た目・文言・進捗計算は変えていない（旧 `App` と新
  `TkMainWindow`+`MainPresenter` でウィジェットツリーがバイト一致することを確認済み）。
  ※ 起動コマンドはこの後 8.6 節（Issue #51）で別途変更した。

---

## 8.6 エントリポイントも src/ 配下へ（2026-09、Issue #51）

**判断:** リポジトリ直下にあった `gui.py` / `cli.py` / `download_transcribe_model.py`
（当時は `prefetch.py`。ルートの薄いランチャー）を `src/meeting_minutes/` 配下へ移し、
直下から `.py` を無くした。参考
プロジェクト `tkinter-task-manager-mvp` と同じ構成。ルートのランチャーが持っていた
`sys.path.insert(0, ".../src")` は、次の `__package__` ブートストラップに置き換えた:

```python
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

`python src/meeting_minutes/gui.py` のようにファイル指定で起動すると `__package__` が
未設定なので `src/` を `sys.path` に足す。`python -m meeting_minutes.gui` で起動された
場合は `__package__` が設定済みなので何もしない（この場合は `cd src` するか
`PYTHONPATH=src` が要る）。両方の起動方法が動く。

**主たる案内コマンドはファイル指定形式**（`python src/meeting_minutes/gui.py`）にした。
非エンジニア利用者向けに「clone して 1 コマンド」の手軽さを保つため、`PYTHONPATH` の
設定や `cd` を要求する `-m` 形式より、パスが長くなるだけで追加設定の要らないファイル
指定形式を採る（8 節の設計判断の延長）。`-m` 形式は開発者向けの補足として併記する。

`download_transcribe_model.py` は元々 `from .config import ...` という相対 import
だったので、ファイル指定起動でも通るよう絶対 import
（`from meeting_minutes.model.config import ...`）に変えた
（`gui.py` / `cli.py` は元から絶対 import）。ロジックは変えていない。

---

## 8.7 Model 層を model/ にまとめる（2026-09、Issue #53）

**判断:** 実処理の 10 モジュール（`audio` / `cancel` / `config` / `ffmpeg_utils` /
`frames` / `llm_client` / `minutes` / `pipeline` / `transcribe` / `vision`）を
`src/meeting_minutes/model/` へ移し、`view/` + `presenter/` + `model/` の対称な
構成にした（参考プロジェクト `tkinter-task-manager-mvp` と同じ）。`gui.py` /
`cli.py` / `download_transcribe_model.py`（エントリポイント）は直下のまま。

- **10 モジュールは互いに相対 import**（`from . import audio` 等）なので、まとめて
  `model/` 直下に移すだけで内部参照は変更不要。外部（`gui` / `cli` /
  `download_transcribe_model` / `presenter/main`）の絶対 import を
  `meeting_minutes.xxx` → `meeting_minutes.model.xxx`
  に変えるだけ。
- `config.py` の `REPO_ROOT = Path(__file__).resolve().parents[N]` は、ファイルが
  1 階層深くなったぶん `parents[2]` → `parents[3]` に直した（挙動を保つための機械的な
  修正。ロジックは不変）。
- テストの文字列パッチ（`monkeypatch.setattr("meeting_minutes.config.REPO_ROOT", ...)`
  等）は import 文の置換では拾えないので、`git grep '"meeting_minutes\.'` で洗い出して
  `"meeting_minutes.model.config..."` に更新した。

---

## 8.8 GUI の表示言語 ja/en（2026-09、Issue #55）

**判断:** GUI の画面文言だけを日本語／英語で切り替えられるようにした。対象は
**GUI の画面テキストのみ** — 文字起こしの言語（`[transcribe] language`）、LLM への
プロンプト、生成される議事録の中身、CLI の出力は対象外。

- **起動時に 1 回だけ選ぶ方式**（動作中のライブ切替はしない）。実行中に文言が
  差し替わる UI を作らずに済み、`presenter` / `view` は言語を 1 個の文字列として
  受け取って保持するだけで済む。
- **カタログは `src/meeting_minutes/i18n.py` に集約**。`_STRINGS: dict[str, dict[str, str]]`
  が `{"window.title": {"ja": "...", "en": "..."}, ...}` の形。`t(key, language, **kwargs)`
  が 1 本の取り出し口で、`language` の訳 → 既定言語（ja）の訳 → `key` そのもの、の順に
  フォールバックし、`kwargs` があれば `str.format` する。未知キーは `default` か `key`。
- **ja 側は i18n 化前のリテラルをそのまま写した**。これにより `language="ja"`（既定）の
  ときの表示は今までとバイト単位で同じ、という保証になる。`test_i18n.py` の
  `test_ja_side_matches_pre_i18n_literals` と `test_gui_presenter.py` の既存テストが
  この不変を守る。
- **文言の出どころは 2 か所**: `view/tk_main_window.py`（静的なラベル・ボタン・
  ツールチップ・ファイルダイアログ）と `presenter/main.py`（工程ラベル・ログ行・
  エラーダイアログ）。どちらも `language` 引数（既定 `"ja"`）を受け取り、内部の
  `self._t(key, **kwargs)` から `i18n.t` を呼ぶ。旧 `_STAGE_LABEL` 辞書は
  カタログの `stage.<name>` キーへ移した。
- **言語の決め方**: 優先順位は `gui.py --lang {ja,en}` > `config.toml` `[gui] language`
  （= 環境変数 `MM_GUI_LANGUAGE`）> 既定 `ja`。`ja` / `en` 以外の値は
  `normalize_language()` が `ja` に丸める（`config.py` のロード時と、`view` /
  `presenter` の受け取り口の両方で）。

---

## 9. テスト戦略

- **純粋ロジックは普通の単体テスト** — フレーム間引き（`_thin_by_gap` / `_cap_count`）、
  チャンク分割（`_split_segments`）、設定解決（`load_config`）は外部依存なしで検証。
- **外部プロセス依存は skip 可能に** — ffmpeg を実際に呼ぶテストは `needs_ffmpeg`
  マーカー＋`skipif` で、ffmpeg が無い環境では飛ばす。
- **LLM はスタブ注入** — `minutes` / `pipeline` のテストはフェイクのクライアントや
  `Deps` を渡し、ネットワークもモデルも要らない。
- **GUI ロジックは FakeView 注入** — `MainPresenter` のテスト（`test_gui_presenter.py`）は
  `MainView` の偽実装を渡し、tkinter を一切起動せず進捗率計算・フォーマット 3 択の解決・
  ボタン状態遷移を検証する（Issue #49、8.5 節）。
- ffmpeg がある環境では、生成したテストクリップから実際にフレームを抜く統合テストも
  走らせる（`-vsync` → `-fps_mode` の回帰もここで防いでいる）。

---

## 9.5 起動前チェックと途中再開

**きっかけ:** 1 時間の会議で文字起こしに数分かけたあと、フレーム解析の段階で
LM Studio が「No models loaded」を返して全部が無駄になった。長時間処理なのに
「失敗が遅い」「失敗するとゼロからやり直し」が痛い。

**起動前チェック（`pipeline.run` の `"preflight"` 段階）:** 音声抽出より前に、
`client.preflight([config.llm.model, config.llm.vlm_model])` を呼ぶ。各モデルへ
`max_tokens=1` の極小リクエストを投げ、1 つでも失敗したら**その場で中断**する。
数分待たされる前に「モデルをロードして」と分かる。副次的に LM Studio の
Just-in-time ロードを前倒しで起こす。`llm_client` は 400 応答の本文に
`No models loaded` / `model_not_found` / `"param": "model"` を見つけたら、
「JIT を ON にする / Loaded Instances でロード / モデル名を Library と一致させる」
ヒントを添える。

**途中再開（`run(..., reuse=True)`、既定 ON）:** 中間ファイルをすべて同じ `reuse`
フラグで再利用し、**終わっている分だけスキップして残りだけ実行**する。

- `transcript.json` があれば `load_transcript` で読み戻し、文字起こしをやり直さない。
- `frames/frames.json` があれば `load_frames` で読み戻し、フレーム抽出をやり直さない。
- `frames/frame_notes.json` があれば、そこまで解析済みのフレームは**1枚単位で**スキップする
  （`vision.describe_frames` が起動時に読み戻し、1枚終えるたびに書き直す）。
  当初は「VLM が落ちた直後は作り直したいことが多い」という判断で再利用しない設計に
  していたが、60枚全部を毎回やり直すコストの方が大きいと分かり、他の中間ファイルと
  同じ「1単位ごとに永続化して再開」方式に揃えた。
- `minutes/minutes_partials.json`（チャンク要約、5章参照）があれば、終わっているチャンクは
  要約し直さない。

最初からやり直したいときは CLI `--fresh` / GUI のチェックボックスで `reuse=False`
（このときはすべての中間ファイルを無視して最初から書き直す）。

**各工程・各チャンク・各フレームの所要時間を表示する:** 「今どのくらい待てばいいか」が
長時間処理では重要なので、`pipeline.py` / `minutes.py` / `vision.py` それぞれに
`_format_elapsed()` を置き、`time.monotonic()` で実測した時間を完了メッセージに
埋め込む（例:「文字起こし完了（444 区間、所要 12分34秒）」）。ログの表示側（GUI/CLI）
を触らずに実現できるよう、時間はメッセージ文字列に含めて渡す設計にしてある——進捗
イベントの形（stage, current, total, message）を増やさずに済む。

**使用モデル名と「応答を待っています」も同じメッセージに埋め込む:** LLM/VLM を呼ぶ
直前のメッセージ（preflight・文字起こし開始・フレーム解析中・部分要約・議事録統合）
すべてに、使っているモデル名と「応答を待っています」を添える。ねらいは2つ:
実行中の GUI の設定パネルを見なくても今どのモデルが動いているか分かること、
そしてブロッキングな HTTP 呼び出し中（数秒〜数分、ストリーミングはしていない）に
画面が固まって見えないようにすること。所要時間の表示と同じ理由で、これも
メッセージ文字列に含めるだけで実現し、進捗イベントの形は変えていない。

---

## 9.6 中断（キャンセル）の設計

**判断:** GUI に「中断」ボタンを追加。実装は **協調的キャンセル**
（cooperative cancellation）——共有の `threading.Event` を長い処理の節目で
チェックし、立っていたら `PipelineCancelled` を送出して巻き戻す方式。

**理由:** Python のスレッドは外部から強制停止できない（`Thread.kill()` は
存在しない）。ネイティブに安全なのは「処理側が自分から見て安全なタイミングで
抜ける」やり方だけ。`on_progress` を各関数に引き回しているのと同じパターンで
`cancel_event` も引き回すことで、既存の構造を壊さずに追加できた。

**`cancel.py` を独立モジュールにした理由:** `PipelineCancelled` を
`pipeline.py` に置くと、`pipeline.py` が import している `transcribe.py` /
`vision.py` / `minutes.py` がそれを使うために `pipeline` を逆 import する形になり
循環 import になる。依存ゼロの `cancel.py` を切り出して全員がそこから import する
構成にした。

**チェックを入れた場所（効く/効かない）:**

| 場所 | 反応の速さ |
| --- | --- |
| 各ステージの開始前 | ほぼ即時 |
| フレーム解析ループ（VLM 呼び出しの合間） | 1フレーム分。体感で一番効く（最大60回ある） |
| 議事録のチャンク要約ループ | 1チャンク分 |
| faster-whisper のセグメント生成ループ | 1セグメント分（遅延生成なので途中で打ち切れば以降の生成も止まる） |
| mlx-whisper の文字起こし呼び出し中 | **反応しない**（結果を一括で返す1回のブロッキング呼び出し。呼び出し前のみチェック） |
| ffmpeg サブプロセス実行中 | **反応しない**（`subprocess.run` で待つだけ。Popen 化すれば中断可能だが今回は見送り） |

**中断時に残るもの:** `finally` で LLM クライアントは必ず閉じる。
`transcript/transcript.json` / `frames/frames.json` に加え、**フレーム解析の途中経過
（`frames/frame_notes.json`）も1枚ごとに保存済み**なので、次回実行時（9.5 の再開機構）は
解析済みのフレームからやり直さない。同様に議事録のチャンク要約
（`minutes/minutes_partials.json`）も終えた分から再開する。

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
