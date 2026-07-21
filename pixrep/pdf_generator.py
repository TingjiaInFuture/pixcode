import hashlib
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

from .analysis import CodeInsightEngine
from .file_utils import normalize_posix_path
from .flowables import CodeBlockChunk
from .fonts import FontRegistry, register_fonts
from .manifest import BuildManifest, FileEntry, compute_options_hash
from .models import FileInfo, RepoInfo
from .pdf_story_builders import build_file_story, build_index_story
from .theme import COLORS
from .utils import xml_escape
from .version import __version__


log = logging.getLogger(__name__)


class PDFGenerator:
    def __init__(self, repo: RepoInfo, output_dir: str,
                 fonts: FontRegistry | None = None,
                 enable_semantic_minimap: bool = True,
                 enable_lint_heatmap: bool = True,
                 linter_timeout: int = 20,
                 incremental: bool = False,
                 max_workers: int | None = None,
                 output_format: str = "pdf",
                 png_dpi: int = 150):
        self.repo = repo
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.fonts = fonts or register_fonts()
        self.page_width, self.page_height = A4
        self.margin = 15 * mm
        self.content_width = self.page_width - 2 * self.margin
        self.avail_height = self.page_height - self.margin - 15 * mm
        self.enable_semantic_minimap = enable_semantic_minimap
        self.enable_lint_heatmap = enable_lint_heatmap
        self.incremental = incremental
        # None → use CPU count, capped at 8 to avoid over-subscription.
        self.max_workers = max_workers if max_workers is not None else min(8, os.cpu_count() or 1)
        self.output_format = output_format
        self.png_dpi = png_dpi
        self.streaming_file_threshold = 256 * 1024
        # Fingerprint the rendering/cache options so that changing theme, font,
        # DPI, format, semantic/lint toggles, linter version/config or pixrep
        # version invalidates stale semantic/lint caches and rendered outputs.
        self.repo.options_hash = compute_options_hash(
            theme_id=self._theme_fingerprint(),
            font_id=self._font_fingerprint(),
            png_dpi=png_dpi,
            output_format=output_format,
            enable_semantic=enable_semantic_minimap,
            enable_lint=enable_lint_heatmap,
            syntax_mode="full",
            repo_root=self.repo.root,
        )
        self.insight_engine = CodeInsightEngine(
            repo,
            enable_semantic_minimap=enable_semantic_minimap,
            enable_lint_heatmap=enable_lint_heatmap,
            linter_timeout=linter_timeout,
        )

    def _file_out_name(self, info: FileInfo, ext: str | None = None) -> str:
        """生成输出文件名，ext 为 None 时使用 self.output_format。"""
        if ext is None:
            ext = self.output_format
        safe_path = str(info.path).replace("/", "_").replace("\\", "_")
        safe_path = re.sub(r"[^\w\-_.]", "_", safe_path)
        return f"{info.index:03d}_{safe_path}.{ext}"

    def _file_pdf_name(self, info: FileInfo) -> str:
        """返回输出文件名（使用当前 output_format 后缀）。"""
        return self._file_out_name(info)

    def generate_all(self):
        """Generate index + one output file per source file into output_dir.

        Execution order (P0-1): compute pending from the manifest first, render
        the index (it does not need semantic/lint), analyze only the pending
        files, render them, then persist the manifest.
        """
        fmt_label = self.output_format.upper()
        log.info("")
        log.info("Project: %s", self.repo.name)
        log.info("Files: %d, Lines: %d", len(self.repo.files), self.repo.total_lines)
        log.info("Output: %s", self.output_dir)
        log.info("Format: %s", fmt_label)
        if self.incremental:
            log.info("Mode: incremental (skipping up-to-date files)")
        log.info("")

        manifest = BuildManifest.load(self.insight_engine.manifest_path)
        pending = [
            info for info in self.repo.files
            if self._needs_regeneration(info, manifest)
        ]
        skipped = len(self.repo.files) - len(pending)
        if skipped:
            log.info("  Skipping %d up-to-date file %ss", skipped, fmt_label)

        # The index only depends on the file list/stats, not on semantic or
        # lint results, so it can be rendered before analysis.
        self._generate_index()

        # Analyze only the files that actually need (re-)rendering (P0-1).
        self.insight_engine.enrich_files(pending)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._generate_file_output, info): info for info in pending}
            total = len(futures)
            for index, fut in enumerate(as_completed(futures), start=1):
                exc = fut.exception()
                if exc:
                    info = futures[fut]
                    log.warning("  Failed to generate %s for %s: %s", fmt_label, info.path, exc)
                if index % 10 == 0 or index == total:
                    log.info("  Progress: %d/%d files", index, total)

        self._save_manifest(manifest)
        log.info("")
        log.info("Done! Generated %d %ss (+ index)", len(pending), fmt_label)

    def generate_index_only(self) -> None:
        """Generate only the index file into output_dir."""
        self.insight_engine.enrich_repo()
        self._generate_index()

    def _needs_regeneration(self, info: FileInfo, manifest: BuildManifest) -> bool:
        """Return True if the output file must be (re-)generated.

        Uses the build manifest (content fingerprint + options hash + version)
        instead of a raw mtime comparison, so that editing rendering options or
        bumping pixrep correctly invalidates stale outputs.
        """
        if not self.incremental:
            return True
        out_name = self._file_out_name(info)
        out_path = self.output_dir / out_name
        if not out_path.exists():
            return True
        entry = manifest.files.get(normalize_posix_path(info.path))
        if entry is None:
            return True
        if entry.git_blob != (info.git_blob or ""):
            return True
        if entry.output != out_name:
            return True
        if manifest.options_hash != self.repo.options_hash:
            return True
        if manifest.pixrep_version != __version__:
            return True
        return False

    def _save_manifest(self, manifest: BuildManifest) -> None:
        """Persist current file fingerprints + options hash for the next run."""
        manifest.pixrep_version = __version__
        manifest.options_hash = self.repo.options_hash
        for info in self.repo.files:
            manifest.files[normalize_posix_path(info.path)] = FileEntry(
                git_blob=info.git_blob or "",
                size=info.size,
                output=self._file_out_name(info),
                mtime_ns=int(info.mtime_ns),
            )
        try:
            manifest.save()
        except OSError:
            log.debug("failed to save manifest", exc_info=True)

    def _theme_fingerprint(self) -> str:
        items = sorted((k, str(v.hexval())) for k, v in COLORS.items())
        return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()[:12]

    def _font_fingerprint(self) -> str:
        f = self.fonts
        return hashlib.sha1(
            repr((f.normal, f.bold, f.mono, f.mono_bold)).encode("utf-8")
        ).hexdigest()[:12]

    def _page_footer(self, canvas, doc):
        canvas.saveState()
        canvas.setFont(self.fonts.normal, 7)
        canvas.setFillColor(HexColor("#999999"))
        canvas.drawString(self.margin, 10 * mm,
                          f"pixrep · {self.repo.name}")
        canvas.drawRightString(self.page_width - self.margin, 10 * mm,
                               f"Page {doc.page}")
        canvas.restoreState()

    def _make_doc(self, target):
        """创建 SimpleDocTemplate。

        Parameters
        ----------
        target : str | Path | io.BytesIO
            输出目标——文件路径或内存缓冲区。
        """
        if isinstance(target, (str, Path)):
            dest = str(target)
        else:
            dest = target
        return SimpleDocTemplate(
            dest, pagesize=A4,
            leftMargin=self.margin, rightMargin=self.margin,
            topMargin=self.margin, bottomMargin=15 * mm,
        )

    def _build_and_save(self, story: list, out_path: Path) -> None:
        """构建 PDF 并根据 output_format 保存为 PDF 或 PNG。

        当 output_format == 'pdf' 时直接写入磁盘。
        当 output_format == 'png' 时先在内存中生成 PDF，再转为长图 PNG 写入磁盘。
        """
        if self.output_format == "pdf":
            doc = self._make_doc(out_path)
            doc.build(story,
                      onFirstPage=self._page_footer,
                      onLaterPages=self._page_footer)
        else:
            from .utils import pdf_bytes_to_long_png

            buf = io.BytesIO()
            doc = self._make_doc(buf)
            doc.build(story,
                      onFirstPage=self._page_footer,
                      onLaterPages=self._page_footer)
            pdf_bytes = buf.getvalue()
            png_bytes = pdf_bytes_to_long_png(pdf_bytes, dpi=self.png_dpi)
            out_path.write_bytes(png_bytes)

    def _cjk_style(self, name, parent_name="Normal", **kwargs):
        styles = getSampleStyleSheet()
        parent = styles[parent_name]
        defaults = {"fontName": self.fonts.normal, "fontSize": parent.fontSize}
        defaults.update(kwargs)
        return ParagraphStyle(name, parent=parent, **defaults)

    def _max_lines_for_height(self, avail_h, font_size=6.5):
        line_h = font_size * 1.6
        padding = 12
        return max(1, int((avail_h - padding) / line_h))

    def _generate_index(self):
        """生成索引文件（PDF 或 PNG）。"""
        ext = self.output_format
        out_path = self.output_dir / f"00_INDEX.{ext}"
        story = self._build_index_story()
        self._build_and_save(story, out_path)
        log.info("  00_INDEX.%s (%d files indexed)", ext, len(self.repo.files))

    def _build_index_story(self) -> list:
        return build_index_story(self)

    def _generate_file_output(self, file_info: FileInfo):
        """生成单个源文件的输出（PDF 或 PNG）。"""
        out_name = self._file_out_name(file_info)
        out_path = self.output_dir / out_name
        story = self._build_file_story(file_info)
        self._build_and_save(story, out_path)
        file_info.release_content()
        log.info("  %s (%d lines)", out_name, file_info.line_count)

    def _build_file_story(self, file_info: FileInfo) -> list:
        return build_file_story(self, file_info)

    def _add_code_chunks(self, story, all_lines, language, width,
                         first_avail, later_avail, font_size=6.5,
                         line_heat: dict[int, str] | None = None):
        offset = 0
        first_chunk = True
        while offset < len(all_lines):
            avail = first_avail if first_chunk else later_avail
            n = self._max_lines_for_height(avail, font_size)
            chunk = all_lines[offset:offset + n]

            story.append(CodeBlockChunk(
                chunk, language,
                fonts=self.fonts,
                start_line=offset + 1,
                width=width, font_size=font_size,
                line_heat=line_heat,
            ))

            offset += n
            first_chunk = False
            if offset < len(all_lines):
                if line_heat and (line_heat.get(offset) or line_heat.get(offset + 1)):
                    story.append(Spacer(1, 0))
                else:
                    story.append(Spacer(1, 1))

    def _add_code_chunks_streaming(self, story, abs_path: Path, language, width,
                                   first_avail, later_avail, font_size=6.5,
                                   line_heat: dict[int, str] | None = None):
        first_chunk = True
        line_no = 1

        try:
            with abs_path.open("r", encoding="utf-8", errors="replace") as f:
                while True:
                    avail = first_avail if first_chunk else later_avail
                    n = self._max_lines_for_height(avail, font_size)
                    chunk: list[str] = []
                    for _ in range(n):
                        line = f.readline()
                        if line == "":
                            break
                        chunk.append(line.rstrip("\n"))

                    if not chunk:
                        break

                    story.append(CodeBlockChunk(
                        chunk, language,
                        fonts=self.fonts,
                        start_line=line_no,
                        width=width, font_size=font_size,
                        line_heat=line_heat,
                    ))

                    line_no += len(chunk)
                    first_chunk = False

                    if len(chunk) == n:
                        if line_heat and (line_heat.get(line_no - 1) or line_heat.get(line_no)):
                            story.append(Spacer(1, 0))
                        else:
                            story.append(Spacer(1, 1))
        except OSError:
            story.append(CodeBlockChunk(
                ["(read failed)"], language,
                fonts=self.fonts,
                start_line=1,
                width=width, font_size=font_size,
                line_heat=line_heat,
            ))

    @staticmethod
    def _line_heat_map(info: FileInfo) -> dict[int, str]:
        line_map: dict[int, str] = {}
        for issue in info.lint_issues:
            if issue.line < 1:
                continue
            current = line_map.get(issue.line)
            if current == "high":
                continue
            if issue.severity == "high":
                line_map[issue.line] = "high"
            elif current is None:
                line_map[issue.line] = "medium"
        return line_map

    @staticmethod
    def _lint_counts(info: FileInfo) -> dict[str, int]:
        high = sum(1 for issue in info.lint_issues if issue.severity == "high")
        medium = sum(1 for issue in info.lint_issues if issue.severity != "high")
        return {"high": high, "medium": medium}

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / 1024 / 1024:.1f} MB"
