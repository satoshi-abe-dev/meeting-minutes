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
    "label.waiting": {"ja": "ログ", "en": "Log"},
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
        ),
        "en": (
            '"Built-in" always uses the same headings and structure (decisions, '
            "action items, discussion points, etc.) regardless of the meeting "
            "content. It does not change dynamically. To match a client's own "
            "format, prepare a template file and choose it from this menu."
        ),
    },
    "tooltip.auto": {
        "ja": (
            "「おまかせ」は、動画の内容に合わせて議事録の見出し構成を毎回 AI に"
            "提案させます（例: 団体旅行の説明会なら「スケジュール」「持ち物」"
            "「注意事項」など）。生成された構成は出力フォルダーの structure_used.txt に"
            "保存され、気に入ればテンプレートファイルとして保存して、以後は"
            "「ファイルを選択」で固定できます。生成に失敗した場合は自動的に"
            "「内蔵」で作成します。"
        ),
        "en": (
            '"Auto" asks the AI to propose the minutes\' heading structure to '
            'match each video\'s content (e.g. for a group-tour briefing: '
            '"Schedule", "What to bring", "Notes"). The generated structure is '
            "saved as structure_used.txt in the output folder; if you like it, "
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
        "ja": "文字起こし: {model}（{note}）",
        "en": "Transcription: {model} ({note})",
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
}


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
