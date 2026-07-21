from functools import lru_cache


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@lru_cache(maxsize=8192)
def char_width(char: str, font_size: float) -> float:
    """
    估算单个字符的渲染宽度。
    CJK字符约为 font_size 宽，ASCII约为 font_size * 0.6。
    """
    cp = ord(char)
    if (
        (0x2E80 <= cp <= 0x9FFF)
        or (0xF900 <= cp <= 0xFAFF)
        or (0xFE30 <= cp <= 0xFE4F)
        or (0xFF00 <= cp <= 0xFFEF)
        or (0x20000 <= cp <= 0x2FA1F)
        or (0x3000 <= cp <= 0x303F)
    ):
        return font_size * 1.0
    return font_size * 0.6


@lru_cache(maxsize=32768)
def _str_width_cached(text: str, font_size: float) -> float:
    return sum(char_width(c, font_size) for c in text)


def str_width(text: str, font_size: float) -> float:
    """
    估算字符串的渲染宽度。

    Note: caching full strings can create memory pressure for huge files.
    We only cache "short" strings; long strings are computed directly.
    """
    if text.isascii():
        return len(text) * font_size * 0.6
    if len(text) > 256:
        return sum(char_width(c, font_size) for c in text)
    return _str_width_cached(text, font_size)


def truncate_to_width(text: str, font_size: float, max_width: float) -> str:
    """将字符串截断到不超过 max_width 像素宽度"""
    if text.isascii():
        max_chars = int(max_width / (font_size * 0.6))
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars)] + "…"

    w = 0.0
    for i, c in enumerate(text):
        w += char_width(c, font_size)
        if w > max_width:
            return text[:i] + "…"
    return text


def _encode_png(image, optimize: bool) -> bytes:
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


def _group_pages(
    page_wh: list[tuple[int, int]],
    max_total_pixels: int,
    split: bool,
) -> list[tuple[list[int], list[tuple[int, int]]]]:
    """Partition pages into canvas groups. A single group unless ``split``."""
    n = len(page_wh)
    if n == 0:
        return []
    if not split:
        return [(list(range(n)), list(page_wh))]
    max_w_all = max(w for w, _ in page_wh)
    max_rows_per_group = max(1, int(max_total_pixels / max(1, max_w_all)))
    groups: list[tuple[list[int], list[tuple[int, int]]]] = []
    cur_idx: list[int] = []
    cur_wh: list[tuple[int, int]] = []
    cur_rows = 0
    for i, (w, h) in enumerate(page_wh):
        if cur_wh and cur_rows + h > max_rows_per_group:
            groups.append((cur_idx, cur_wh))
            cur_idx, cur_wh, cur_rows = [], [], 0
        cur_idx.append(i)
        cur_wh.append((w, h))
        cur_rows += h
    if cur_wh:
        groups.append((cur_idx, cur_wh))
    return groups


def pdf_to_long_png(
    pdf_source,
    *,
    dpi: int = 150,
    max_total_pixels: int = 120_000_000,
    optimize: bool = False,
    split: bool = False,
) -> dict[str, bytes]:
    """将 PDF 渲染为 PNG 长图，返回 {stem: png_bytes}。

    Pre-computes the render scale from page rects **before** rendering (P0-3):
    full-resolution pages are never materialised in memory, and each rendered
    page is pasted onto the canvas and closed immediately. When ``split`` is
    True and a single stitched canvas would exceed the pixel budget, pages are
    grouped into multiple images ("0001", "0002", ...).

    Parameters
    ----------
    pdf_source : bytes | Path
        PDF 内容（内存字节）或 PDF 文件路径。传路径可避免在内存中保留
        完整 PDF 字节副本。
    dpi : int
        目标渲染分辨率（在不超过像素预算的前提下）。
    max_total_pixels : int
        单张长图的最大像素总数；超限时整体缩小。
    optimize : bool
        是否开启 Pillow PNG 优化（默认关闭以省 CPU）。
    split : bool
        超像素预算时是否输出多张分页 PNG（默认 False，仅缩小）。
    """

    import fitz
    from PIL import Image

    if isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=bytes(pdf_source), filetype="pdf")
    else:
        doc = fitz.open(str(pdf_source), filetype="pdf")

    try:
        page_count = doc.page_count
        if page_count == 0:
            return {"image": _encode_png(Image.new("RGB", (1, 1), color="white"), optimize)}

        base_scale = dpi / 72.0
        page_rects = [doc[i].rect for i in range(page_count)]
        # Estimated total pixels at the requested DPI, before any rendering.
        estimated = sum(r.width * r.height for r in page_rects) * base_scale * base_scale
        scale = base_scale
        if estimated > max_total_pixels and estimated > 0:
            scale = base_scale * (max_total_pixels / estimated) ** 0.5

        matrix = fitz.Matrix(scale, scale)
        page_wh = [
            (max(1, int(r.width * scale)), max(1, int(r.height * scale))) for r in page_rects
        ]

        groups = _group_pages(page_wh, max_total_pixels, split)
        result: dict[str, bytes] = {}
        for gi, (indices, gwh) in enumerate(groups):
            group_width = max(w for w, _ in gwh)
            group_height = sum(h for _, h in gwh)
            canvas = Image.new("RGB", (group_width, group_height), color="white")
            y = 0
            for idx, (_w, h) in zip(indices, gwh, strict=False):
                pix = doc[idx].get_pixmap(matrix=matrix)
                page_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                canvas.paste(page_img, (0, y))
                y += h
                page_img.close()
            stem = "image" if len(groups) == 1 else f"{gi + 1:04d}"
            result[stem] = _encode_png(canvas, optimize)
            canvas.close()
        return result
    finally:
        doc.close()
