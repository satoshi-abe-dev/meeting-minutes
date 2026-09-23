"""Tests for the consistency of the i18n catalog and t()'s behavior."""

from __future__ import annotations

import pytest

from meeting_minutes.i18n import (
    _STRINGS,
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    normalize_language,
    t,
)


def test_every_key_has_all_languages():
    """Every key has a translation for each of SUPPORTED_LANGUAGES (guards
    against adding ja and forgetting en)."""
    missing: list[str] = []
    for key, entry in _STRINGS.items():
        for lang in SUPPORTED_LANGUAGES:
            if not entry.get(lang):
                missing.append(f"{key}[{lang}]")
    assert missing == []


def test_no_empty_or_extra_language_keys():
    for key, entry in _STRINGS.items():
        assert set(entry) == set(SUPPORTED_LANGUAGES), key


@pytest.mark.parametrize(
    "given, expected",
    [("ja", "ja"), ("en", "en"), (None, "en"), ("", "en"), ("fr", "en"), ("EN", "en")],
)
def test_normalize_language(given, expected):
    assert normalize_language(given) == expected
    assert DEFAULT_LANGUAGE == "en"


def test_t_returns_language_specific_text():
    assert t("button.stop", "ja") == "中断"
    assert t("button.stop", "en") == "Stop"
    # the progress label's display right after launch (Issue #102 changed it
    # from "ログ" to "準備完了" / "Ready")
    assert t("label.waiting", "ja") == "準備完了"
    assert t("label.waiting", "en") == "Ready"


def test_t_falls_back_to_default_language_for_unsupported():
    assert t("button.stop", "fr") == t("button.stop", "en")


def test_t_unknown_key_returns_key_or_default():
    assert t("no.such.key", "ja") == "no.such.key"
    assert t("no.such.key", "ja", default="代替文言") == "代替文言"


def test_t_formats_kwargs():
    assert t("log.minutes_path", "ja", path="/x/minutes.md") == "議事録: /x/minutes.md"
    assert t("log.minutes_path", "en", path="/x/minutes.md") == "Minutes: /x/minutes.md"
    assert (
        t("stage_text.counter", "ja", current=2, total=5) == "（2/5）"
    )
    assert t("stage_text.counter", "en", current=2, total=5) == "(2/5)"


def test_ja_side_matches_pre_i18n_literals():
    """The ja side matches the pre-i18n literals (the core guarantee of unchanged behavior)."""
    assert t("window.title", "ja") == "議事録生成AI（ローカル処理）"
    assert t("stage.transcribe", "ja") == "文字起こし"
    assert t("stage_text.error", "ja") == "エラー"
    assert t("log.start", "ja", name="a.mp4", suffix="") == "開始: a.mp4"
    assert (
        t("log.start", "ja", name="a.mp4", suffix=t("log.start_suffix_fresh", "ja"))
        == "開始: a.mp4（最初からやり直す）"
    )
    assert (
        t("log.counts", "ja", segments=444, frames=12)
        == "文字起こし 444 区間 / フレーム 12 枚"
    )
