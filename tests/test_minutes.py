"""minutes モジュールのテスト（LLM は呼ばずにスタブ）。"""

from __future__ import annotations

import json
import threading

import pytest

from meeting_minutes.cancel import PipelineCancelled
from meeting_minutes.config import LLMConfig
from meeting_minutes.minutes import (
    MinutesMeta,
    _fill_minutes_template,
    _MINUTES_STRUCTURE,
    _MINUTES_RESPONSE_TOKENS,
    _save_partials,
    _split_segments,
    generate_minutes,
    load_minutes_structure,
)
from meeting_minutes.transcribe import Segment
from meeting_minutes.vision import FrameNote


class FakeClient:
    """LLMClient.chat 互換のスタブ。呼び出しを記録する。"""

    def __init__(self, reply: str = "# 議事録\n\n本文"):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return self.reply


def _segments(n: int, text: str = "発言") -> list[Segment]:
    return [Segment(start=i * 3.0, end=i * 3.0 + 3.0, text=f"{text}{i}") for i in range(n)]


@pytest.fixture
def chunking_config() -> LLMConfig:
    """分割要約（長い会議）の経路を必ず通すための、しきい値を下げた LLMConfig。

    通常運用ではコンテキストに収まる限り一発生成を優先するため既定のしきい値は大きい。
    テストでは chunk_trigger_chars / chunk_size_chars を小さくして分割経路を検証する。
    """
    return LLMConfig(chunk_trigger_chars=2000, chunk_size_chars=1000)


def test_approx_tokens_ratio_matches_measured_qwen_japanese():
    """_approx_tokens は実測（Qwen2.5 tokenizer で日本語 ~0.5〜0.8 tok/字）に基づき
    安全側 0.8 で見積もる。"""
    from meeting_minutes.minutes import _approx_tokens

    assert _approx_tokens("あ" * 100) == 81
    assert _approx_tokens("") == 1


def test_split_segments_respects_size():
    segs = _segments(50, text="あ" * 100)  # 1 セグメント約 100 文字
    chunks = _split_segments(segs, size_chars=1000)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 50
    # 順序が保たれている
    flat = [s for c in chunks for s in c]
    assert flat == segs


def test_generate_minutes_short_path_single_call():
    client = FakeClient(reply="# 議事録: テスト\n\n## 決定事項\n- なし\n")
    notes = [FrameNote(timestamp=12.0, path="frames/frame_0001.jpg", description="表題スライド")]
    meta = MinutesMeta(title="テスト会議", duration_hint="約 5 分")

    md = generate_minutes(
        _segments(5),
        notes,
        client,
        LLMConfig(),
        meta,
    )

    assert md.startswith("# 議事録: テスト")
    assert md.endswith("\n")
    assert len(client.calls) == 1
    # テンプレートに文字起こしとフレーム説明が両方入っている
    user = client.calls[0]["user"]
    assert "テスト会議" in user
    assert "発言0" in user
    assert "表題スライド" in user


def test_generate_minutes_single_pass_under_char_fallback_threshold():
    """context_tokens 不明時は文字数しきい値で判断。既定 20000 字未満は一発生成。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(180, text="議題について長い発言をする" * 3)  # 1万字弱 < 20000
    generate_minutes(segs, [], client, LLMConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) == 1
    assert client.calls[0]["user"].startswith("以下のテンプレートに沿って")


def test_generate_minutes_char_fallback_chunks_over_threshold():
    """context_tokens 不明で 20000 字を超えると分割する。"""
    client = FakeClient(reply="要約")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # 2.5万字超 > 20000
    generate_minutes(segs, [], client, LLMConfig(), MinutesMeta(title="会議"))
    assert len(client.calls) >= 2


def test_generate_minutes_context_tokens_allow_single_pass():
    """実コンテキスト長が分かっていて余裕があれば、文字数が多めでも一発生成。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # char 閾値なら分割される長さ
    generate_minutes(
        segs, [], client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=32768,
    )
    assert len(client.calls) == 1


def test_generate_minutes_context_tokens_force_chunk_when_prompt_would_overflow():
    """Issue #18: 文字数は少なくても、frames 込みで実コンテキスト長を超えると
    推定される場合は分割生成にフォールバックする。"""
    client = FakeClient(reply="要約")
    segs = _segments(80, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの文字。" * 60)
        for i in range(40)
    ]
    generate_minutes(
        segs, notes, client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=4096,
    )
    assert len(client.calls) >= 2


