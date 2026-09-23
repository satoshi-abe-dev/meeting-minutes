"""View (abstraction layer) — main window

Defines only the "contract" the Presenter depends on. The Tkinter
implementation is tk_main_window.py. The Presenter depends only on this
contract and the Model (``meeting_minutes.model.*``), and knows nothing
about tkinter at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path


class MainView(ABC):
    # --- Handler registration (passes button/radio actions to the Presenter) ------------
    @abstractmethod
    def set_on_choose_video(self, handler: Callable[[], None]) -> None:
        """Register the handler for when the "Video file: Choose..." button is pressed."""

    @abstractmethod
    def set_on_pick_template(self, handler: Callable[[], None]) -> None:
        """Register the handler for when the "Choose a file: Choose..." button is pressed."""

    @abstractmethod
    def set_on_start(self, handler: Callable[[], None]) -> None:
        """Register the handler for when the "Create minutes" button is pressed."""

    @abstractmethod
    def set_on_stop(self, handler: Callable[[], None]) -> None:
        """Register the handler for when the "Stop" button is pressed."""

    @abstractmethod
    def set_on_open_minutes(self, handler: Callable[[], None]) -> None:
        """Register the handler for when the "Open minutes" button is pressed."""

    @abstractmethod
    def set_on_open_folder(self, handler: Callable[[], None]) -> None:
        """Register the handler for when the "Open output folder" button is pressed."""

    # --- Reading input state -------------------------------------------------
    @abstractmethod
    def get_format_mode(self) -> str:
        """Return the selected minutes format ("builtin" / "file" / "auto")."""

    @abstractmethod
    def get_reuse(self) -> bool:
        """Return the state of the "Reuse existing data" checkbox."""

    # --- Updating the screen ---------------------------------------------------
    @abstractmethod
    def set_format_mode(self, mode: str) -> None:
        """Switch the selected minutes format (used e.g. after picking a template file)."""

    @abstractmethod
    def set_config_summary(self, text: str) -> None:
        """Set the body text of the "Settings (change in config.toml)" section."""

    @abstractmethod
    def set_video_name(self, name: str) -> None:
        """Display the name of the selected video file."""

    @abstractmethod
    def set_template_name(self, name: str) -> None:
        """Display the template file name currently selected under "Choose a file"."""

    @abstractmethod
    def set_start_enabled(self, enabled: bool) -> None:
        """Enable/disable the "Create minutes" button."""

    @abstractmethod
    def set_stop_enabled(self, enabled: bool) -> None:
        """Enable/disable the "Stop" button."""

    @abstractmethod
    def set_open_minutes_enabled(self, enabled: bool) -> None:
        """Enable/disable the "Open minutes" button."""

    @abstractmethod
    def set_open_folder_enabled(self, enabled: bool) -> None:
        """Enable/disable the "Open output folder" button."""

    @abstractmethod
    def set_progress(self, value: int) -> None:
        """Set the progress bar's value (0-1000)."""

    @abstractmethod
    def set_stage_text(self, text: str) -> None:
        """Set the progress label's text (stage name + counter)."""

    @abstractmethod
    def append_log(self, text: str) -> None:
        """Append one line to the log area (adds a trailing newline and scrolls)."""

    @abstractmethod
    def show_error(self, title: str, message: str) -> None:
        """Show an error dialog."""

    # --- File-selection dialogs / OS integration ------------------------------
    @abstractmethod
    def ask_video_path(self) -> str | None:
        """Let the user choose a video file. None if cancelled."""

    @abstractmethod
    def ask_template_path(self) -> str | None:
        """Let the user choose a minutes template file. None if cancelled."""

    @abstractmethod
    def open_in_file_manager(self, path: Path) -> None:
        """Open the given path in the OS's file manager / default app."""

    # --- Event loop -----------------------------------------------
    @abstractmethod
    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """Call callback once, delay_ms milliseconds from now (a thin wrapper around tkinter's after()).

        For repeated execution, have callback itself call schedule() again.
        Used to poll for a worker thread's result on the UI thread.
        """

    @abstractmethod
    def run(self) -> None:
        """Start the event loop (does not return)."""
