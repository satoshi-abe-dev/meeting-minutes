"""議事録 Markdown を .docx（Word）へ変換する。

汎用 Markdown パーサーは使わない。議事録（内蔵テンプレート・カスタムテンプレート・
「おまかせ」モード）が実際に取る Markdown サブセットだけを、行ベースの小さな
state machine で変換する。想定外の行は素の段落として落とし、変換自体は例外を
投げない（.docx は minutes.md の副次成果物。ここでランを落とさない方針）。

対応する記法:
    - ATX 見出し `#`〜`######`            → Word の Heading 1..6
    - 箇条書き `- ` / `* ` / `+ `（先頭スペースでネスト）→ List Bullet / 2 / 3
    - 番号リスト `1. ` / `1) `            → List Number / 2 / 3
    - GFM パイプ表（`| … |` 行 ＋ `| --- |` 区切り行）→ 表（ヘッダ行のセルを太字）
    - フェンスドコードブロック ``` … ```  → 等幅（Consolas）の段落
    - 空行 → 段落の区切り / その他の行 → 素の段落
    - インライン: `**bold**` と `` `code` `` のみ（ネストは非対応）
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 重い依存。実行時は関数内 import する。
    from docx.document import Document

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_TABLE_SEP_CELL_RE = re.compile(r"^\s*:?-+:?\s*$")
# `**bold**`（1 文字以上）または `` `code` ``（1 文字以上、バッククォートを含まない）。
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")

_MONO_FONT = "Consolas"
_MAX_LIST_DEPTH = 3  # List Bullet / List Bullet 2 / List Bullet 3 まで


# --- インライン ------------------------------------------------------------

def _iter_inline(text: str) -> Iterator[tuple[str, bool, bool]]:
    """text を (断片, 太字か, 等幅コードか) の並びに分解する。"""
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            yield text[pos:m.start()], False, False
        tok = m.group(0)
        if tok.startswith("**"):
            yield tok[2:-2], True, False
        else:  # `code`
            yield tok[1:-1], False, True
        pos = m.end()
    if pos < len(text):
        yield text[pos:], False, False


def _add_runs(paragraph, text: str, *, force_bold: bool = False) -> None:
    """paragraph に text をインライン記法込みで流し込む。"""
    for seg, is_bold, is_code in _iter_inline(text):
        run = paragraph.add_run(seg)
        if is_bold or force_bold:
            run.bold = True
        if is_code:
            run.font.name = _MONO_FONT


# --- 表 ------------------------------------------------------------------

def _looks_like_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [c.replace("\\|", "|").strip() for c in cells]


def _is_table_separator(line: str) -> bool:
    if not _looks_like_row(line):
        return False
    cells = _split_row(line)
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _add_table(doc: Document, header: list[str], rows: list[list[str]]) -> None:
    ncols = max(1, len(header))
    table = doc.add_table(rows=1, cols=ncols)
    table.style = "Table Grid"
    for k in range(ncols):
        cell = table.rows[0].cells[k]
        _add_runs(cell.paragraphs[0], header[k] if k < len(header) else "",
                  force_bold=True)
    for row in rows:
        cells = table.add_row().cells
        for k in range(ncols):
            _add_runs(cells[k].paragraphs[0], row[k] if k < len(row) else "")


# --- コードブロック -----------------------------------------------------

def _add_code_block(doc: Document, code_lines: list[str]) -> None:
    p = doc.add_paragraph(style="No Spacing")
    run = p.add_run("\n".join(code_lines))
    run.font.name = _MONO_FONT


# --- 変換本体 ----------------------------------------------------------

def markdown_to_docx(markdown: str) -> Document:
    """議事録 Markdown を python-docx の Document に変換して返す。

    未知の行は素の段落として落とす。1 行の処理で万一例外が出ても、その行を
    プレーンな段落にしてスキップし、変換全体は止めない。
    """
    import docx

    doc = docx.Document()
    lines = markdown.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # フェンスドコードブロック
        if stripped.startswith("```"):
            j = i + 1
            buf: list[str] = []
            while j < n and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            _add_code_block(doc, buf)
            i = j + 1 if j < n else n
            continue

        if not stripped:
            i += 1
            continue

        try:
            # GFM パイプ表（区切り行が続くときだけ表として扱う）
            if (
                _looks_like_row(line)
                and i + 1 < n
                and _is_table_separator(lines[i + 1])
            ):
                header = _split_row(line)
                body: list[list[str]] = []
                j = i + 2
                while j < n and _looks_like_row(lines[j]):
                    body.append(_split_row(lines[j]))
                    j += 1
                _add_table(doc, header, body)
                i = j
                continue

            m = _HEADING_RE.match(line)
            if m:
                p = doc.add_paragraph(style=f"Heading {len(m.group(1))}")
                _add_runs(p, m.group(2).strip())
                i += 1
                continue

            m = _BULLET_RE.match(line)
            if m:
                depth = min(len(m.group(1)) // 2, _MAX_LIST_DEPTH - 1)
                style = "List Bullet" if depth == 0 else f"List Bullet {depth + 1}"
                _add_runs(doc.add_paragraph(style=style), m.group(2).strip())
                i += 1
                continue

            m = _NUMBER_RE.match(line)
            if m:
                depth = min(len(m.group(1)) // 2, _MAX_LIST_DEPTH - 1)
                style = "List Number" if depth == 0 else f"List Number {depth + 1}"
                _add_runs(doc.add_paragraph(style=style), m.group(2).strip())
                i += 1
                continue

            _add_runs(doc.add_paragraph(), stripped)
        except Exception:
            # 想定外の行でも .docx 生成は止めない。素の段落にして次へ。
            doc.add_paragraph(stripped)
        i += 1

    return doc


def save_minutes_docx(markdown: str, out_dir: str | Path) -> Path:
    """議事録 Markdown を out_dir/minutes.docx として書き出し、そのパスを返す。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "minutes.docx"
    markdown_to_docx(markdown).save(str(path))
    return path
