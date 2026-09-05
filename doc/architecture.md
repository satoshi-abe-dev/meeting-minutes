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
  │        ├─ save_frame_index → frames.json
  │        └─ reuse=True かつ frames.json があれば load_frames で再利用
  │
  ├─(4) vision.describe_frames     ローカル VLM で各フレームを要点化
  │        ├─ 1枚終えるたびに save_frame_notes → frame_notes.json へ逐次保存
  │        └─ reuse=True なら残っているフレームだけ解析（1枚単位で再開）
  │
  └─(5) minutes.generate_minutes   文字起こし＋フレーム要点 → 議事録 Markdown
           ├─ 長文はチャンク要約ごとに minutes_partials.json へ逐次保存
           │    → reuse=True なら残っている分は要約し直さない（1チャンク単位で再開）
           └─ save_minutes → minutes.md
```

`pipeline.run()` がこの順序と進捗通知、出力ディレクトリ（`output/<動画名>/`）の
管理を担当する。GUI・CLI・テストはすべて `run()` を呼ぶだけ。`run(..., reuse=True)`
（既定）は前回の中間生成物（transcript.json / frames.json / frame_notes.json /
minutes_partials.json）を再利用し、終わっている分をやり直さない（`--fresh` / GUI の
チェックで無効化）。進捗の `stage` は
`preflight → audio → transcribe → frames → vision → minutes → done`。
各工程・各チャンク・各フレームの完了メッセージには「所要 X」の実測時間が、
LLM/VLM を呼ぶ工程の開始メッセージには使用モデル名と「応答を待っています」が付く
（`pipeline._format_elapsed` / `minutes._format_elapsed` / `vision._format_elapsed`、
モジュールをまたいで共有するほどでもない小関数なので複製している）。

## モジュールの責務

| モジュール | 役割 | 外部依存 |
| --- | --- | --- |
| `config.py` | TOML＋環境変数から `Config` を作る。プロンプト読み込み。 | なし（標準 `tomllib`） |
| `cancel.py` | 協調的キャンセルの共通部品（`PipelineCancelled` / `check_cancel`） | なし |
| `ffmpeg_utils.py` | ffmpeg / ffprobe の存在確認・実行・動画長取得 | ffmpeg（システム） |
| `audio.py` | 動画 → wav | ffmpeg |
| `transcribe.py` | wav → `Segment` 配列、保存、テキスト整形。`backend` で mlx / faster-whisper を切替 | mlx-whisper / faster-whisper |
| `frames.py` | 動画 → `Frame` 配列（時刻付き画像） | ffmpeg |
| `llm_client.py` | OpenAI 互換サーバーへの `chat` / `describe_image` | httpx、ローカル LLM サーバー |
| `vision.py` | `Frame` → `FrameNote`（要点テキスト） | `llm_client` |
| `minutes.py` | `Segment`＋`FrameNote` → 議事録 Markdown。長文はチャンク要約→統合 | `llm_client` |
| `pipeline.py` | 全工程のオーケストレーション、進捗、`Deps` による差し替え | 上記すべて |
| `prefetch.py` | 解決後バックエンドの Whisper モデルを事前DL（`scripts/setup.sh` から） | huggingface_hub / faster-whisper |

ルート直下の `gui.py` / `cli.py` / `prefetch.py` は `src/` を `sys.path` に足して
対応する `meeting_minutes.*` を呼ぶ薄いランチャー。

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
