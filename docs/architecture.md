# アーキテクチャ

## 全体の流れ

```
動画ファイル
  │
  ├─(0) client.preflight           LLM/VLM モデルへ極小リクエスト。落ちるならここで中断
  │
  ├─(1) audio.extract_audio        ffmpeg で 16kHz/モノラル wav を作る
  │        └─ transcript/audio.wav （文字起こしと同じフォルダーにまとめる）
  │
  ├─(2) transcribe.transcribe_wav  時刻付き文字起こし（mlx-whisper / faster-whisper）
  │        ├─ save_transcript → transcript/transcript.json / transcript.txt
  │        └─ reuse=True かつ transcript.json があれば load_transcript で再利用
  │
  ├─(3) frames.extract_frames      ffmpeg でシーン変化＋一定間隔のフレーム抽出
  │        ├─ save_frame_index → frames/frames.json
  │        └─ reuse=True かつ frames/frames.json があれば load_frames で再利用
  │
  ├─(4) vision.describe_frames     ローカル VLM で各フレームを要点化
  │        ├─ 1枚終えるたびに save_frame_notes → frames/frame_notes.json へ逐次保存
  │        └─ reuse=True なら残っているフレームだけ解析（1枚単位で再開）
  │
  └─(5) minutes.generate_minutes   文字起こし＋フレーム要点 → 議事録 Markdown
           ├─ 長文はチャンク要約ごとに work/minutes_partials.json へ逐次保存
           │    → reuse=True なら残っている分は要約し直さない（1チャンク単位で再開）
           ├─ save_minutes → minutes.md
           └─ save_minutes_docx → minutes.docx（.md と同内容の Word 版。失敗しても警告のみ）
```

`pipeline.run()` がこの順序と進捗通知、出力ディレクトリ（`output/<動画名>/`）の
管理を担当する。GUI・CLI・テストはいずれも `run()` を呼び出す。

- `run(..., reuse=True)`（既定）は前回の中間生成物（transcript/transcript.json /
  frames/frames.json / frames/frame_notes.json / work/minutes_partials.json）を
  再利用し、終わっている分をやり直さない（`--fresh` / GUI のチェックで無効化）
- 進捗の `stage` は `preflight → audio → transcribe → frames → vision → minutes → done`
- 各工程・各チャンク・各フレームの完了メッセージには「所要 X」の実測時間が付く
- LLM/VLM を呼ぶ工程の開始メッセージには使用モデル名と「応答を待っています」が付く
  （`pipeline._format_elapsed` / `minutes._format_elapsed` / `vision._format_elapsed`、
  モジュールをまたいで共有するほどでもない小関数なので複製している）

## モジュールの責務

コードは役割ごとに分かれている。

- 実処理（Model）は `src/meeting_minutes/model/` にまとまっている
- GUI は `src/meeting_minutes/view/` + `src/meeting_minutes/presenter/`
- 実行の入口は `src/meeting_minutes/` 直下の `gui.py` / `cli.py` /
  `download_transcribe_model.py`
- 下表の `download_transcribe_model.py` 以外の 10 モジュールが `model/` 配下
  （例: `model/pipeline.py` ⇔ `meeting_minutes.model.pipeline`）

| モジュール | 役割 | 外部依存 |
| --- | --- | --- |
| `model/config.py` | TOML＋環境変数から `Config` を作る。プロンプト読み込み。 | なし（標準 `tomllib`） |
| `model/cancel.py` | 協調的キャンセルの共通部品（`PipelineCancelled` / `check_cancel`） | なし |
| `model/ffmpeg_utils.py` | ffmpeg / ffprobe の存在確認・実行・動画長取得 | ffmpeg（システム） |
| `model/audio.py` | 動画 → wav | ffmpeg |
| `model/transcribe.py` | wav → `Segment` 配列、保存、テキスト整形。`backend` で mlx / faster-whisper を切替 | mlx-whisper / faster-whisper |
| `model/frames.py` | 動画 → `Frame` 配列（時刻付き画像） | ffmpeg |
| `model/llm_client.py` | OpenAI 互換サーバーへの `chat` / `describe_image` | httpx、ローカル LLM サーバー |
| `model/vision.py` | `Frame` → `FrameNote`（要点テキスト） | `llm_client` |
| `model/minutes.py` | `Segment`＋`FrameNote` → 議事録 Markdown。長文はチャンク要約→統合 | `llm_client` |
| `model/pipeline.py` | 全工程のオーケストレーション、進捗、`Deps` による差し替え | 上記すべて |
| `i18n.py` | GUI 表示文言のカタログ（`{key: {"ja", "en"}}`）と `t(key, language, **kwargs)`。`view/` と `presenter/` が共有（→ `DESIGN.md` 8.8 節） | なし |
| `download_transcribe_model.py` | 解決後バックエンドの Whisper モデルを取得（`scripts/setup.sh` から） | huggingface_hub / faster-whisper |

## エントリポイント

`src/meeting_minutes/` 配下の `gui.py` / `cli.py` / `download_transcribe_model.py` が実行の入口。

- GUI は `view/` + `presenter/` を組み立てて起動する薄いラッパー（→ `DESIGN.md` 8.5 節）
- CLI / モデル取得は argparse + `meeting_minutes.model.*`（取得スクリプトは自身が
  model 外）の呼び出し
- いずれも冒頭に `__package__` ブートストラップがあり、`python src/meeting_minutes/gui.py`
  のようなファイル指定でも `python -m meeting_minutes.gui`（`cd src` か
  `PYTHONPATH=src` が要る）でも動く（→ `DESIGN.md` 8.6 節）

`gui.py` は `--lang {ja,en}` を受け付ける。

- 指定があればその回だけ表示言語を上書きする
- 省略時は `config.toml` の `[gui] language`（既定 `ja`、不正値は `ja` 扱い）に従う
- 影響するのは GUI の画面文言だけ（→ `DESIGN.md` 8.8 節）

## 進捗通知

`on_progress(stage, current, total, message)` を全工程で共通に使う。

- `stage`: `"preflight" | "audio" | "transcribe" | "frames" | "vision" | "minutes" | "done"`
- `current` / `total`: その工程内の進捗（`total=0` は不定）
- CLI はテキスト行、GUI は `queue` 経由で受けて進捗バーとログに反映する。

## テスト用の差し替え（`Deps`）

`pipeline.Deps` が各工程の関数参照を保持する。テストではダミー関数を渡した
`Deps` を `run(..., deps=fake_deps)` に渡し、ffmpeg も LLM も呼ばずに
「工程の順序」「進捗コールバックの内容」「出力パスの組み立て」を検証する。

## データ構造

- `transcribe.Segment(start: float, end: float, text: str)`
- `frames.Frame(timestamp: float, path: Path)`
- `vision.FrameNote(timestamp: float, path: str, description: str)`
- `minutes.MinutesMeta(title, datetime_hint, duration_hint)`
- `pipeline.PipelineResult(...)` — 生成物のパス一式と件数、警告
