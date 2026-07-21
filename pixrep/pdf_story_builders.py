from __future__ import annotations

from datetime import datetime

from reportlab.lib.colors import HexColor, white
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, Spacer, Table, TableStyle

from .flowables import HeaderBar, LintLegend, SemanticMiniMap, StatBox
from .models import FileInfo
from .theme import COLORS
from .utils import xml_escape


def build_index_story(gen) -> list:
    story = []
    cw = gen.content_width

    story.append(Spacer(1, 10 * mm))
    title_style = gen._cjk_style(
        "CTitle", "Title", fontSize=28,
        textColor=COLORS["accent"], fontName=gen.fonts.bold,
        spaceAfter=4 * mm,
    )
    story.append(Paragraph(xml_escape(gen.repo.name), title_style))

    sub_style = gen._cjk_style(
        "CSub", fontSize=10,
        textColor=HexColor("#888888"), spaceAfter=8 * mm,
    )
    story.append(Paragraph(
        "Code Repository Overview · Generated "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        sub_style))

    bw = (cw - 20) / 4
    stat_data = [[
        StatBox("FILES", str(len(gen.repo.files)),
                COLORS["accent"], fonts=gen.fonts, width=bw, height=50),
        StatBox("LINES", f"{gen.repo.total_lines:,}",
                COLORS["accent2"], fonts=gen.fonts, width=bw, height=50),
        StatBox("SIZE", gen._fmt_size(gen.repo.total_size),
                COLORS["green"], fonts=gen.fonts, width=bw, height=50),
        StatBox("LANGUAGES", str(len(gen.repo.language_stats)),
                COLORS["type"], fonts=gen.fonts, width=bw, height=50),
    ]]
    table = Table(stat_data, colWidths=[bw + 5] * 4)
    table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(table)
    story.append(Spacer(1, 8 * mm))

    story.append(HeaderBar("Language Statistics", fonts=gen.fonts, width=cw))
    story.append(Spacer(1, 3 * mm))

    ns = gen._cjk_style("CN", fontSize=8)
    lang_data = [[
        Paragraph("<b>Language</b>", ns),
        Paragraph("<b>Files</b>", ns),
        Paragraph("<b>Lines</b>", ns),
        Paragraph("<b>%</b>", ns),
    ]]
    for lang, stats in gen.repo.language_stats.items():
        pct = (stats["lines"] / max(gen.repo.total_lines, 1)) * 100
        lang_data.append([
            Paragraph(
                f'<font color="{COLORS["accent"].hexval()}">'
                f"{xml_escape(lang)}</font>", ns),
            Paragraph(str(stats["files"]), ns),
            Paragraph(f"{stats['lines']:,}", ns),
            Paragraph(f"{pct:.1f}%", ns),
        ])
    lang_table = Table(lang_data,
                       colWidths=[cw * 0.35, cw * 0.2, cw * 0.25, cw * 0.2])
    lang_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLORS["header_bg"]),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.5, COLORS["border"]),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [white, HexColor("#f8f9fa")]),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    story.append(lang_table)
    story.append(Spacer(1, 8 * mm))

    story.append(HeaderBar("Directory Structure", fonts=gen.fonts, width=cw))
    story.append(Spacer(1, 3 * mm))

    tree_str = (
        gen.repo.tree_str
        .replace("\u251c\u2500\u2500 ", "|-- ")
        .replace("\u2514\u2500\u2500 ", "'-- ")
        .replace("\u2502   ", "|   ")
        .replace("\u2500", "-")
        .replace("\u2502", "|")
    )
    tree_lines = tree_str.split("\n")
    if len(tree_lines) > 120:
        tree_lines = tree_lines[:120] + [
            f"  ... ({len(tree_lines)} entries total)"]

    gen._add_code_chunks(story, tree_lines, "text", cw,
                         first_avail=gen.avail_height - 300,
                         later_avail=gen.avail_height - 10)
    story.append(Spacer(1, 6 * mm))

    story.append(PageBreak())
    story.append(HeaderBar("File Index",
                           f"{len(gen.repo.files)} files",
                           fonts=gen.fonts, width=cw))
    story.append(Spacer(1, 3 * mm))

    fs = gen._cjk_style("FE", fontSize=7, fontName=gen.fonts.normal)
    header = [
        Paragraph("<b>#</b>", fs),
        Paragraph("<b>File Path</b>", fs),
        Paragraph("<b>Lang</b>", fs),
        Paragraph("<b>Lines</b>", fs),
        Paragraph("<b>Size</b>", fs),
        Paragraph("<b>Output</b>", fs),
    ]
    data = [header]
    for info in gen.repo.files:
        out_name = gen._file_out_name(info)
        data.append([
            Paragraph(str(info.index), fs),
            Paragraph(
                f'<font color="{COLORS["accent"].hexval()}">'
                f"{xml_escape(str(info.path))}</font>", fs),
            Paragraph(info.language, fs),
            Paragraph(f"{info.line_count:,}", fs),
            Paragraph(gen._fmt_size(info.size), fs),
            Paragraph(
                f'<font color="{COLORS["accent2"].hexval()}">'
                f"{xml_escape(out_name)}</font>", fs),
        ])
    cols = [cw * 0.06, cw * 0.38, cw * 0.12,
            cw * 0.12, cw * 0.12, cw * 0.20]
    file_table = Table(data, colWidths=cols, repeatRows=1)
    file_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLORS["header_bg"]),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.3, COLORS["border"]),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [white, HexColor("#f8f9fa")]),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 0), (4, -1), "RIGHT"),
    ]))
    story.append(file_table)

    return story


