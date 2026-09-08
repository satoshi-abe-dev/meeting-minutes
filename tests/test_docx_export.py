"""docx_export の変換テスト。生成した .docx を python-docx で開き直して検証する。"""

from __future__ import annotations

import docx  # core 依存（requirements.txt）。無ければテストは失敗させる。
import pytest

from meeting_minutes.model.docx_export import markdown_to_docx, save_minutes_docx

_SAMPLE = """\
# 議事録: 定例ミーティング

- 日時: 2026-09-09
- 出席者: （記載なし）

## 議論の要点

- 新機能の**リリース時期**を確認した
  - QA は `pytest` の緑を条件にする
- 予算は据え置き

## 宿題・アクションアイテム

| 内容 | 担当 | 期限 |
| --- | --- | --- |
| 仕様書の更新 | 田中 | 9/15 |
| レビュー依頼 | 佐藤 | 9/12 |

## 決定事項

（該当なし）

```
$ make deploy
done
```
"""


def _open(path):
    return docx.Document(str(path))


def test_headings_map_to_word_heading_styles(tmp_path):
    path = save_minutes_docx(_SAMPLE, tmp_path)
    doc = _open(path)
    styles = [p.style.name for p in doc.paragraphs]
    assert "Heading 1" in styles
    assert styles.count("Heading 2") >= 3  # 議論の要点 / 宿題 / 決定事項
    # 見出しテキストが入っている
    h1 = next(p for p in doc.paragraphs if p.style.name == "Heading 1")
    assert "定例ミーティング" in h1.text


def test_bullets_and_nesting(tmp_path):
    doc = markdown_to_docx(_SAMPLE)
    bullet_styles = [
        p.style.name for p in doc.paragraphs if p.style.name.startswith("List Bullet")
    ]
    assert "List Bullet" in bullet_styles
    assert "List Bullet 2" in bullet_styles  # 「QA は …」のネスト
    nested = next(p for p in doc.paragraphs if p.style.name == "List Bullet 2")
    assert "pytest" in nested.text


def test_inline_bold_and_code(tmp_path):
    doc = markdown_to_docx(_SAMPLE)
    para = next(p for p in doc.paragraphs if "リリース時期" in p.text)
    bold_run = next(r for r in para.runs if r.text == "リリース時期")
    assert bold_run.bold is True
    nested = next(p for p in doc.paragraphs if "pytest" in p.text)
    code_run = next(r for r in nested.runs if r.text == "pytest")
    assert code_run.font.name == "Consolas"


def test_action_item_table(tmp_path):
    doc = markdown_to_docx(_SAMPLE)
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert len(table.columns) == 3
    assert len(table.rows) == 3  # ヘッダ + データ 2 行
    header = [c.text for c in table.rows[0].cells]
    assert header == ["内容", "担当", "期限"]
    # ヘッダセルは太字
    hdr_run = table.rows[0].cells[0].paragraphs[0].runs[0]
    assert hdr_run.bold is True
    # データ行
    assert [c.text for c in table.rows[1].cells] == ["仕様書の更新", "田中", "9/15"]


def test_placeholder_lines_become_plain_paragraphs(tmp_path):
    doc = markdown_to_docx(_SAMPLE)
    para = next(p for p in doc.paragraphs if p.text == "（該当なし）")
    assert para.style.name in ("Normal", "Body Text")


def test_code_fence_is_monospace(tmp_path):
    doc = markdown_to_docx(_SAMPLE)
    para = next(p for p in doc.paragraphs if "make deploy" in p.text)
    assert "done" in para.text  # フェンス内は 1 段落にまとめる
    assert para.runs[0].font.name == "Consolas"


def test_separate_number_lists_keep_their_own_start(tmp_path):
    """見出しで区切られた独立した番号リストが2つあっても、2つ目が前の続き番号
    （4, 5, …）にならず、それぞれ `1.` から始まる（Word の共有 numbering 問題）。"""
    md = (
        "## アジェンダ\n\n1. 予算\n2. 人員\n\n"
        "## アクションアイテム\n\n1. 見積り\n2. 稟議\n"
    )
    doc = markdown_to_docx(md)
    numbered = [p.text for p in doc.paragraphs if p.text[:2] in ("1.", "2.")]
    assert numbered == ["1. 予算", "2. 人員", "1. 見積り", "2. 稟議"]


def test_number_list_preserves_nonstandard_start(tmp_path):
    """`3.` 始まりのような開始番号もリテラルで保持される。"""
    doc = markdown_to_docx("3. 三番目\n4. 四番目\n")
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert texts == ["3. 三番目", "4. 四番目"]


def test_unknown_lines_do_not_crash(tmp_path):
    weird = "> 引用っぽい行\n<html>tag</html>\n| 崩れた | 表\nplain text\n####### too deep"
    doc = markdown_to_docx(weird)  # 例外を投げない
    texts = "\n".join(p.text for p in doc.paragraphs)
    assert "引用っぽい行" in texts
    assert "too deep" in texts  # 見出しにならず段落へ


def test_save_minutes_docx_writes_to_out_dir_root(tmp_path):
    out = tmp_path / "会議名"
    path = save_minutes_docx("# タイトル\n\n本文", out)
    assert path == out / "minutes.docx"  # minutes/ 配下ではなくルート
    assert path.is_file()
    doc = _open(path)
    assert doc.paragraphs[0].style.name == "Heading 1"


@pytest.mark.parametrize("md", ["", "   \n\n  \n"])
def test_empty_and_whitespace_markdown(tmp_path, md):
    path = save_minutes_docx(md, tmp_path / "out")
    assert path.is_file()
    assert docx.Document(str(path)) is not None  # 壊れた .docx になっていない
