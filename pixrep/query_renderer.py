"""Render query results into PDF/PNG output."""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .flowables import CodeBlockChunk, HeaderBar, StatBox
from .fonts import FontRegistry, register_fonts
from .models import RepoInfo
from .query import CodeSnippet
from .theme import COLORS
from .utils import xml_escape


class QueryResultRenderer:
    """Render a list of CodeSnippets into a single PDF/PNG."""

    def __init__(
        self,
        repo: RepoInfo,
        query: str,
        snippets: list[CodeSnippet],
        output_path: Path,
        fonts: FontRegistry | None = None,
        output_format: str = "pdf",
        png_dpi: int = 150,
    ):
        self.repo = repo
        self.query = query
        self.snippets = snippets
        self.output_path = output_path
        self.fonts = fonts or register_fonts()
        self.output_format = output_format
        self.png_dpi = png_dpi

        self.page_width, self.page_height = A4
        self.margin = 15 * mm
        self.content_width = self.page_width - 2 * self.margin

    def render(self) -> None:
        story = self._build_story()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        if self.output_format == "pdf":
            doc = SimpleDocTemplate(
                str(self.output_path),
                pagesize=A4,
                leftMargin=self.margin,
                rightMargin=self.margin,
                topMargin=self.margin,
                bottomMargin=15 * mm,
            )
            doc.build(story, onFirstPage=self._footer, onLaterPages=self._footer)
            return

        import tempfile

        from .utils import pdf_to_long_png

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            doc = SimpleDocTemplate(
                str(tmp_path),
                pagesize=A4,
                leftMargin=self.margin,
                rightMargin=self.margin,
                topMargin=self.margin,
                bottomMargin=15 * mm,
            )
            doc.build(story, onFirstPage=self._footer, onLaterPages=self._footer)
            images = pdf_to_long_png(tmp_path, dpi=self.png_dpi)
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

        if len(images) == 1:
            self.output_path.write_bytes(next(iter(images.values())))
        else:
            stem = self.output_path.stem
            for name, data in images.items():
                self.output_path.with_name(f"{stem}_{name}.png").write_bytes(data)

    def _build_story(self) -> list:
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

        story: list = []
        styles = getSampleStyleSheet()
        content_width = self.content_width

        title_style = ParagraphStyle(
            "QTitle",
            parent=styles["Title"],
            fontSize=22,
            textColor=COLORS["accent"],
            fontName=self.fonts.bold,
            spaceAfter=4 * mm,
        )
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(f"Query: {xml_escape(self.query)}", title_style))

        sub_style = ParagraphStyle(
            "QSub",
            parent=styles["Normal"],
            fontSize=9,
            textColor=HexColor("#888888"),
            fontName=self.fonts.normal,
            spaceAfter=6 * mm,
        )
        unique_files = len({s.rel_path for s in self.snippets})
        story.append(
            Paragraph(
                f"{self.repo.name} · {len(self.snippets)} snippets in {unique_files} files · "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                sub_style,
            )
        )

        box_width = (content_width - 15) / 3
        stats_table = Table(
            [
                [
                    StatBox(
                        "MATCHES",
                        str(sum(len(s.match_lines) for s in self.snippets)),
                        COLORS["accent"],
                        fonts=self.fonts,
                        width=box_width,
                        height=45,
                    ),
                    StatBox(
                        "FILES",
                        str(unique_files),
                        COLORS["accent2"],
                        fonts=self.fonts,
                        width=box_width,
                        height=45,
                    ),
                    StatBox(
                        "SNIPPETS",
                        str(len(self.snippets)),
                        COLORS["green"],
                        fonts=self.fonts,
                        width=box_width,
                        height=45,
                    ),
                ]
            ],
            colWidths=[box_width + 5] * 3,
        )
        stats_table.setStyle(
            TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        story.append(stats_table)
        story.append(Spacer(1, 8 * mm))

        for idx, snippet in enumerate(self.snippets, 1):
            story.append(
                HeaderBar(
                    f"[{idx}] {snippet.rel_path}",
                    f"{snippet.language} · lines {snippet.start_line}–{snippet.end_line}",
                    fonts=self.fonts,
                    width=content_width,
                )
            )
            story.append(Spacer(1, 2 * mm))

            line_heat = {line: "match" for line in snippet.match_lines}
            story.append(
                CodeBlockChunk(
                    snippet.lines,
                    snippet.language,
                    fonts=self.fonts,
                    start_line=snippet.start_line,
                    width=content_width,
                    font_size=6.5,
                    line_heat=line_heat,
                )
            )
            story.append(Spacer(1, 6 * mm))

            if idx % 18 == 0 and idx < len(self.snippets):
                story.append(PageBreak())

        return story

    def _footer(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(self.fonts.normal, 7)
        canvas.setFillColor(HexColor("#999999"))
        canvas.drawString(self.margin, 10 * mm, f"pixrep query · {self.repo.name}")
        canvas.drawRightString(self.page_width - self.margin, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()
