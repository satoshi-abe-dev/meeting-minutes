"""View（抽象層）— メイン画面

Presenter が依存する「契約」だけを定義する。Tkinter 実装は tk_main_window.py。
Presenter はこの契約と Model（``meeting_minutes.*``）にだけ依存し、tkinter を
一切知らない。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable


class MainView(ABC):
    # --- ハンドラ登録（ボタン／ラジオの操作を Presenter へ渡す）------------
    @abstractmethod
    def set_on_choose_video(self, handler: Callable[[], None]) -> None:
        """「動画ファイル: 選択...」ボタン押下時のハンドラを登録する。"""

    @abstractmethod
    def set_on_pick_template(self, handler: Callable[[], None]) -> None:
        """「ファイルを選択: 選択...」ボタン押下時のハンドラを登録する。"""

    @abstractmethod
    def set_on_start(self, handler: Callable[[], None]) -> None:
        """「議事録を作成」ボタン押下時のハンドラを登録する。"""

    @abstractmethod
    def set_on_stop(self, handler: Callable[[], None]) -> None:
        """「中断」ボタン押下時のハンドラを登録する。"""

    @abstractmethod
    def set_on_open_minutes(self, handler: Callable[[], None]) -> None:
        """「議事録を開く」ボタン押下時のハンドラを登録する。"""

    @abstractmethod
    def set_on_open_folder(self, handler: Callable[[], None]) -> None:
        """「出力フォルダーを開く」ボタン押下時のハンドラを登録する。"""

    # --- 入力状態の取得 -------------------------------------------------
    @abstractmethod
    def get_format_mode(self) -> str:
        """議事録フォーマットの選択（"builtin" / "file" / "auto"）を返す。"""

    @abstractmethod
    def get_reuse(self) -> bool:
        """「作成済みデータを利用する」チェックの状態を返す。"""

    # --- 画面の更新 ---------------------------------------------------
    @abstractmethod
    def set_format_mode(self, mode: str) -> None:
        """議事録フォーマットの選択を切り替える（テンプレファイル選択後などに使う）。"""

    @abstractmethod
    def set_config_summary(self, text: str) -> None:
        """「設定（config.toml で変更）」欄の本文を設定する。"""

    @abstractmethod
    def set_video_name(self, name: str) -> None:
        """選択された動画ファイル名を表示する。"""

    @abstractmethod
    def set_template_name(self, name: str) -> None:
        """「ファイルを選択」側で選択中のテンプレートファイル名を表示する。"""

    @abstractmethod
    def set_start_enabled(self, enabled: bool) -> None:
        """「議事録を作成」ボタンの有効／無効を切り替える。"""

    @abstractmethod
    def set_stop_enabled(self, enabled: bool) -> None:
        """「中断」ボタンの有効／無効を切り替える。"""

    @abstractmethod
    def set_open_minutes_enabled(self, enabled: bool) -> None:
        """「議事録を開く」ボタンの有効／無効を切り替える。"""

    @abstractmethod
    def set_open_folder_enabled(self, enabled: bool) -> None:
        """「出力フォルダーを開く」ボタンの有効／無効を切り替える。"""

    @abstractmethod
    def set_progress(self, value: int) -> None:
        """進捗バーの値を設定する（0〜1000）。"""

    @abstractmethod
    def set_stage_text(self, text: str) -> None:
        """進捗ラベル（工程名＋カウンタ）の文字列を設定する。"""

    @abstractmethod
    def append_log(self, text: str) -> None:
        """ログ欄に 1 行追記する（末尾に改行を付けてスクロール）。"""

    @abstractmethod
    def show_error(self, title: str, message: str) -> None:
        """エラーダイアログを表示する。"""

    # --- ファイル選択ダイアログ / OS 連携 ------------------------------
    @abstractmethod
    def ask_video_path(self) -> str | None:
        """動画ファイルをユーザーに選ばせる。キャンセル時は None。"""

    @abstractmethod
    def ask_template_path(self) -> str | None:
        """議事録テンプレートファイルをユーザーに選ばせる。キャンセル時は None。"""

    @abstractmethod
    def open_in_file_manager(self, path: Path) -> None:
        """指定パスを OS のファイルマネージャ／既定アプリで開く。"""

    # --- イベントループ -----------------------------------------------
    @abstractmethod
    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """delay_ms ミリ秒後に callback を 1 回呼ぶ（tkinter の after() の薄いラッパー）。

        定期実行したい場合は callback 自身の中で再度 schedule() を呼ぶ。
        ワーカースレッドからの結果を UI スレッドで受け取るポーリングに使う。
        """

    @abstractmethod
    def run(self) -> None:
        """イベントループを開始する（戻ってこない）。"""
