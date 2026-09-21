"""GUI 表示文言の ja / en カタログ（3 層共通の小さな基盤モジュール）。

view（`view/tk_main_window.py`）と presenter（`presenter/main.py`）の両方から
`t(key, language, **kwargs)` で引く。対象は GUI の画面文言だけ（文字起こし言語・
LLM プロンプト・議事録内容・CLI 出力は対象外）。

言語は起動時に 1 回決まる（実行中の切替はしない）。`ja` 側の文言は i18n 化前の
コードのリテラルを一字一句そのままコピーしてあり、`language="ja"`（既定）のときの
表示は従来と完全に一致する。
"""

from __future__ import annotations

SUPPORTED_LANGUAGES = ("ja", "en")
DEFAULT_LANGUAGE = "ja"


def normalize_language(language: str | None) -> str:
    """対応外・None・空は既定（ja）に丸める（他の設定項目のフォールバック方針に合わせる）。"""
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


# key -> {language -> テンプレート文字列}
# 動的な文（ログ等）は ja / en それぞれで自然な語順になるようテンプレートごと持つ。
_STRINGS: dict[str, dict[str, str]] = {
    # --- ウィンドウ / 静的ラベル（view）------------------------------
    "window.title": {
        "ja": "議事録生成AI（ローカル処理）",
        "en": "Meeting Minutes AI (Local Processing)",
    },
    "label.video_file": {"ja": "動画ファイル:", "en": "Video file:"},
    "button.choose": {"ja": "選択...", "en": "Choose..."},
    "label.unselected": {"ja": "未選択", "en": "Not selected"},
    "label.format": {"ja": "議事録フォーマット:", "en": "Minutes format:"},
    "radio.builtin": {"ja": "内蔵（既定）", "en": "Built-in (default)"},
    "radio.file": {"ja": "ファイルを選択", "en": "Choose a file"},
    "radio.auto": {
        "ja": "おまかせ（動画に合わせて自動生成）",
        "en": "Auto (generate to match the video)",
    },
    "label.settings": {
        "ja": "設定（config.toml で変更）",
        "en": "Settings (change in config.toml)",
    },
    "check.reuse": {"ja": "作成済みデータを利用する", "en": "Reuse existing data"},
    "button.run": {"ja": "議事録を作成", "en": "Create minutes"},
    "button.stop": {"ja": "中断", "en": "Stop"},
    # 進捗ラベルの起動直後の表示（実行が始まると工程名＋進捗に差し替わる）。
    "label.waiting": {"ja": "準備完了", "en": "Ready"},
    "button.open_minutes": {"ja": "議事録を開く", "en": "Open minutes"},
    "button.open_folder": {"ja": "出力フォルダーを開く", "en": "Open output folder"},
    "info.privacy": {
        "ja": (
            "※ 音声・映像・文字起こし・要約はすべてこの PC 内で処理します。"
            "外部サービスへは送信しません。\n"
            "（LLM・VLM はローカルサーバーのものを使用）"
        ),
        "en": (
            "* Audio, video, transcription and summarization are all processed on "
            "this PC. Nothing is sent to external services.\n"
            "(LLM/VLM run on a local server.)"
        ),
    },
    # --- ツールチップ（view）---------------------------------------
    "tooltip.builtin": {
        "ja": (
            "「内蔵」は、会議の内容に関わらず常に同じ見出し・構成（決定事項・宿題・"
            "議事の要点など）を使う既定のフォーマットです。内容に応じて動的に変わる"
            "ことはありません。お客様ごとの様式に合わせたい場合は、テンプレートファイルを"
            "用意してこのメニューから選んでください。"
            "見出し構成は templates/minutes_template_example.txt で確認できます（内蔵と同一）。"
        ),
        "en": (
            '"Built-in" always uses the same headings and structure (decisions, '
            "action items, discussion points, etc.) regardless of the meeting "
            "content. It does not change dynamically. To match a client's own "
            "format, prepare a template file and choose it from this menu. "
            "You can see the heading structure in "
            "templates/minutes_template_example.txt (identical to the built-in one)."
        ),
    },
    "tooltip.auto": {
        "ja": (
            "「おまかせ」は、動画の内容に合わせて議事録の見出し構成を毎回 AI に"
            "提案させます（例: 団体旅行の説明会なら「スケジュール」「持ち物」"
            "「注意事項」など）。生成された構成は出力フォルダーの work/structure_used.txt に"
            "保存され、気に入ればテンプレートファイルとして保存して、以後は"
            "「ファイルを選択」で固定できます。生成に失敗した場合は自動的に"
            "「内蔵」で作成します。"
        ),
        "en": (
            '"Auto" asks the AI to propose the minutes\' heading structure to '
            'match each video\'s content (e.g. for a group-tour briefing: '
            '"Schedule", "What to bring", "Notes"). The generated structure is '
            "saved as work/structure_used.txt in the output folder; if you like it, "
            'save it as a template file and pin it via "Choose a file". If '
            'generation fails, "Built-in" is used automatically.'
        ),
    },
    "tooltip.reuse": {
        "ja": (
            "チェックを入れると、前回までの中間ファイルを再利用して、"
            "処理を早く終えられます。"
        ),
        "en": (
            "When checked, intermediate files from previous runs are reused to "
            "finish faster."
        ),
    },
    "tooltip.open_minutes": {
        "ja": (
            "Word 版（minutes.docx）を開きます。無い場合は Markdown 版"
            "（minutes.md）を開きます。"
        ),
        "en": (
            "Opens the Word version (minutes.docx). If it wasn't produced, opens "
            "the Markdown version (minutes.md) instead."
        ),
    },
    # --- ファイル選択ダイアログ（view）--------------------------------
    "dialog.choose_video.title": {
        "ja": "打ち合わせ動画を選択",
        "en": "Choose a meeting video",
    },
    "dialog.choose_template.title": {
        "ja": "議事録テンプレートを選択",
        "en": "Choose a minutes template",
    },
    "filetype.video": {"ja": "動画ファイル", "en": "Video files"},
    "filetype.text": {"ja": "テキスト", "en": "Text"},
    "filetype.all": {"ja": "すべてのファイル", "en": "All files"},
    # --- 設定サマリ（presenter が組み立て、view に表示）----------------
    "cfg.transcribe": {
        "ja": "文字起こし: {model} / {note}",
        "en": "Transcription: {model} / {note}",
    },
    # backend が明示指定（mlx / faster-whisper）で、実際に使う値と一致するとき。
    "cfg.backend_explicit": {
        "ja": "backend={backend}",
        "en": "backend={backend}",
    },
    # backend=auto（や未知の値）を、この環境の実際のバックエンドへ読み替えたとき。
    "cfg.backend_resolved": {
        "ja": "backend={configured}（自動選択: {actual}）",
        "en": "backend={configured} (auto-selected: {actual})",
    },
    "cfg.vlm": {"ja": "VLM: {model}", "en": "VLM: {model}"},
    "cfg.llm": {"ja": "LLM: {model}", "en": "LLM: {model}"},
    "cfg.endpoint": {
        "ja": "LLM・VLM 接続先: {url}",
        "en": "LLM/VLM endpoint: {url}",
    },
    # --- 進捗工程ラベル / ステータス（presenter）---------------------
    "stage.preflight": {"ja": "サーバー確認", "en": "Server check"},
    "stage.audio": {"ja": "音声抽出", "en": "Audio extraction"},
    "stage.transcribe": {"ja": "文字起こし", "en": "Transcription"},
    "stage.frames": {"ja": "フレーム抽出", "en": "Frame extraction"},
    "stage.vision": {"ja": "フレーム解析", "en": "Frame analysis"},
    "stage.minutes": {"ja": "議事録生成", "en": "Minutes generation"},
    "stage.done": {"ja": "完了", "en": "Done"},
    "stage_text.done": {"ja": "完了", "en": "Done"},
    "stage_text.error": {"ja": "エラー", "en": "Error"},
    "stage_text.cancelled": {"ja": "中断しました", "en": "Cancelled"},
    "stage_text.counter": {
        "ja": "（{current}/{total}）",
        "en": "({current}/{total})",
    },
    # --- エラーダイアログ（presenter）------------------------------
    "dialog.format_error.title": {"ja": "議事録フォーマット", "en": "Minutes format"},
    "dialog.format_error.message": {
        "ja": (
            "「ファイルを選択」が選ばれていますが、テンプレートファイルが"
            "選択されていません。\n「選択...」からファイルを選ぶか、"
            "「内蔵（既定）」を選んでください。"
        ),
        "en": (
            '"Choose a file" is selected but no template file has been chosen.\n'
            'Choose a file with "Choose...", or select "Built-in (default)".'
        ),
    },
    "dialog.error.title": {"ja": "エラー", "en": "Error"},
    # --- ログ（presenter）---------------------------------------
    "log.start": {"ja": "開始: {name}{suffix}", "en": "Started: {name}{suffix}"},
    "log.start_suffix_fresh": {
        "ja": "（最初からやり直す）",
        "en": " (starting over)",
    },
    "log.format": {"ja": "議事録フォーマット: {desc}", "en": "Minutes format: {desc}"},
    "log.format_desc.auto": {
        "ja": "おまかせ（動画に合わせて自動生成）",
        "en": "Auto (generate to match the video)",
    },
    "log.format_desc.builtin": {"ja": "内蔵（既定）", "en": "Built-in (default)"},
    "log.stop_requested": {
        "ja": (
            "中断を要求しました…現在の工程の区切りまで少し待ちます"
            "（mlx-whisper の文字起こし中や ffmpeg 実行中は即座には止まりません）"
        ),
        "en": (
            "Stop requested... waiting until the current step reaches a "
            "checkpoint (it will not stop immediately during mlx-whisper "
            "transcription or ffmpeg execution)."
        ),
    },
    "log.minutes_path": {"ja": "議事録: {path}", "en": "Minutes: {path}"},
    "log.counts": {
        "ja": "文字起こし {segments} 区間 / フレーム {frames} 枚",
        "en": "Transcript: {segments} segments / Frames: {frames}",
    },
    "log.warning": {"ja": "警告: {msg}", "en": "Warning: {msg}"},
    "log.failure": {"ja": "失敗: {exc}", "en": "Failed: {exc}"},
    "log.cancelled": {
        "ja": (
            "中断しました。ここまでの文字起こし・フレームは output に残っており、"
            "次回の実行（「作成済みデータを利用する」にチェックした状態）"
            "で再利用されます。"
        ),
        "en": (
            "Cancelled. The transcription and frames so far remain in output and "
            'will be reused on the next run (with "Reuse existing data" checked).'
        ),
    },
    # --- パイプライン内部の進捗メッセージ（model 層。on_progress の message 引数）---
    # Issue #100: pipeline.py / minutes.py / vision.py / transcribe.py が
    # on_progress へ渡す message は Issue #55 の対象外だった。GUI の --lang en では
    # ここも英語にする（CLI は language 未指定＝ja のまま）。
    "pmsg.pre_wait": {
        "ja": "LLM サーバーの応答を待っています…（LLM: {model} / VLM: {vlm}）",
        "en": "Waiting for the LLM server… (LLM: {model} / VLM: {vlm})",
    },
    "pmsg.pre_ok": {"ja": "LLM サーバー確認 OK", "en": "LLM server check OK"},
    "pmsg.audio_extracting": {
        "ja": "動画から音声を抽出中",
        "en": "Extracting audio from the video",
    },
    "pmsg.warn_duration": {
        "ja": "動画長の取得に失敗: {exc}",
        "en": "Failed to get the video length: {exc}",
    },
    "pmsg.audio_done": {
        "ja": "音声抽出が完了（所要 {elapsed}）",
        "en": "Audio extraction done (elapsed {elapsed})",
    },
    "pmsg.transcribe_reuse": {
        "ja": "既存の文字起こしを再利用（{n} 区間）",
        "en": "Reusing the existing transcript ({n} segments)",
    },
    "pmsg.warn_transcript_reuse": {
        "ja": "transcript.json の再利用に失敗、作り直します: {exc}",
        "en": "Could not reuse transcript.json, regenerating: {exc}",
    },
    "pmsg.transcribe_start": {
        "ja": "文字起こしを開始（モデル: {model} / backend={backend}）",
        "en": "Starting transcription (model: {model} / backend={backend})",
    },
    "pmsg.transcribe_done": {
        "ja": "文字起こし完了（{n} 区間、所要 {elapsed}）",
        "en": "Transcription done ({n} segments, elapsed {elapsed})",
    },
    "pmsg.frames_reuse": {
        "ja": "既存のフレームを再利用（{n} 枚）",
        "en": "Reusing the existing frames ({n})",
    },
    "pmsg.warn_frames_reuse": {
        "ja": "frames/frames.json の再利用に失敗、作り直します: {exc}",
        "en": "Could not reuse frames/frames.json, regenerating: {exc}",
    },
    "pmsg.frames_extracting": {"ja": "フレームを抽出中", "en": "Extracting frames"},
    "pmsg.frames_done": {
        "ja": "フレーム抽出完了（{n} 枚、所要 {elapsed}）",
        "en": "Frame extraction done ({n}, elapsed {elapsed})",
    },
    "pmsg.vision_analyzing": {
        "ja": "フレームを解析中（モデル: {vlm}）",
        "en": "Analyzing frames (model: {vlm})",
    },
    "pmsg.vision_done": {
        "ja": "フレーム解析完了（所要 {elapsed}）",
        "en": "Frame analysis done (elapsed {elapsed})",
    },
    "pmsg.done": {"ja": "完了: {path}", "en": "Done: {path}"},
    "pmsg.err_video_not_found": {
        "ja": "動画ファイルが見つかりません: {path}",
        "en": "Video file not found: {path}",
    },
    # vision.py
    "pmsg.vis_reuse": {
        "ja": "既存のフレーム解析を再利用（{n}/{total}）",
        "en": "Reusing the existing frame analysis ({n}/{total})",
    },
    "pmsg.vis_frame_analyzing": {
        "ja": "{ts} のフレームを解析中…応答を待っています",
        "en": "Analyzing the frame at {ts}… waiting for a response",
    },
    "pmsg.vis_frame_done": {
        "ja": "{ts} のフレーム解析が完了（所要 {elapsed}）",
        "en": "Frame at {ts} analyzed (elapsed {elapsed})",
    },
    # transcribe.py
    "pmsg.stt_preparing": {
        "ja": "文字起こしモデルを準備中…",
        "en": "Preparing the transcription model…",
    },
    "pmsg.stt_mlx_running": {
        "ja": "mlx-whisper で文字起こし中（完了まで進捗は動きません）",
        "en": "Transcribing with mlx-whisper (progress will not move until it finishes)",
    },
    "pmsg.stt_model_missing": {
        "ja": (
            "文字起こしモデル（{repo}）がローカルにありません。"
            "先に `bash scripts/setup.sh` を実行してモデルを取得してください"
            "（アプリ実行時は自動ダウンロードしません）。"
        ),
        "en": (
            "The transcription model ({repo}) is not available locally. "
            "Run `bash scripts/setup.sh` first to fetch it "
            "(the app does not download models at runtime)."
        ),
    },
    # minutes.py
    "pmsg.warn_prefix": {"ja": "警告: {msg}", "en": "Warning: {msg}"},
    "pmsg.tpl_unreadable": {
        "ja": "テンプレート {p} を読めませんでした。内蔵テンプレートを使います: {exc}",
        "en": "Could not read the template {p}; using the built-in one: {exc}",
    },
    "pmsg.tpl_empty": {
        "ja": "テンプレート {p} が空です。内蔵テンプレートを使います",
        "en": "The template {p} is empty; using the built-in one",
    },
    "pmsg.struct_generating": {
        "ja": "議事録の型を自動生成中…応答を待っています（モデル: {model}）",
        "en": (
            "Auto-generating the minutes structure… waiting for a response "
            "(model: {model})"
        ),
    },
    "pmsg.struct_gen_failed": {
        "ja": "警告: 議事録の型の自動生成に失敗しました（{exc}）。内蔵テンプレートを使います",
        "en": (
            "Warning: auto-generating the minutes structure failed ({exc}); "
            "using the built-in template"
        ),
    },
    "pmsg.struct_invalid": {
        "ja": "警告: 自動生成された議事録の型が不正（{reason}）でした。内蔵テンプレートを使います",
        "en": (
            "Warning: the auto-generated minutes structure was invalid ({reason}); "
            "using the built-in template"
        ),
    },
    "pmsg.struct_reason_empty": {"ja": "空の応答", "en": "empty response"},
    "pmsg.struct_reason_missing": {
        "ja": "プレースホルダー欠落 {names}",
        "en": "missing placeholders {names}",
    },
    "pmsg.struct_too_big": {
        "ja": (
            "警告: 自動生成された議事録の型が大きすぎます（文字起こしを抜いても"
            "コンテキスト長に収まりません）。内蔵テンプレートを使います"
        ),
        "en": (
            "Warning: the auto-generated minutes structure is too large (it does not "
            "fit the context window even without the transcript); using the built-in "
            "template"
        ),
    },
    "pmsg.struct_rm_failed": {
        "ja": "警告: 古い {name} を削除できませんでした（{exc}）",
        "en": "Warning: could not delete the old {name} ({exc})",
    },
    "pmsg.struct_save_failed": {
        "ja": "警告: {name} を保存できませんでした（{exc}）",
        "en": "Warning: could not save {name} ({exc})",
    },
    "pmsg.struct_saved": {
        "ja": "議事録の型を自動生成しました（{name} に保存）",
        "en": "Auto-generated the minutes structure (saved to {name})",
    },
    "pmsg.chunk_wait": {
        "ja": "部分要約 {i}/{n} の応答を待っています…（モデル: {model}）",
        "en": "Partial summary {i}/{n}: waiting for a response… (model: {model})",
    },
    "pmsg.chunk_done": {
        "ja": "部分要約 {i}/{n} 完了（所要 {elapsed}）",
        "en": "Partial summary {i}/{n} done (elapsed {elapsed})",
    },
    "pmsg.partials_reuse": {
        "ja": "既存の部分要約を再利用（{n}/{m}）",
        "en": "Reusing the existing partial summaries ({n}/{m})",
    },
    "pmsg.partials_stale": {
        "ja": (
            "保存済みの部分要約は分割設定が変わっている（または旧形式）ため使わず、"
            "最初から要約し直します"
        ),
        "en": (
            "The saved partial summaries have a different chunking config (or an old "
            "format), so they are not reused; re-summarizing from scratch"
        ),
    },
    "pmsg.ctx_too_small": {
        "ja": (
            "警告: コンテキスト長（約 {ctx} トークン）が小さすぎます。"
            "使う LLM のコンテキスト長を増やすか（LM Studio なら Context Length）、"
            "config.toml の [ai] context_tokens / chunk_size_chars を見直してください"
        ),
        "en": (
            "Warning: the context window (~{ctx} tokens) is too small. Increase the "
            "LLM's context length (in LM Studio, Context Length), or review "
            "[ai] context_tokens / chunk_size_chars in config.toml"
        ),
    },
    "pmsg.minutes_generating": {
        "ja": "議事録を生成中…応答を待っています（モデル: {model}）",
        "en": "Generating the minutes… waiting for a response (model: {model})",
    },
    "pmsg.minutes_generated": {
        "ja": "議事録を生成しました（所要 {elapsed}）",
        "en": "Minutes generated (elapsed {elapsed})",
    },
    "pmsg.docx_saved": {
        "ja": "Word 版も書き出しました（{path}）",
        "en": "Also wrote the Word version ({path})",
    },
    "pmsg.warn_docx_failed": {
        "ja": "警告: Word 版（.docx）の書き出しに失敗しました（minutes.md は生成済み）: {exc}",
        "en": (
            "Warning: failed to write the Word (.docx) version (minutes.md was "
            "written): {exc}"
        ),
    },
    "pmsg.switch_to_split": {
        "ja": (
            "一発生成はコンテキスト長（約 {ctx} トークン）に収まらないため"
            "分割生成に切り替えます"
        ),
        "en": (
            "Single-pass does not fit the context window (~{ctx} tokens); "
            "switching to split generation"
        ),
    },
    "pmsg.merging": {
        "ja": "議事録に統合中…応答を待っています（モデル: {model}）",
        "en": "Merging into the minutes… waiting for a response (model: {model})",
    },
    "pmsg.leaked_instructions": {
        "ja": (
            "警告: テンプレートの指示文（丸括弧の説明）が議事録にそのまま残っている"
            "可能性があります（{n} 箇所。例: {head}）。"
            "より大きいモデルを使う・テンプレートの丸括弧を減らすと改善することがあります"
        ),
        "en": (
            "Warning: the template's instruction text (the parenthetical notes) may "
            "have been left verbatim in the minutes ({n} place(s); e.g. {head}). "
            "Using a larger model or reducing the parentheses in the template can help"
        ),
    },
    "pmsg.merge_truncated": {
        "ja": (
            "警告: 部分要約が多く統合リクエストがコンテキスト長を超えるため、"
            "統合入力の末尾を一部省略しました"
        ),
        "en": (
            "Warning: there are many partial summaries and the merge request exceeds "
            "the context window, so the tail of the merge input was truncated"
        ),
    },
    # llm_client.py（接続エラー時のヒント。例外メッセージとしてエラーダイアログ・ログに出る）
    "pmsg.llm_hint_conn": {
        "ja": (
            "ローカル LLM サーバー（{base_url}）に接続できません。サーバーが起動していて "
            "OpenAI 互換 API を待ち受けているか確認してください。"
            "LM Studio なら Settings → Local Models → Local Model API で "
            "『Local API server』を ON（Running）に、旧 UI では Developer タブの "
            "Local Server を Start。"
        ),
        "en": (
            "Cannot connect to the local LLM server ({base_url}). Check that the "
            "server is running and serving an OpenAI-compatible API. In LM Studio, "
            "turn on \"Local API server\" (Running) under Settings → Local Models → "
            "Local Model API; in the old UI, Start the Local Server on the Developer "
            "tab."
        ),
    },
    "pmsg.llm_hint_timeout": {
        "ja": (
            "【タイムアウト】ローカル LLM サーバーへのリクエストが {timeout:.0f} 秒以内に"
            "終わらず、タイムアウトしました。サーバー自体は動いていて、応答の生成に時間が"
            "かかっているだけの可能性が高いです"
            "（大きいモデルほど、また出力トークン数が多いほど時間がかかります）。"
            "config.toml の [ai] timeout を増やしてください（例: 600）。"
        ),
        "en": (
            "[Timeout] The request to the local LLM server did not finish within "
            "{timeout:.0f} s and timed out. The server is most likely up and just "
            "taking a long time to generate a response (larger models and more "
            "output tokens take longer). Increase [ai] timeout in config.toml "
            "(e.g. 600)."
        ),
    },
    "pmsg.llm_hint_model": {
        "ja": (
            "モデルがサーバーにロードされていない可能性があります。"
            "config の model / vlm_model がサーバーの返すモデル ID"
            "（`curl {base_url}/models` で確認可）と一致しているか、対象モデルが"
            "ロード済みかを確認してください。"
            "LM Studio なら『Just-in-time model loading』を ON にするか、"
            "Loaded Instances で対象モデルをロード。"
        ),
        "en": (
            "The model may not be loaded on the server. Check that model / vlm_model "
            "in the config match the model IDs the server returns (check with "
            "`curl {base_url}/models`) and that the target model is loaded. In "
            "LM Studio, turn on \"Just-in-time model loading\", or load the target "
            "model under Loaded Instances."
        ),
    },
    "pmsg.llm_hint_context": {
        "ja": (
            "プロンプトがモデルのコンテキスト長を超えています。使う LLM のコンテキスト長を "
            "32768 以上にしてください（設定方法はサーバー依存）。"
            "LM Studio ならこの LLM をロードするときに Context Length を 32768 以上に"
            "設定して読み込み直す（一度ロード済みなら Eject してから設定し直す）。"
            "詳しくは docs/models.md の「コンテキスト長の設定」を参照。"
            "コンテキスト長を大きくできない場合は、config.toml の "
            "[ai] chunk_trigger_chars / chunk_size_chars を小さくすると分割要約に切り替わり、"
            "1 回あたりのプロンプトが短くなります。"
        ),
        "en": (
            "The prompt exceeds the model's context window. Set the LLM's context "
            "length to 32768 or more (how to do this depends on the server). In "
            "LM Studio, set Context Length to 32768 or more when loading this LLM and "
            "reload it (if already loaded, Eject first and set it again). See "
            "\"Setting the context length\" in docs/models.md. If you cannot increase "
            "it, lowering [ai] chunk_trigger_chars / chunk_size_chars in config.toml "
            "switches to split summarization and shortens each prompt."
        ),
    },
    "pmsg.llm_hint_reasoning": {
        "ja": (
            "モデルが「思考」（reasoning）に max_tokens を使い切り、本文を1文字も"
            "出力できませんでした（reasoning は {reasoning_len} 文字生成、本文は空、"
            "finish_reason={finish_reason!r}）。Qwen3 系などの推論モデルは、入力が長い"
            "ほど思考に多くのトークンを使います。config.toml の [ai] max_tokens を"
            "増やすか、サーバー側でこのモデルの reasoning（思考の強さ）を下げてください"
            "（LM Studio なら reasoning 設定、または非推論の Instruct 系モデルへ）。"
        ),
        "en": (
            "The model used up max_tokens on \"reasoning\" and produced no body text "
            "at all (reasoning generated {reasoning_len} chars, body empty, "
            "finish_reason={finish_reason!r}). Reasoning models such as the Qwen3 "
            "family spend more tokens on thinking as the input gets longer. Increase "
            "[ai] max_tokens in config.toml, or lower this model's reasoning effort "
            "on the server side (in LM Studio, the reasoning setting; or switch to a "
            "non-reasoning Instruct model)."
        ),
    },
    "pmsg.llm_err_http": {
        "ja": "LLM サーバーがエラーを返しました (HTTP {status}): {body}",
        "en": "The LLM server returned an error (HTTP {status}): {body}",
    },
    "pmsg.llm_err_unparsable": {
        "ja": "LLM サーバーの応答を解釈できません: {body}",
        "en": "Could not parse the LLM server's response: {body}",
    },
    "pmsg.llm_err_detail": {"ja": "\n詳細: {exc}", "en": "\nDetails: {exc}"},
    "pmsg.llm_err_preflight": {
        "ja": "起動前チェックに失敗しました（モデル {model}）。\n{detail}",
        "en": "Preflight check failed (model {model}).\n{detail}",
    },
}


def format_elapsed(seconds: float, language: str = DEFAULT_LANGUAGE) -> str:
    """処理にかかった時間の表示用。旧: 各 model モジュールの `_format_elapsed`
    （日本語ハードコード）を i18n 対応で一本化した。"""
    if seconds < 60:
        return f"{seconds:.1f}秒" if language != "en" else f"{seconds:.1f}s"
    minutes, sec = divmod(round(seconds), 60)
    if minutes < 60:
        return (
            f"{minutes}分{sec:02d}秒" if language != "en" else f"{minutes}m{sec:02d}s"
        )
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分" if language != "en" else f"{hours}h{minutes:02d}m"


def t(key: str, language: str, *, default: str | None = None, **kwargs: object) -> str:
    """カタログから文言を引く。

    language が対応外なら ja にフォールバック。key 自体が無い場合は default
    （指定なければ key 文字列）を返す（旧 `_STAGE_LABEL.get(stage, stage)` のように
    未知値でも壊れないようにするため）。kwargs があれば str.format で差し込む。
    """
    entry = _STRINGS.get(key)
    if entry is None:
        return default if default is not None else key
    text = entry.get(language) or entry.get(DEFAULT_LANGUAGE) or key
    return text.format(**kwargs) if kwargs else text
