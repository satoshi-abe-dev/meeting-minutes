"""i18n カタログの整合性と t() の挙動のテスト。"""

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
    """全キーが SUPPORTED_LANGUAGES ぶんの訳をそろえている（ja だけ足して en 忘れ防止）。"""
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
    [("ja", "ja"), ("en", "en"), (None, "ja"), ("", "ja"), ("fr", "ja"), ("EN", "ja")],
)
def test_normalize_language(given, expected):
    assert normalize_language(given) == expected
    assert DEFAULT_LANGUAGE == "ja"


def test_t_returns_language_specific_text():
    assert t("button.stop", "ja") == "中断"
    assert t("button.stop", "en") == "Stop"
    # 進捗ラベルの起動直後の表示（Issue #57 で「待機中」→「ログ」）
    assert t("label.waiting", "ja") == "ログ"
    assert t("label.waiting", "en") == "Log"


def test_t_falls_back_to_default_language_for_unsupported():
    assert t("button.stop", "fr") == t("button.stop", "ja")


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
    """ja 側は i18n 化前のリテラルと一致していること（挙動保証の要）。"""
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
