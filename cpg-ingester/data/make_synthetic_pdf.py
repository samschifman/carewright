#!/usr/bin/env python3
"""Render the synthetic hypertension v2 Markdown guideline as a PDF."""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

# Set before constructing any ReportLab document so metadata and document IDs
# are stable across runs.
import reportlab.rl_config

reportlab.rl_config.invariant = 1

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

DATA_DIR = Path(__file__).resolve().parent
MARKDOWN_PATH = DATA_DIR / "synthetic-hypertension-cpg-v2.md"
PDF_PATH = DATA_DIR / "synthetic-hypertension-cpg-v2.pdf"
PAGE_WIDTH, _ = LETTER
LEFT_MARGIN = 0.68 * inch
RIGHT_MARGIN = 0.68 * inch
TABLE_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)(.*)$")
_ORDERED_ITEM_RE = re.compile(r"^\s*\d+\.\s+")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _register_fonts() -> None:
    """Use ReportLab's bundled Vera fonts for the guideline's Unicode text."""
    import reportlab

    fonts_dir = Path(reportlab.__file__).resolve().parent / "fonts"
    pdfmetrics.registerFont(TTFont("Vera", str(fonts_dir / "Vera.ttf")))
    pdfmetrics.registerFont(TTFont("Vera-Bold", str(fonts_dir / "VeraBd.ttf")))
    pdfmetrics.registerFont(TTFont("Vera-Italic", str(fonts_dir / "VeraIt.ttf")))
    pdfmetrics.registerFont(TTFont("Vera-BoldItalic", str(fonts_dir / "VeraBI.ttf")))
    pdfmetrics.registerFontFamily(
        "Vera", normal="Vera", bold="Vera-Bold", italic="Vera-Italic",
        boldItalic="Vera-BoldItalic",
    )


def _inline_markup(text: str) -> str:
    """Convert the small inline Markdown subset used by this CPG to ReportLab."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`]+)`", r'<font name="Vera">\1</font>', escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)
    return escaped


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "CpgTitle", parent=base["Title"], fontName="Vera-Bold", fontSize=17,
            leading=21, alignment=TA_CENTER, textColor=colors.HexColor("#17324d"),
            spaceAfter=10,
        ),
        "h1": ParagraphStyle(
            "CpgH1", parent=base["Heading1"], fontName="Vera-Bold", fontSize=14,
            leading=18, textColor=colors.HexColor("#17324d"), spaceBefore=13,
            spaceAfter=6, keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "CpgH2", parent=base["Heading2"], fontName="Vera-Bold", fontSize=11,
            leading=14, textColor=colors.HexColor("#275b7a"), spaceBefore=10,
            spaceAfter=4, keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "CpgH3", parent=base["Heading3"], fontName="Vera-Bold", fontSize=10,
            leading=13, spaceBefore=8, spaceAfter=3, keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "CpgBody", parent=base["BodyText"], fontName="Vera", fontSize=8.6,
            leading=12, alignment=TA_LEFT, spaceAfter=6,
        ),
        "list": ParagraphStyle(
            "CpgList", parent=base["BodyText"], fontName="Vera", fontSize=8.6,
            leading=12, spaceAfter=2,
        ),
        "table": ParagraphStyle(
            "CpgTable", parent=base["BodyText"], fontName="Vera", fontSize=7,
            leading=9, spaceAfter=0,
        ),
        "table_header": ParagraphStyle(
            "CpgTableHeader", parent=base["BodyText"], fontName="Vera-Bold",
            fontSize=7, leading=9, textColor=colors.white, spaceAfter=0,
        ),
        "meta": ParagraphStyle(
            "CpgMeta", parent=base["BodyText"], fontName="Vera", fontSize=8,
            leading=10, spaceAfter=2,
        ),
    }


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _make_table(rows: list[list[str]], styles: dict[str, ParagraphStyle]) -> Table:
    rendered = []
    for row_index, row in enumerate(rows):
        style = styles["table_header"] if row_index == 0 else styles["table"]
        rendered.append([Paragraph(_inline_markup(cell), style) for cell in row])
    column_count = max(len(row) for row in rows)
    widths = [TABLE_WIDTH / column_count] * column_count
    table = Table(rendered, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#275b7a")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#9aa9b5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f7")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _render_markdown(markdown: str, styles: dict[str, ParagraphStyle]) -> list[object]:
    lines = markdown.splitlines()
    story: list[object] = []
    paragraph_lines: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph_lines:
            text = " ".join(line.strip() for line in paragraph_lines)
            story.append(Paragraph(_inline_markup(text), styles["body"]))
            paragraph_lines.clear()

    while index < len(lines):
        line = lines[index].strip()
        if not line:
            flush_paragraph()
            index += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            flush_paragraph()
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            style = "title" if level == 1 else "h1" if level == 2 else "h2" if level == 3 else "h3"
            story.append(Paragraph(_inline_markup(heading.group(2)), styles[style]))
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(lines[index + 1]):
            flush_paragraph()
            rows = [_split_table_row(line)]
            index += 2  # header and separator
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_table_row(lines[index].strip()))
                index += 1
            story.append(_make_table(rows, styles))
            story.append(Spacer(1, 8))
            continue
        if _LIST_ITEM_RE.match(line):
            flush_paragraph()
            ordered = bool(_ORDERED_ITEM_RE.match(line))
            items = []
            while index < len(lines):
                match = _LIST_ITEM_RE.match(lines[index].strip())
                if not match:
                    break
                items.append(ListItem(Paragraph(_inline_markup(match.group(1)), styles["list"])))
                index += 1
            story.append(ListFlowable(
                items, bulletType="1" if ordered else "bullet", start="1",
                leftIndent=18, bulletFontName="Vera", bulletFontSize=8,
                spaceAfter=6,
            ))
            continue
        paragraph_lines.append(line)
        index += 1

    flush_paragraph()
    return story


def generate_pdf(markdown_path: Path = MARKDOWN_PATH, output_path: Path = PDF_PATH) -> Path:
    """Render one Markdown source file to a deterministic, searchable PDF."""
    _register_fonts()
    styles = _styles()
    story = _render_markdown(markdown_path.read_text(encoding="utf-8"), styles)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=LETTER, title="Initial Management of Hypertension in Adults",
        author="Synthetic Guidelines Collaborative", subject="Synthetic guideline v2.0",
        creator="cpg-to-acp synthetic CPG renderer", leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN, topMargin=0.65 * inch, bottomMargin=0.65 * inch,
    )
    doc.build(story)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", type=Path, default=MARKDOWN_PATH)
    parser.add_argument("--output", type=Path, default=PDF_PATH)
    args = parser.parse_args()
    generate_pdf(args.markdown, args.output)


if __name__ == "__main__":
    main()
