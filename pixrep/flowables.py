import re

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus.flowables import Flowable

from .fonts import COURIER_FALLBACK, FontRegistry
from .models import SemanticMap
from .syntax import BUILTIN_FUNCTIONS, COMMENT_STYLES, KEYWORDS
from .theme import COLORS
from .utils import str_width, truncate_to_width


_SPLIT_WS = re.compile(r"(\s+)")
_BACKTICK_RE = re.compile(r"(?<!\\)`")


def _strip_nonword(token: str) -> str:
    start = 0
    end = len(token)
    while start < end and not (token[start].isalnum() or token[start] == "_"):
        start += 1
    while end > start and not (token[end - 1].isalnum() or token[end - 1] == "_"):
        end -= 1
    return token[start:end]


def _is_simple_number(word: str) -> bool:
    if not word:
        return False
    dot_seen = False
    for ch in word:
        if ch == ".":
            if dot_seen:
                return False
            dot_seen = True
            continue
        if not ch.isdigit():
            return False
    return True


def _count_unescaped_triple_quotes(line: str) -> int:
    count = 0
    i = 0
    length = len(line)
    while i + 2 < length:
        marker = line[i : i + 3]
        if marker not in {"'''", '"""'}:
            i += 1
            continue

        backslashes = 0
        j = i - 1
        while j >= 0 and line[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 0:
            count += 1
            i += 3
            continue
        i += 1
    return count


class CodeBlockChunk(Flowable):
    """
    渲染一段代码行，保证高度不超页面。
    所有文字用注册的 mono 字体绘制。
    """

    def __init__(self, lines: list[str], language: str,
                 fonts: FontRegistry,
                 start_line: int = 1, width: float | None = None,
                 font_size: float = 6.5,
                 line_heat: dict[int, str] | None = None,
                 syntax: str = "full",
                 _precomputed_mask: list[bool] | None = None):
        super().__init__()
        self.code_lines = lines
        self.language = language
        self.start_line = start_line
        self.block_width = width or (A4[0] - 30 * mm)
        self.font_size = font_size
        self.line_height = font_size * 1.6
        self.padding = 6
        self.line_heat = line_heat or {}
        self._syntax = syntax
        # ASCII-only files use built-in Courier so their PDFs do not embed the
        # CJK font (P1-6). Files with any non-ASCII byte keep the CJK registry.
        ascii_only = all(line.isascii() for line in lines)
        self.fonts = COURIER_FALLBACK if ascii_only else fonts

        self.kw_set = KEYWORDS.get(language, set())
        self.builtin_set = BUILTIN_FUNCTIONS.get(language, set())
        self.line_comment = COMMENT_STYLES.get(language)
        self._ml_string_mask = _precomputed_mask if _precomputed_mask is not None else self._compute_multiline_string_mask()

    def _compute_multiline_string_mask(self) -> list[bool]:
        """
        Best-effort multiline string/comment masking.

        This is intentionally heuristic: we avoid expensive parsing but prevent
        obvious false highlighting in long blocks.
        """
        mask: list[bool] = []
        in_ml = False
        if self.language == "python":
            for line in self.code_lines:
                mask.append(in_ml)
                toggles = _count_unescaped_triple_quotes(line) % 2
                if toggles:
                    # The line with the delimiter is part of the string region.
                    mask[-1] = True
                    in_ml = not in_ml
            return mask

        if self.language in {"javascript", "typescript"}:
            for line in self.code_lines:
                mask.append(in_ml)
                # Fast-path for common lines without template literals.
                if "`" not in line:
                    continue
                n = len(_BACKTICK_RE.findall(line))
                if n % 2:
                    mask[-1] = True
                    in_ml = not in_ml
            return mask

        return [False] * len(self.code_lines)

    def wrap(self, availWidth, availHeight):
        self.block_width = min(self.block_width, availWidth)
        h = len(self.code_lines) * self.line_height + self.padding * 2
        return (self.block_width, h)

    def split(self, availWidth, availHeight):
        self.block_width = min(self.block_width, availWidth)
        if availWidth <= 0:
            return []

        epsilon = 0.01
        full_height = len(self.code_lines) * self.line_height + self.padding * 2
        if full_height <= availHeight + epsilon:
            return [self]

        max_lines = int((availHeight - self.padding * 2) / self.line_height)
        while max_lines > 0:
            chunk_height = max_lines * self.line_height + self.padding * 2
            if chunk_height <= availHeight + epsilon:
                break
            max_lines -= 1
        if max_lines <= 0:
            return []

        first_chunk = CodeBlockChunk(
            self.code_lines[:max_lines],
            self.language,
            fonts=self.fonts,
            start_line=self.start_line,
            width=self.block_width,
            font_size=self.font_size,
            line_heat=self.line_heat,
            syntax=self._syntax,
            _precomputed_mask=self._ml_string_mask[:max_lines],
        )
        second_chunk = CodeBlockChunk(
            self.code_lines[max_lines:],
            self.language,
            fonts=self.fonts,
            start_line=self.start_line + max_lines,
            width=self.block_width,
            font_size=self.font_size,
            line_heat=self.line_heat,
            syntax=self._syntax,
            _precomputed_mask=self._ml_string_mask[max_lines:],
        )
        return [first_chunk, second_chunk]

    def draw(self):
        canv = self.canv
        num_lines = len(self.code_lines)
        total_h = num_lines * self.line_height + self.padding * 2

        canv.setFillColor(COLORS["bg"])
        canv.roundRect(0, 0, self.block_width, total_h, 4, fill=1, stroke=0)

        max_no = self.start_line + num_lines
        # Heuristic sizing: line numbers are rendered using monospace; keep a minimum gutter.
        min_gutter = 35
        digit_width = 7
        padding = 14
        line_no_width = max(min_gutter, len(str(max_no)) * digit_width + padding)

        canv.setFillColor(COLORS["header_bg"])
        canv.roundRect(0, 0, line_no_width, total_h, 4, fill=1, stroke=0)
        canv.rect(line_no_width - 4, 0, 4, total_h, fill=1, stroke=0)

        canv.setStrokeColor(COLORS["border"])
        canv.setLineWidth(0.5)
        canv.line(line_no_width, 0, line_no_width, total_h)

        code_x = line_no_width + 8
        code_area_width = self.block_width - code_x - 6

        for i, line in enumerate(self.code_lines):
            line_no = self.start_line + i
            row_top = total_h - self.padding - i * self.line_height
            line_bottom = row_top - self.line_height
            text_y = line_bottom + (self.line_height - self.font_size) * 0.45

            level = self.line_heat.get(line_no)
            if level:
                if level == "high":
                    heat_color = COLORS["heat_high"]
                    indicator_color = COLORS["red"]
                elif level == "match":
                    heat_color = COLORS.get("heat_match", COLORS["heat_medium"])
                    indicator_color = COLORS["accent"]
                else:
                    heat_color = COLORS["heat_medium"]
                    indicator_color = COLORS["number"]
                canv.setFillColor(heat_color)
                canv.rect(0, line_bottom, self.block_width, self.line_height, fill=1, stroke=0)

                canv.setFillColor(indicator_color)
                canv.rect(line_no_width - 3, line_bottom, 3, self.line_height, fill=1, stroke=0)

            canv.setFont("Courier", self.font_size)
            canv.setFillColor(COLORS["line_no"])
            canv.drawRightString(line_no_width - 6, text_y, str(line_no))

            display = truncate_to_width(line, self.font_size, code_area_width)
            self._draw_line(canv, code_x, text_y, display, in_multiline_string=self._ml_string_mask[i])

    def _draw_line(self, canv, x, y, line, in_multiline_string: bool = False):
        fs = self.font_size

        if self._syntax == "none":
            # Fast path: no highlighting, single drawString (--fast / --syntax none).
            canv.setFont(self.fonts.mono, fs)
            canv.setFillColor(COLORS["fg"])
            canv.drawString(x, y, line)
            return

        stripped = line.lstrip()

        if in_multiline_string:
            canv.setFont(self.fonts.mono, fs)
            canv.setFillColor(COLORS["string"])
            canv.drawString(x, y, line)
            return

        if self.line_comment and stripped.startswith(self.line_comment):
            canv.setFont(self.fonts.mono, fs)
            canv.setFillColor(COLORS["comment"])
            canv.drawString(x, y, line)
            return

        if self.language == "python" and stripped.startswith("@"):
            canv.setFont(self.fonts.mono, fs)
            canv.setFillColor(COLORS["function"])
            canv.drawString(x, y, line)
            return

        if stripped and stripped[0] in ('"', "'", "`"):
            canv.setFont(self.fonts.mono, fs)
            canv.setFillColor(COLORS["string"])
            canv.drawString(x, y, line)
            return

        segments = self._split_line_segments(line)
        cur_x = x
        batches: list[tuple[str, str, Color, float]] = []

        def add_batch(text: str, font: str, color: Color, draw_x: float):
            if not text:
                return
            if batches and batches[-1][1] == font and batches[-1][2] == color:
                prev_text, prev_font, prev_color, prev_x = batches[-1]
                batches[-1] = (prev_text + text, prev_font, prev_color, prev_x)
                return
            batches.append((text, font, color, draw_x))

        for seg_text, seg_kind in segments:
            if not seg_text:
                continue
            if seg_text.isspace():
                cur_x += str_width(seg_text, fs)
                continue

            if seg_kind == "comment":
                add_batch(seg_text, self.fonts.mono, COLORS["comment"], cur_x)
                cur_x += str_width(seg_text, fs)
                continue

            if seg_kind == "string":
                add_batch(seg_text, self.fonts.mono, COLORS["string"], cur_x)
                cur_x += str_width(seg_text, fs)
                continue

            tokens = _SPLIT_WS.split(seg_text)
            for token in tokens:
                if not token:
                    continue
                if token.isspace():
                    cur_x += str_width(token, fs)
                    continue

                color = COLORS["fg"]
                font = self.fonts.mono

                word = _strip_nonword(token)

                if word in self.kw_set:
                    color = COLORS["keyword"]
                    font = self.fonts.mono_bold
                elif word in self.builtin_set:
                    color = COLORS["type"]
                elif self.language == "python" and word in ("self", "cls"):
                    color = COLORS["red"]
                elif _is_simple_number(word):
                    color = COLORS["number"]

                add_batch(token, font, color, cur_x)
                cur_x += str_width(token, fs)

        last_font = None
        last_color = None
        for text, font, color, draw_x in batches:
            if font != last_font:
                canv.setFont(font, fs)
                last_font = font
            if color != last_color:
                canv.setFillColor(color)
                last_color = color
            canv.drawString(draw_x, y, text)

    def _split_line_segments(self, line: str) -> list[tuple[str, str]]:
        """
        Split a line into (text, kind) segments where kind is one of:
        - "code"
        - "string"
        - "comment"
        """
        comment = self.line_comment
        out: list[tuple[str, str]] = []
        buf: list[str] = []
        kind = "code"
        quote: str | None = None
        esc = False

        def flush():
            nonlocal buf
            if buf:
                out.append(("".join(buf), kind))
                buf = []

        i = 0
        while i < len(line):
            ch = line[i]

            if kind == "code" and comment and quote is None:
                # Start of line comment (outside any string).
                if line.startswith(comment, i):
                    flush()
                    out.append((line[i:], "comment"))
                    return out

            if kind == "code":
                if ch in ('"', "'", "`"):
                    flush()
                    kind = "string"
                    quote = ch
                    buf.append(ch)
                    esc = False
                    i += 1
                    continue
                buf.append(ch)
                i += 1
                continue

            # string
            if esc:
                buf.append(ch)
                esc = False
                i += 1
                continue
            if ch == "\\":
                buf.append(ch)
                esc = True
                i += 1
                continue
            buf.append(ch)
            i += 1
            if quote and ch == quote:
                flush()
                kind = "code"
                quote = None

        flush()
        return out


class SemanticMiniMap(Flowable):
    def __init__(self, semantic_map: SemanticMap, fonts: FontRegistry, width: float | None = None):
        super().__init__()
        self.semantic_map = semantic_map
        self.fonts = fonts
        self.map_width = width or (A4[0] - 30 * mm)
        self.padding = 6
        self.line_height = 9
        self.title_height = 14
        self._display_lines: list[str] = []
        self._show_summary_line = False

    def wrap(self, availWidth, availHeight):
        self.map_width = min(self.map_width, availWidth)
        rows = self._compute_layout_rows(availHeight)
        total_h = self.title_height + rows * self.line_height + self.padding * 2
        return (self.map_width, total_h)

    def _compute_layout_rows(self, availHeight: float | None) -> int:
        all_lines = list(self.semantic_map.lines)
        max_rows = len(all_lines) + (1 if self.semantic_map.truncated else 0)
        max_rows = max(1, max_rows)

        if availHeight is None or availHeight <= 0:
            allowed_rows = max_rows
        else:
            content_height = max(0, availHeight - self.title_height - self.padding * 2)
            allowed_rows = max(1, int(content_height / self.line_height))

        summary_needed = self.semantic_map.truncated
        summary_slots = 1 if summary_needed else 0
        line_slots = max(0, allowed_rows - summary_slots)
        shown_lines = all_lines[:line_slots]

        self._show_summary_line = summary_needed or len(all_lines) > len(shown_lines)
        if self._show_summary_line and len(shown_lines) == 0 and allowed_rows > 1 and all_lines:
            shown_lines = all_lines[:1]

        self._display_lines = shown_lines
        rows = len(self._display_lines) + (1 if self._show_summary_line else 0)
        return max(1, rows)

    def draw(self):
        canv = self.canv
        if not self._display_lines and not self._show_summary_line:
            self._compute_layout_rows(None)
        rows = len(self._display_lines) + (1 if self._show_summary_line else 0)
        rows = max(1, rows)
        total_h = self.title_height + rows * self.line_height + self.padding * 2

        canv.setFillColor(COLORS["bg_light"])
        canv.roundRect(0, 0, self.map_width, total_h, 4, fill=1, stroke=0)

        title_area_h = self.title_height + self.padding
        title_y = total_h - title_area_h
        canv.setFillColor(COLORS["header_bg"])
        canv.roundRect(0, title_y, self.map_width, title_area_h, 4, fill=1, stroke=0)
        canv.rect(0, title_y, self.map_width, 4, fill=1, stroke=0)

        canv.setStrokeColor(COLORS["border"])
        canv.setLineWidth(0.5)
        canv.line(0, title_y, self.map_width, title_y)

        canv.setFont(self.fonts.bold, 8)
        canv.setFillColor(COLORS["white"])
        title = "Semantic Minimap"
        kind = self.semantic_map.kind.upper()
        canv.drawString(8, total_h - self.title_height, f"{title}  [{kind}]")

        y = total_h - self.title_height - self.padding - self.line_height + 2
        canv.setFont(self.fonts.mono, 7)
        canv.setFillColor(COLORS["fg"])
        for line in self._display_lines:
            canv.drawString(8, y, truncate_to_width(line, 7, self.map_width - 16))
            y -= self.line_height

        if self._show_summary_line:
            canv.setFillColor(COLORS["comment"])
            summary = (
                f"... ({self.semantic_map.node_count} nodes, {self.semantic_map.edge_count} edges total)"
            )
            canv.drawString(8, y, truncate_to_width(summary, 7, self.map_width - 16))


class LintLegend(Flowable):
    def __init__(self, fonts: FontRegistry, width: float | None = None):
        super().__init__()
        self.fonts = fonts
        self.legend_width = width or (A4[0] - 30 * mm)
        self.legend_height = 14

    def wrap(self, availWidth, availHeight):
        self.legend_width = min(self.legend_width, availWidth)
        return (self.legend_width, self.legend_height)

    def draw(self):
        canv = self.canv
        y = 3
        x = 8

        canv.setFillColor(COLORS["heat_high"])
        canv.rect(x, y, 20, 8, fill=1, stroke=0)
        canv.setFont(self.fonts.mono, 6)
        canv.setFillColor(COLORS["fg"])
        canv.drawString(x + 24, y + 1, "High severity")

        x += 110
        canv.setFillColor(COLORS["heat_medium"])
        canv.rect(x, y, 20, 8, fill=1, stroke=0)
        canv.setFillColor(COLORS["fg"])
        canv.drawString(x + 24, y + 1, "Medium severity")


class HeaderBar(Flowable):
    def __init__(self, text: str, subtext: str = "",
                 fonts: FontRegistry | None = None,
                 width: float | None = None):
        super().__init__()
        self.text = text
        self.subtext = subtext
        self.bar_width = width or (A4[0] - 30 * mm)
        self.bar_height = 28 if subtext else 22
        self.fonts = fonts

    def wrap(self, availWidth, availHeight):
        self.bar_width = min(self.bar_width, availWidth)
        return (self.bar_width, self.bar_height)

    def draw(self):
        canv = self.canv
        canv.setFillColor(COLORS["accent"])
        canv.roundRect(0, 0, self.bar_width, self.bar_height, 4, fill=1, stroke=0)
        canv.setFont(self.fonts.bold if self.fonts else "Helvetica-Bold", 10)
        canv.setFillColor(COLORS["white"])
        canv.drawString(10, self.bar_height - 15, self.text)
        if self.subtext:
            canv.setFont(self.fonts.normal if self.fonts else "Helvetica", 7)
            canv.setFillColor(HexColor("#d0e8ff"))
            canv.drawString(10, 5, self.subtext)


class StatBox(Flowable):
    def __init__(self, label: str, value: str, color: Color,
                 fonts: FontRegistry | None = None,
                 width: float = 80, height: float = 50):
        super().__init__()
        self.label = label
        self.value = value
        self.color = color
        self.box_width = width
        self.box_height = height
        self.fonts = fonts

    def wrap(self, availWidth, availHeight):
        return (self.box_width, self.box_height)

    def draw(self):
        canv = self.canv
        canv.setFillColor(self.color)
        canv.roundRect(0, 0, self.box_width, self.box_height, 6, fill=1, stroke=0)
        canv.setFillColor(white)
        canv.setFont(self.fonts.bold if self.fonts else "Helvetica-Bold", 16)
        canv.drawCentredString(self.box_width / 2, self.box_height - 25,
                               str(self.value))
        canv.setFont(self.fonts.normal if self.fonts else "Helvetica", 8)
        canv.setFillColor(HexColor("#ffffffcc"))
        canv.drawCentredString(self.box_width / 2, 8, self.label)