def test_generate_minutes_frames_text_is_truncated_to_budget():
    """巨大な frames_text は上限トークンで切り詰められ、プロンプトを食い尽くさない。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(20, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの詳細な文字起こし。" * 80)
        for i in range(50)
    ]
    generate_minutes(
        segs, notes, client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=32768,
    )
    assert len(client.calls) == 1
    user = client.calls[0]["user"]
    assert "コンテキスト長の都合でここまで" in user  # 切り詰めマーカー


def test_generate_minutes_chunk_trigger_chars_from_config_controls_path():
    """[llm] chunk_trigger_chars を下げると、短い文字起こしでも分割経路になる。"""
    segs = _segments(20, text="短い発言")  # 数百文字。既定 40000 なら一発生成
    single = FakeClient(reply="要約")
    generate_minutes(segs, [], single, LLMConfig(), MinutesMeta(title="会議"))
    assert len(single.calls) == 1

    split = FakeClient(reply="要約")
    generate_minutes(
        segs, [], split,
        LLMConfig(chunk_trigger_chars=50, chunk_size_chars=30),
        MinutesMeta(title="会議"),
    )
    assert len(split.calls) >= 2  # チャンク要約(複数) + 統合


def test_generate_minutes_long_path_maps_then_reduces(chunking_config):
    # チャンク要約が走るよう十分長い文字起こしを作る
    client = FakeClient(reply="部分要約 or 最終議事録")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")

    progress: list[tuple] = []
    md = generate_minutes(
        long_segs,
        [],
        client,
        chunking_config,
        meta,
        on_progress=lambda c, t, m: progress.append((c, t, m)),
    )

    assert md.endswith("\n")
    # チャンク要約(複数) + 最終統合(1) で 2 回以上呼ばれる
    assert len(client.calls) >= 2
    assert progress  # 進捗が通知されている


def test_generate_minutes_cancel_stops_chunk_loop(chunking_config):
    client = FakeClient(reply="部分要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()  # 1 チャンク目の後に中断ボタンが押された想定

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            long_segs,
            [],
            client,
            chunking_config,
            meta,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    # 統合（最終 chat 呼び出し）までは進まない
    assert client.calls[-1]["user"].startswith("次の会議の文字起こしの一部です")


def test_generate_minutes_cancel_before_start_raises_immediately():
    client = FakeClient()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            _segments(3), [], client, LLMConfig(), MinutesMeta(title="会議"),
            cancel_event=cancel_event,
        )
    assert client.calls == []


# --- 部分要約の永続化と再開 -----------------------------------------

def _save_partials_matching(tmp_path, entries, chunking_config, long_segs):
    """long_segs / chunking_config と整合するシグネチャで部分要約ファイルを書く。"""
    _save_partials(
        entries,
        tmp_path,
        size_chars=chunking_config.chunk_size_chars,
        num_segments=len(long_segs),
        num_chunks=len(_split_segments(long_segs, chunking_config.chunk_size_chars)),
    )


def test_generate_minutes_persists_partials_on_cancel(tmp_path, chunking_config):
    client = FakeClient(reply="部分要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if cur == 1:
            cancel_event.set()

    with pytest.raises(PipelineCancelled):
        generate_minutes(
            long_segs, [], client, chunking_config, meta,
            on_progress=on_progress, cancel_event=cancel_event, out_dir=tmp_path,
        )

    saved = json.loads((tmp_path / "minutes_partials.json").read_text(encoding="utf-8"))
    assert saved["format"] == 2
    assert saved["chunk_size_chars"] == chunking_config.chunk_size_chars
    assert saved["num_segments"] == len(long_segs)
    assert len(saved["partials"]) == 1
    assert saved["partials"][0].startswith("### 部分 1")


def test_generate_minutes_resumes_from_saved_partials(tmp_path, chunking_config):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n既存の要約"], chunking_config, long_segs)
    client = FakeClient(reply="最終議事録")
    meta = MinutesMeta(title="長い会議")

    md = generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=True
    )

    assert md.strip() == "最終議事録"
    # 1つ目のチャンクは要約し直していない
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert all("既存の要約" not in c["user"] for c in chunk_calls)
    # 統合（最終）呼び出しには再利用した部分要約が含まれる
    assert "既存の要約" in client.calls[-1]["user"]


def test_generate_minutes_fresh_ignores_saved_partials(tmp_path, chunking_config):
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n既存の要約"], chunking_config, long_segs)
    client = FakeClient(reply="要約")
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=False
    )

    # reuse=False なので既存の部分要約は使われず、最初から要約し直す
    assert "既存の要約" not in client.calls[-1]["user"]


def test_generate_minutes_ignores_empty_bodied_partials(tmp_path, chunking_config):
    """見出しだけで本文が空の部分要約（LLMが空応答を返した形跡）は無効として扱う。"""
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials_matching(tmp_path, ["### 部分 1\n"], chunking_config, long_segs)  # 本文なし
    client = FakeClient(reply="要約")
    meta = MinutesMeta(title="長い会議")

    generate_minutes(
        long_segs, [], client, chunking_config, meta, out_dir=tmp_path, reuse=True
    )

    # 空だった部分1はやり直されている（チャンク呼び出しが発生している）
    chunk_calls = [
        c for c in client.calls if c["user"].startswith("次の会議の文字起こしの一部です")
    ]
    assert len(chunk_calls) >= 1


def test_generate_minutes_discards_partials_when_chunk_size_changed(tmp_path):
    """chunk_size_chars を変えて再開すると、旧チャンク境界の部分要約は使わない。

    doc/models.md の「Context Length を上げられないなら chunk_size_chars を下げる」
    手順（中断→config変更→再開）で、内容がサイレントに重複・欠落しないことを保証する。
    """
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    _save_partials(
        ["### 部分 1\n古い境界の要約"], tmp_path,
        size_chars=1000, num_segments=len(long_segs), num_chunks=5,
    )
    client = FakeClient(reply="新しい要約")
    cfg = LLMConfig(chunk_trigger_chars=500, chunk_size_chars=400)  # size を変更

    generate_minutes(
        long_segs, [], client, cfg, MinutesMeta(title="会議"),
        out_dir=tmp_path, reuse=True,
    )

    # 旧要約は捨てられ、最終統合にも現れない
    assert "古い境界の要約" not in client.calls[-1]["user"]


def test_generate_minutes_discards_legacy_list_format_partials(tmp_path, chunking_config):
    """メタ情報の無い旧形式（JSON 配列）の部分要約は境界を検証できないので使わない。"""
    (tmp_path / "minutes_partials.json").write_text(
        json.dumps(["### 部分 1\n旧形式の要約"]), encoding="utf-8"
    )
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    client = FakeClient(reply="要約")

    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="会議"),
        out_dir=tmp_path, reuse=True,
    )

    assert "旧形式の要約" not in client.calls[-1]["user"]


def test_generate_minutes_chunk_max_tokens_matches_budget_cap(tmp_path):
    """チャンク要約の実 max_tokens は予算計算と同じ値（_MINUTES_RESPONSE_TOKENS 頭打ち）。

    ここがズレると safe_chunk_tokens の計算と実リクエストが食い違い、チャンクが
    コンテキストを超え得る（Codex 指摘）。
    """
    from meeting_minutes.minutes import _MINUTES_RESPONSE_TOKENS

    client = FakeClient(reply="要約")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    meta = MinutesMeta(title="長い会議")
    llm_config = LLMConfig(max_tokens=12345, chunk_trigger_chars=2000, chunk_size_chars=1000)

    generate_minutes(long_segs, [], client, llm_config, meta, out_dir=tmp_path)

    calls = client.calls
    chunk_calls = [c for c in calls if c["user"].startswith("次の会議の文字起こしの一部です")]
    assert chunk_calls
    expected = min(12345, _MINUTES_RESPONSE_TOKENS)
    assert all(c["kwargs"]["max_tokens"] == expected for c in chunk_calls)
    # 最終統合も同じ上限
    assert calls[-1]["kwargs"]["max_tokens"] == expected


def test_generate_minutes_manual_context_tokens_beats_autodetect():
    """config の [llm] context_tokens が 0 でなければ、自動検出値より優先される。"""
    client = FakeClient(reply="要約")
    segs = _segments(320, text="議題について長い発言をする" * 5)  # 約2.5万字
    # 手動 4096（小さい）を設定。自動検出で 200000 が来ても手動が勝ち、分割になる。
    cfg = LLMConfig(context_tokens=4096)
    generate_minutes(
        segs, [], client, cfg, MinutesMeta(title="会議"), context_tokens=200000
    )
    assert len(client.calls) >= 2  # 手動 4096 が効いて分割された


def test_generate_minutes_small_context_does_not_force_oversized_frames_budget():
    """ctx が小さくても frames 予算は下限 6000 を無理に確保せず ctx で頭打ちする。"""
    client = FakeClient(reply="# 議事録\n\n本文\n")
    segs = _segments(20, text="短い発言")
    notes = [
        FrameNote(timestamp=float(i), path=f"frames/f{i}.jpg",
                  description="スライドの文字。" * 40)
        for i in range(30)
    ]
    # ctx=4096 では一発生成は無理なので分割になるが、frames の切り詰めで
    # frames_text 自体が 4096 を単独で超えることはない。
    generate_minutes(
        segs, notes, client, LLMConfig(), MinutesMeta(title="会議"),
        context_tokens=4096,
    )
    from meeting_minutes.minutes import _approx_tokens
    for c in client.calls:
        # どの実リクエストのプロンプトも、frames だけで ctx を食い尽くしていない
        assert _approx_tokens(c["user"]) < 4096 * 3  # ざっくり: 暴走していない


# --- カスタム議事録テンプレート（Issue #21）--------------------------

def test_load_minutes_structure_builtin_when_empty():
    assert load_minutes_structure("") is _MINUTES_STRUCTURE
    assert load_minutes_structure(None) is _MINUTES_STRUCTURE


def test_load_minutes_structure_reads_custom_file(tmp_path):
    p = tmp_path / "tpl.txt"
    p.write_text("# 顧客様式\n\n## 決定\n## 宿題\n", encoding="utf-8")
    assert load_minutes_structure(str(p)) == "# 顧客様式\n\n## 決定\n## 宿題"


def test_load_minutes_structure_missing_file_falls_back_with_warning(tmp_path):
    warnings: list[str] = []
    got = load_minutes_structure(str(tmp_path / "nope.txt"), on_warning=warnings.append)
    assert got is _MINUTES_STRUCTURE
    assert warnings and "内蔵テンプレート" in warnings[0]


def test_load_minutes_structure_empty_file_falls_back_with_warning(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n", encoding="utf-8")
    warnings: list[str] = []
    assert load_minutes_structure(str(p), on_warning=warnings.append) is _MINUTES_STRUCTURE
    assert warnings and "空です" in warnings[0]


def test_fill_minutes_template_appends_input_section_when_missing():
    out = _fill_minutes_template(
        "# 様式\n## 決定事項", MinutesMeta(title="会議X"), "文字起こし本文", "フレーム本文"
    )
    assert "# 様式" in out
    assert "会議X" not in out or "{title}" not in out  # {title} は様式に無いだけ
    assert "文字起こし本文" in out and "フレーム本文" in out
    assert "## 入力: 文字起こし" in out  # 自動で足された


def test_fill_minutes_template_respects_own_placeholders_and_stray_braces():
    tpl = "# {title} 議事録\nJSON例: {\"a\": 1}\n## 本文\n{transcript}\n資料:{frames}"
    out = _fill_minutes_template(tpl, MinutesMeta(title="定例"), "T!", "F!")
    assert out.count("## 入力: 文字起こし") == 0  # {transcript} を持つので入力節は足さない
    assert "# 定例 議事録" in out
    assert 'JSON例: {"a": 1}' in out  # 素の { } は壊れない
    assert "T!" in out and "F!" in out


def test_generate_minutes_uses_custom_template_single_pass(tmp_path):
    tpl = tmp_path / "cust.txt"
    tpl.write_text("# お客様フォーマット\n## 合意事項\n## 次アクション\n", encoding="utf-8")
    client = FakeClient(reply="# 議事録\n本文\n")
    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        template_path=str(tpl),
    )
    assert len(client.calls) == 1
    assert "お客様フォーマット" in client.calls[0]["user"]
    assert "## 合意事項" in client.calls[0]["user"]
    # 内蔵テンプレの見出しは出ない
    assert "## 宿題・アクションアイテム" not in client.calls[0]["user"]


def test_generate_minutes_uses_custom_template_in_merge_step(chunking_config):
    tpl_text = "# 客先様式\n## 決めたこと\n{transcript}\n{frames}\n"
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, tpl_text.encode("utf-8"))
    os.close(fd)
    try:
        client = FakeClient(reply="部分/最終")
        long_segs = _segments(300, text="議題について長い発言をする" * 5)
        generate_minutes(
            long_segs, [], client, chunking_config, MinutesMeta(title="長い会議"),
            template_path=path,
        )
        # 統合（最終 chat）に客先様式が使われている
        assert "客先様式" in client.calls[-1]["user"]
    finally:
        os.unlink(path)


def test_generate_minutes_custom_template_missing_still_produces_minutes(tmp_path):
    """テンプレートが見つからなくてもエラーで止めず内蔵で生成し、警告を出す。"""
    msgs: list[str] = []
    client = FakeClient(reply="# 議事録\n本文\n")
    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        template_path=str(tmp_path / "missing.txt"),
        on_progress=lambda c, t, m: msgs.append(m),
    )
    assert len(client.calls) == 1
    assert "## 宿題・アクションアイテム" in client.calls[0]["user"]  # 内蔵テンプレ
    assert any("テンプレート" in m and "内蔵" in m for m in msgs)


def test_generate_minutes_system_prompt_unchanged_by_custom_template(tmp_path):
    """テンプレートを変えても system プロンプト（幻覚防止ルール）は同じ。"""
    tpl = tmp_path / "t.txt"
    tpl.write_text("# 様式\n## 本文\n", encoding="utf-8")
    a = FakeClient(reply="x")
    b = FakeClient(reply="x")
    generate_minutes(_segments(3), [], a, LLMConfig(), MinutesMeta(title="会議"))
    generate_minutes(_segments(3), [], b, LLMConfig(), MinutesMeta(title="会議"),
                     template_path=str(tpl))
    assert a.calls[0]["system"] == b.calls[0]["system"]


def test_example_template_file_matches_builtin_structure():
    """templates/minutes_template_example.txt は内蔵テンプレートと同一（雛形なので）。"""
    from meeting_minutes.config import REPO_ROOT

    p = REPO_ROOT / "templates" / "minutes_template_example.txt"
    assert load_minutes_structure(str(p)) == _MINUTES_STRUCTURE.strip()


def test_fill_minutes_template_appends_only_missing_frames_section():
    """{transcript} は書いたが {frames} を忘れた場合でも、フレーム情報は消えない（Codex 指摘1）。"""
    tpl = "# 様式\n## 本文\n{transcript}\n"  # {frames} なし
    out = _fill_minutes_template(tpl, MinutesMeta(title="会議"), "文字起こし本文", "フレーム解析本文")
    assert "文字起こし本文" in out
    assert "フレーム解析本文" in out  # 個別に補われる
    assert "## 入力: 画面キャプチャの説明" in out
    assert out.count("## 入力: 文字起こし") == 0  # transcript は構造側にあるので二重にしない


def test_fill_minutes_template_no_contamination_from_replaced_values():
    """置換後の値の中に別プレースホルダー文字列があっても巻き込まれない（Codex 指摘2）。"""
    tpl = "# {title}\n## 本文\n{transcript}\n## 資料\n{frames}\n"
    tricky_transcript = "田中: このスライドの {frames} という表記について質問です"
    out = _fill_minutes_template(
        tpl, MinutesMeta(title="定例"), tricky_transcript, "実際のフレーム解析結果"
    )
    # 文字起こし中の "{frames}" はそのまま残る（フレーム解析結果で上書きされない）
    assert "{frames} という表記について" in out
    # 本来の {frames} プレースホルダーだけがフレーム解析結果になる
    assert "実際のフレーム解析結果" in out
    assert out.count("実際のフレーム解析結果") == 1


def test_fill_minutes_template_leaves_unknown_braces_untouched():
    tpl = "# {title}\n設定例: {timeout: 600}\n{transcript}\n{frames}"
    out = _fill_minutes_template(tpl, MinutesMeta(title="X"), "T", "F")
    assert "設定例: {timeout: 600}" in out  # 既知プレースホルダー名でない { } は不変


# --- 「おまかせ」モード: 型を会議内容から自動生成（Issue #34）--------------

class RoutingFakeClient:
    """呼び出し内容で返答を出し分けるスタブ（型生成 / チャンク要約 / 議事録本文）。"""

    def __init__(
        self,
        *,
        structure: str | None = None,
        chunk: str = "- 箇条書きの要点",
        minutes: str = "# 議事録\n本文\n",
        raise_on_structure: bool = False,
    ):
        self.structure = structure
        self.chunk = chunk
        self.minutes = minutes
        self.raise_on_structure = raise_on_structure
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        if "議事録の「型」" in user:
            if self.raise_on_structure:
                raise RuntimeError("LLM 500")
            return self.structure or ""
        if user.startswith("次の会議の文字起こしの一部です"):
            return self.chunk
        return self.minutes

    def struct_calls(self) -> list[dict]:
        return [c for c in self.calls if "議事録の「型」" in c["user"]]

    def chunk_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if c["user"].startswith("次の会議の文字起こしの一部です")
        ]


_GEN_STRUCTURE = (
    "# 議事録: {title}\n\n"
    "- 日時: {datetime_hint}\n"
    "- 記録時間: {duration_hint}\n\n"
    "## スケジュール\n"
    "（旅程を時系列で。文字起こし・資料に明示的に出てくることだけ書く。推測で補わない）\n\n"
    "## 決定事項\n（「〜する」と明言されたものだけ。無ければ「（該当なし）」）\n\n"
    "## 次アクション\n（誰が・いつまでに・何を、と明言された場合のみ）\n"
)


def test_auto_structure_single_pass_uses_full_transcript_and_saves(tmp_path):
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    segs = _segments(5, text="旅行の説明")

    generate_minutes(
        segs, [], client, LLMConfig(), MinutesMeta(title="旅行説明会"),
        out_dir=tmp_path, auto_structure=True,
    )

    # 型生成 → 議事録本文 の 2 回だけ
    assert len(client.calls) == 2
    struct_call = client.calls[0]
    assert "旅行の説明0" in struct_call["user"]  # 全文をそのまま材料にしている
    # 応答予約は議事録本文と同じ minutes_max_tokens（推論モデル耐性を揃える）
    assert struct_call["kwargs"]["max_tokens"] == client.calls[1]["kwargs"]["max_tokens"]
    assert struct_call["kwargs"]["max_tokens"] == min(
        LLMConfig().max_tokens, _MINUTES_RESPONSE_TOKENS
    )

    # 生成された型が最終議事録プロンプトに使われ、内蔵は使われていない
    minutes_user = client.calls[1]["user"]
    assert "## スケジュール" in minutes_user
    assert "## 宿題・アクションアイテム" not in minutes_user

    # 保存された型はプレースホルダーがリテラルのまま（そのまま templates/ に置ける）
    saved = (tmp_path / "structure_used.txt").read_text(encoding="utf-8")
    assert "{title}" in saved
    assert "{datetime_hint}" in saved
    assert "{duration_hint}" in saved
    assert "旅行説明会" not in saved  # 実際の値では埋めていない
    # 一方、最終プロンプトでは実値に置換されている
    assert "旅行説明会" in minutes_user


def test_auto_structure_large_transcript_reuses_chunk_summaries(tmp_path, chunking_config):
    client = RoutingFakeClient(structure=_GEN_STRUCTURE, chunk="- 部分要点")
    long_segs = _segments(300, text="議題について長い発言をする" * 5)
    n_chunks = len(_split_segments(long_segs, chunking_config.chunk_size_chars))

    generate_minutes(
        long_segs, [], client, chunking_config, MinutesMeta(title="長い会議"),
        out_dir=tmp_path, auto_structure=True,
    )

    assert len(client.struct_calls()) == 1
    material = client.struct_calls()[0]["user"]
    # 材料はチャンク要約であって、生の全文ではない（新たな全文読み込みパスを増やさない）
    assert "### 部分 1" in material
    assert "議題について長い発言をする" not in material
    # チャンク要約は 1 周分だけ（型生成のために二重に回っていない）
    assert len(client.chunk_calls()) == n_chunks
    # 最終統合に生成された型
    assert "## 次アクション" in client.calls[-1]["user"]
    assert (tmp_path / "structure_used.txt").is_file()


def test_auto_structure_fallback_on_missing_placeholder(tmp_path):
    # {duration_hint} が欠落 → 使い回せないので内蔵にフォールバック
    bad = "# 議事録: {title}\n- 日時: {datetime_hint}\n## 議論\n（略）\n"
    msgs: list[str] = []
    client = RoutingFakeClient(structure=bad)

    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]  # 内蔵テンプレート
    assert any("プレースホルダー" in m and "内蔵" in m for m in msgs)
    assert not (tmp_path / "structure_used.txt").exists()  # 失敗時は保存しない


def test_auto_structure_fallback_on_llm_error(tmp_path):
    msgs: list[str] = []
    client = RoutingFakeClient(raise_on_structure=True)

    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]
    assert any("自動生成に失敗" in m for m in msgs)
    assert not (tmp_path / "structure_used.txt").exists()


def test_auto_structure_prompt_instructs_literal_placeholders():
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        auto_structure=True,
    )
    struct_user = client.calls[0]["user"]
    # 材料を差し込んでもプロンプトの指示・プレースホルダー例は壊れず残っている
    assert "{title}" in struct_user
    assert "{transcript}" in struct_user  # 「書かない」指示のリテラルが .replace で壊れない
    assert "実際の値で" in struct_user


def test_auto_structure_takes_precedence_over_template_path(tmp_path):
    tpl = tmp_path / "cust.txt"
    tpl.write_text("# 客先様式\n## 合意事項\n", encoding="utf-8")
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)

    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, template_path=str(tpl),
    )

    minutes_user = client.calls[-1]["user"]
    assert "## スケジュール" in minutes_user  # 自動生成の型
    assert "客先様式" not in minutes_user     # template_path は使われない


def test_auto_structure_without_out_dir_still_generates():
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        auto_structure=True,  # out_dir なし → 保存はしないが生成はする
    )
    assert "## スケジュール" in client.calls[-1]["user"]


def test_auto_structure_failure_falls_back_to_builtin_not_file_template(tmp_path):
    """Codex 指摘2: auto 失敗時は template_path のファイルではなく必ず内蔵へ。"""
    tpl = tmp_path / "cust.txt"
    tpl.write_text("# 客先様式だけ\n## 合意事項\n", encoding="utf-8")
    client = RoutingFakeClient(structure="型らしきもの（プレースホルダー無し）")  # 必須欠落→失敗

    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, template_path=str(tpl),
    )

    minutes_user = client.calls[-1]["user"]
    assert "## 宿題・アクションアイテム" in minutes_user  # 内蔵テンプレート
    assert "客先様式だけ" not in minutes_user  # ファイルテンプレートにはフォールバックしない
    assert not (tmp_path / "structure_used.txt").exists()


def test_auto_structure_recomputes_budget_after_generation(tmp_path):
    """Codex 指摘1: 生成構造が（棄却されない範囲で）大きい場合、予算を計算し直して分割へ。

    system＋構造＋応答予約＋マージンは ctx に収まる（棄却されない）が、そこに
    文字起こしを足すと収まらない大きさの構造。生成後に one_pass を評価し直して
    チャンク要約経路に落とす。
    """
    biggish_structure = (
        "# 議事録: {title}\n- {datetime_hint} / {duration_hint}\n"
        + "## 追加の見出し\n（この見出しに書く内容の説明）\n" * 170
    )
    client = RoutingFakeClient(
        structure=biggish_structure, chunk="- 部分要点", minutes="# 議事録\n本文\n"
    )
    segs = _segments(90, text="そこそこの長さの発言をする" * 4)

    generate_minutes(
        segs, [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, context_tokens=13000,
    )

    # 型生成は1回、棄却はされず（保存あり）、一発生成を諦めて分割へ切り替わる
    assert len(client.struct_calls()) == 1
    assert (tmp_path / "structure_used.txt").is_file()
    assert len(client.chunk_calls()) >= 1


def test_auto_structure_rejects_structure_too_large_for_merge(tmp_path):
    """Codex 指摘: 生成構造自体が大きすぎて統合しても収まらないなら内蔵へ。

    分割生成に切り替えても、統合の最終リクエストは system＋巨大な構造＋partials＋
    frames なので partials がどれだけ小さくても収まらない。プレースホルダー欠落と
    同様に不正な構造として内蔵にフォールバックする。
    """
    ctx = 10000
    huge = (
        "# 議事録: {title}\n- {datetime_hint} / {duration_hint}\n"
        + "## 見出し\n（この見出しに書くことの説明を長めに書く）\n" * 400
    )
    msgs: list[str] = []
    client = RoutingFakeClient(structure=huge, chunk="- 要点")
    long_segs = _segments(200, text="議題の発言" * 3)

    generate_minutes(
        long_segs, [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True, context_tokens=ctx,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    assert "## 宿題・アクションアイテム" in client.calls[-1]["user"]  # 内蔵テンプレート
    assert not (tmp_path / "structure_used.txt").exists()  # 棄却したので保存しない
    assert any("大きすぎ" in m for m in msgs)


def test_auto_structure_no_size_reject_when_ctx_unknown(tmp_path):
    """ctx 不明時は構造サイズ判定をしない（文字数しきい値の経路に任せる）。"""
    big = (
        "# 議事録: {title}\n- {datetime_hint} / {duration_hint}\n"
        + "## 見出し\n（説明）\n" * 400
    )
    client = RoutingFakeClient(structure=big)
    generate_minutes(
        _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, auto_structure=True,  # context_tokens 指定なし
    )
    # サイズ理由での棄却はされず、生成された型が使われて保存される
    assert (tmp_path / "structure_used.txt").is_file()
    assert "## 見出し" in client.calls[-1]["user"]


def test_auto_structure_truncates_oversized_chunk_summary_material(tmp_path):
    """Codex 指摘: チャンク要約を連結した material も構造生成予算でチェック・切り詰める。"""
    from meeting_minutes.minutes import _approx_tokens

    ctx = 16000
    big_summary = "・とても長い部分要約の行。" * 400  # 連結すると構造生成予算を超える
    client = RoutingFakeClient(structure=_GEN_STRUCTURE, chunk=big_summary)
    long_segs = _segments(400, text="議題について長い発言をする" * 5)

    generate_minutes(
        long_segs, [], client, LLMConfig(), MinutesMeta(title="長い会議"),
        out_dir=tmp_path, auto_structure=True, context_tokens=ctx,
    )

    assert len(client.struct_calls()) == 1
    struct = client.struct_calls()[0]
    assert "コンテキスト長の都合で省略" in struct["user"]  # 末尾が切り詰められた
    # 構造生成リクエスト（system + user + 実際の応答予約）が ctx に収まる
    total = (
        _approx_tokens(struct["system"])
        + _approx_tokens(struct["user"])
        + struct["kwargs"]["max_tokens"]
    )
    assert total <= ctx


def test_auto_structure_cancel_after_generation_stops_before_minutes(tmp_path):
    """Codex 指摘: 構造生成の直後にも中断を拾い、重い議事録生成へ進まない。"""
    cancel_event = threading.Event()

    def on_progress(cur, tot, msg):
        if "型を自動生成しました" in msg:
            cancel_event.set()  # 構造生成が終わった直後に「中断」

    client = RoutingFakeClient(structure=_GEN_STRUCTURE)
    with pytest.raises(PipelineCancelled):
        generate_minutes(
            _segments(5), [], client, LLMConfig(), MinutesMeta(title="会議"),
            out_dir=tmp_path, auto_structure=True,
            on_progress=on_progress, cancel_event=cancel_event,
        )

    assert len(client.struct_calls()) == 1
    assert client.calls == client.struct_calls()  # 議事録本文の呼び出しは無い


def test_auto_structure_response_reserve_follows_llm_max_tokens():
    """Codex 指摘: 構造生成の応答予約は固定値でなく minutes_max_tokens に追随する。

    推論（thinking）モデルで max_tokens を小さくしている場合でも、メインの議事録生成と
    同じ基準（min(llm_config.max_tokens, _MINUTES_RESPONSE_TOKENS)）で予約する。
    """
    cfg = LLMConfig(max_tokens=900)  # 推論モデル想定で小さめ
    client = RoutingFakeClient(structure=_GEN_STRUCTURE)

    generate_minutes(
        _segments(5), [], client, cfg, MinutesMeta(title="会議"),
        auto_structure=True,
    )

    struct_mt = client.struct_calls()[0]["kwargs"]["max_tokens"]
    minutes_mt = client.calls[-1]["kwargs"]["max_tokens"]
    assert struct_mt == minutes_mt == min(900, _MINUTES_RESPONSE_TOKENS)


def test_generate_minutes_truncates_oversized_merged_transcript(tmp_path):
    """Codex 指摘: 統合ステップの実プロンプト（system+構造+merged_transcript+frames）を
    組み立てる前に実トークン数で予算チェックし、超える分は末尾を切り詰める。"""
    from meeting_minutes.minutes import _approx_tokens, _PROMPT_MARGIN_TOKENS

    ctx = 12000
    # 各チャンク要約を大きく返す → 連結した merged_transcript が統合予算を超える
    client = FakeClient(reply="・" + "とても長い部分要約の一行。" * 500)
    long_segs = _segments(400, text="議題の発言" * 3)
    msgs: list[str] = []

    generate_minutes(
        long_segs, [], client, LLMConfig(), MinutesMeta(title="会議"),
        out_dir=tmp_path, context_tokens=ctx,
        on_progress=lambda c, t, m: msgs.append(m),
    )

    merge_user = client.calls[-1]["user"]
    assert "以降はコンテキスト長の都合で省略" in merge_user  # 末尾を切り詰めた
    reserve = min(LLMConfig().max_tokens, _MINUTES_RESPONSE_TOKENS)
    # 統合リクエスト（user + 応答予約 + マージン）が ctx に収まる
    assert _approx_tokens(merge_user) + reserve + _PROMPT_MARGIN_TOKENS <= ctx
    assert any("統合入力の末尾を一部省略" in m for m in msgs)
