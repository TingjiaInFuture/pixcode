from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .fonts import FontRegistry, register_fonts


def pdf_escape_literal(s: str) -> str:
    # PDF literal string escaping.
    return (
        s.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


class StreamingPDFWriter:
    """
    Stream-oriented PDF writer for ONEPDF_CORE.

    Writes each page immediately without retaining the whole document in memory.
    Uses reportlab's canvas so CJK text can be rendered via a registered system font.
    """

    def __init__(self, title: str, out_path: Path, fonts: FontRegistry | None = None):
        self.title = title
        self.out_path = out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fonts = fonts or register_fonts()
        self._canvas = canvas.Canvas(str(out_path), pagesize=A4, pageCompression=1)
        self._canvas.setTitle(title)
        _, self._page_height = A4
        self.page_count = 0

    def add_page_lines(
        self,
        lines: list[str],
        *,
        font_size: int = 7,
        leading: int = 9,
        start_x: int = 36,
        top_margin: int = 36,
    ) -> None:
        text = self._canvas.beginText()
        text.setTextOrigin(start_x, self._page_height - top_margin - font_size)
        text.setFont(self._fonts.mono, font_size)
        text.setLeading(leading)
        for line in lines:
            text.textLine(line)
        self._canvas.drawText(text)
        self._canvas.showPage()
        self.page_count += 1

    def finalize(self) -> None:
        self._canvas.save()

