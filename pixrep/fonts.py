import hashlib
import logging
import os
from dataclasses import dataclass

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FontRegistry:
    normal: str
    bold: str
    mono: str
    mono_bold: str
    # Stable identity of the fonts actually backing this registry: the resolved
    # font file path(s) + size + mtime_ns. Two runs that pick a different system
    # CJK font (e.g. Microsoft YaHei → Noto Sans CJK) get a different
    # fingerprint, so --incremental can't reuse a PDF rendered with the old
    # font. Empty for the built-in Courier fallback (no embedded file).
    fingerprint: str = ""


def _font_fingerprint(*file_paths: str) -> str:
    """Hash the resolved font files' identity (path + size + mtime_ns).

    Duplicate paths collapse, so a registry that points normal/mono/bold at the
    same file hashes it once. All-empty input yields a stable value that still
    differs from any real-font fingerprint."""
    parts: list[str] = []
    seen: set[str] = set()
    for p in file_paths:
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            st = os.stat(p)
            parts.append(f"{p}|{st.st_size}|{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{p}|?|?")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def register_fonts() -> FontRegistry:
    """
    注册 CJK 字体。关键点：
    - 比例字体: 用于标题、段落、表格等
    - 等宽字体: 用于代码块。系统一般没有中文等宽字体，
      所以用同一个 CJK 字体同时作为 mono 使用。
    """
    candidates = [
        (r"C:\Windows\Fonts\msyh.ttc", "CJK_Normal"),
        (r"C:\Windows\Fonts\msyhbd.ttc", "CJK_Bold"),
        (r"C:\Windows\Fonts\simhei.ttf", "CJK_Normal"),
        (r"C:\Windows\Fonts\simsun.ttc", "CJK_Normal"),
        ("/System/Library/Fonts/PingFang.ttc", "CJK_Normal"),
        ("/System/Library/Fonts/STHeiti Medium.ttc", "CJK_Normal"),
        ("/System/Library/Fonts/STHeiti Light.ttc", "CJK_Normal"),
        ("/Library/Fonts/Arial Unicode.ttf", "CJK_Normal"),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "CJK_Normal"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "CJK_Normal"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "CJK_Normal"),
        ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "CJK_Normal"),
        ("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc", "CJK_Normal"),
    ]

    registered_normal = False
    registered_bold = False
    normal_path = ""
    bold_path = ""

    for font_path, font_name in candidates:
        if not os.path.exists(font_path):
            continue
        try:
            if font_name == "CJK_Bold" and not registered_bold:
                pdfmetrics.registerFont(TTFont("CJK_Bold", font_path))
                registered_bold = True
                bold_path = font_path
                log.info("  CJK Bold  : %s", font_path)
            elif font_name == "CJK_Normal" and not registered_normal:
                pdfmetrics.registerFont(TTFont("CJK_Normal", font_path))
                registered_normal = True
                normal_path = font_path
                log.info("  CJK Normal: %s", font_path)
                if not registered_bold:
                    try:
                        pdfmetrics.registerFont(TTFont("CJK_Bold", font_path))
                        registered_bold = True
                        bold_path = font_path
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("  Font registration failed for %s: %s", font_path, exc)
            continue

        if registered_normal and registered_bold:
            break

    if registered_normal:
        log.info("  All rendering will use CJK font")
        return FontRegistry(
            normal="CJK_Normal",
            bold="CJK_Bold" if registered_bold else "CJK_Normal",
            mono="CJK_Normal",
            mono_bold="CJK_Bold" if registered_bold else "CJK_Normal",
            fingerprint=_font_fingerprint(normal_path, bold_path),
        )

    log.warning("  No CJK font found. Chinese characters may render as tofu squares.")
    log.warning("  Windows: msyh.ttc should exist in C:\\Windows\\Fonts\\")
    log.warning("  Linux  : apt install fonts-wqy-microhei")
    log.warning("  macOS  : PingFang should be built-in")
    return FontRegistry(
        normal="Helvetica",
        bold="Helvetica-Bold",
        mono="Courier",
        mono_bold="Courier-Bold",
        # Built-in PDF fonts have no embedded file; a constant still differs
        # from any real-font fingerprint, so a CJK↔fallback switch is detected.
        fingerprint=_font_fingerprint(),
    )


# Built-in PDF fonts (no CJK embedding). Used for ASCII-only files so their
# PDFs stay small (P1-6).
COURIER_FALLBACK = FontRegistry(
    normal="Helvetica",
    bold="Helvetica-Bold",
    mono="Courier",
    mono_bold="Courier-Bold",
)