def build_file_story(gen, file_info: FileInfo) -> list:
    story = []
    cw = gen.content_width

    story.append(HeaderBar(
        str(file_info.path),
        f"{file_info.language} · {file_info.line_count:,} lines · "
        f"{gen._fmt_size(file_info.size)}",
        fonts=gen.fonts,
        width=cw,
    ))
    story.append(Spacer(1, 4 * mm))

    meta = gen._cjk_style("Meta", fontSize=8,
                           textColor=HexColor("#666666"))
    for item in [
        f"<b>Path:</b> {xml_escape(str(file_info.path))}",
        f"<b>Language:</b> {file_info.language}",
        f"<b>Lines:</b> {file_info.line_count:,}",
        f"<b>Size:</b> {gen._fmt_size(file_info.size)}",
    ]:
        story.append(Paragraph(item, meta))

    if gen.enable_semantic_minimap:
        if file_info.semantic_map.kind != "none":
            stats = (
                f"<b>Semantic Map:</b> {xml_escape(file_info.semantic_map.kind)} "
                f"({file_info.semantic_map.node_count} nodes / "
                f"{file_info.semantic_map.edge_count} edges)"
            )
            story.append(Paragraph(stats, meta))

    lint_counts = gen._lint_counts(file_info)
    if gen.enable_lint_heatmap:
        story.append(Paragraph(
            f"<b>Linter:</b> {lint_counts['high']} high / "
            f"{lint_counts['medium']} medium findings",
            meta,
        ))

    story.append(Spacer(1, 4 * mm))

    legend_budget = 0
    minimap_budget = 0
    minimap_spacer_budget = 0
    if gen.enable_semantic_minimap and file_info.semantic_map.kind != "none":
        minimap = SemanticMiniMap(file_info.semantic_map, fonts=gen.fonts, width=cw)
        _, minimap_h = minimap.wrap(cw, gen.avail_height)
        story.append(minimap)
        story.append(Spacer(1, 4 * mm))
        minimap_budget = minimap_h
        minimap_spacer_budget = 4 * mm

    if gen.enable_lint_heatmap and (lint_counts["high"] + lint_counts["medium"]) > 0:
        legend = LintLegend(fonts=gen.fonts, width=cw)
        _, legend_h = legend.wrap(cw, gen.avail_height)
        story.append(Spacer(1, 2 * mm))
        story.append(legend)
        story.append(Spacer(1, 2 * mm))
        legend_budget = legend_h + 4 * mm

    base_meta_lines = 4
    semantic_meta_lines = 1 if (gen.enable_semantic_minimap and file_info.semantic_map.kind != "none") else 0
    lint_meta_lines = 1 if gen.enable_lint_heatmap else 0

    header_budget = 28
    spacing_budget = 4 * mm + 4 * mm
    meta_lines_budget = (base_meta_lines + semantic_meta_lines + lint_meta_lines) * 14
    first_page_used = (
        header_budget
        + spacing_budget
        + meta_lines_budget
        + legend_budget
        + minimap_budget
        + minimap_spacer_budget
    )
    first_avail = gen.avail_height - first_page_used
    later_avail = gen.avail_height - 10

    line_heat = gen._line_heat_map(file_info) if gen.enable_lint_heatmap else {}
    if file_info.size >= gen.streaming_file_threshold:
        gen._add_code_chunks_streaming(
            story,
            file_info.abs_path,
            file_info.language,
            cw,
            first_avail=first_avail,
            later_avail=later_avail,
            line_heat=line_heat,
        )
    else:
        # Read once from disk without back-filling the lazy content cache; the
        # semantic step already released any cached content (P0-4).
        try:
            raw = file_info.abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = "(read failed)"
        all_lines = raw.split("\n")
        gen._add_code_chunks(story, all_lines, file_info.language, cw,
                             first_avail=first_avail,
                             later_avail=later_avail,
                             line_heat=line_heat)

    return story
