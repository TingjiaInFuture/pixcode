from __future__ import annotations

import os
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
    """Stream-oriented PDF writer for ONEPDF_CORE.

    Writes each page immediately without retaining the whole document in
    memory, and writes to a temp file that is fsync'd + atomically replaced on
    finalize so a mid-run crash never corrupts the previous good output.
    """

    def __init__(
        self,
        title: str,
        out_path: Path,
        fonts: FontRegistry | None = None,
        deterministic: bool = False,
    ):
        self.title = title
        self.out_path = out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fonts = fonts or register_fonts()
        # A real CJK font is registered (vs the built-in Courier fallback).
        self._cjk_active = self._fonts.mono not in {"Courier"}
        self._deterministic = deterministic
        self._tmp_path = out_path.with_name(out_path.name + ".tmp")
        # invariant=1 makes reportlab emit fixed timestamps / doc-id so two
        # runs over identical input produce byte-identical output.
        self._canvas = canvas.Canvas(
            str(self._tmp_path), pagesize=A4, pageCompression=1, invariant=deterministic
        )
        self._canvas.setTitle(title)
        if deterministic:
            self._canvas.setCreator("pixrep")
            self._canvas.setAuthor("pixrep")
            self._canvas.setSubject("pixrep ONEPDF_CORE")
        _, self._page_height = A4
        self.page_count = 0

    def _font_for_line(self, line: str) -> str:
        # ASCII-only lines render with the built-in Courier (no CJK embedding);
        # lines containing non-ASCII fall back to the registered CJK mono font.
        if self._cjk_active and line.isascii():
            return "Courier"
        return self._fonts.mono

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
        text.setLeading(leading)
        last_font = None
        for line in lines:
            font = self._font_for_line(line)
            if font != last_font:
                text.setFont(font, font_size)
                last_font = font
            text.textLine(line)
        self._canvas.drawText(text)
        self._canvas.showPage()
        self.page_count += 1

    def finalize(self) -> None:
        self._canvas.save()
        # fsync the temp file then atomically replace the destination, so the
        # output is either fully old or fully new — never half-written.
        try:
            with open(self._tmp_path, "rb") as f:
                os.fsync(f.fileno())
        except OSError:
            pass
        os.replace(self._tmp_path, self.out_path)
