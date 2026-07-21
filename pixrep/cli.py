import argparse
import logging
import re
import sys
from pathlib import Path

from .file_utils import normalize_posix_path
from .models import RepoInfo
from .onepdf import pack_repo_to_one_pdf
from .pdf_generator import PDFGenerator
from .scanner import RepoScanner
from .version import __version__

log = logging.getLogger(__name__)


def _build_common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Path to code repository (default: .) / 仓库路径（默认当前目录）",
    )
    common.add_argument(
        "--max-size",
        type=int,
        default=512,
        metavar="KB",
        help="Max file size in KB (default: 512) / 单文件最大大小（KB）",
    )
    common.add_argument(
        "--ignore",
        nargs="*",
        default=[],
        metavar="PATTERN",
        help="Extra ignore patterns, e.g. '*.test.js' / 额外忽略规则",
    )
    common.add_argument(
        "--log-level",
        default="INFO",
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Logging level (default: INFO) / 日志级别",
    )
    return common


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="pixrep",
        description=(
            "Convert code repositories into hierarchical PDFs for LLM collaboration "
            "/ 将代码仓库转换为分层 PDF 以支持 LLM 协作"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  pixrep .                                # Backward-compatible, same as: pixrep generate .
  pixrep generate /path/to/repo -o ./pdfs
    pixrep generate . --format png          # Output as PNG long images
  pixrep list . --top-languages 10
  pixrep help generate
        """,
    )
    parser.add_argument("-V", "--version", action="version", version=f"pixrep {__version__}")

    common = _build_common_parser()
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    generate_parser = subparsers.add_parser(
        "generate",
        parents=[common],
        help="Generate index + file PDFs / 生成索引与文件 PDF",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    generate_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: ./pixrep_output/<repo>) / 输出目录",
    )
    generate_parser.add_argument(
        "--format",
        choices=["pdf", "png"],
        default="pdf",
        help="Output format: pdf or png (default: pdf) / 输出格式",
    )
    generate_parser.add_argument(
        "--png-dpi",
        type=int,
        default=150,
        metavar="DPI",
        help="DPI for PNG rendering (default: 150, only used with --format png) / PNG 渲染分辨率",
    )
    generate_parser.add_argument(
        "--max-total-pixels",
        type=int,
        default=120_000_000,
        metavar="N",
        help="Max pixels per PNG long image (default: 120000000); scales down when exceeded / 单张 PNG 长图最大像素",
    )
    generate_parser.add_argument(
        "--png-optimize",
        action="store_true",
        help="Enable Pillow PNG optimization (slower, smaller) / 开启 PNG 优化",
    )
    generate_parser.add_argument(
        "--png-split",
        action="store_true",
        help="Split oversized PNGs into multiple images / 超像素时输出多张分页 PNG",
    )
    generate_parser.add_argument(
        "--index-only",
        action="store_true",
        help="Generate only 00_INDEX.pdf / 仅生成索引 PDF",
    )
    generate_parser.add_argument(
        "--disable-semantic-minimap",
        action="store_true",
        help="Disable semantic UML/callgraph blocks / 关闭语义微缩图",
    )
    generate_parser.add_argument(
        "--disable-lint-heatmap",
        action="store_true",
        help="Disable linter severity background heatmap / 关闭 linter 热力图",
    )
    generate_parser.add_argument(
        "--linter-timeout",
        type=int,
        default=20,
        metavar="SECONDS",
        help="Timeout for each linter command (default: 20) / 单次 linter 超时",
    )
    generate_parser.add_argument(
        "--incremental",
        action="store_true",
        help="Skip files whose PDF is already up to date / 跳过已是最新 PDF 的文件",
    )
    generate_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Parallel worker threads for PDF generation (default: CPU count, max 8) / PDF 生成并行线程数",
    )
    generate_parser.add_argument(
        "--syntax",
        choices=["full", "none"],
        default="full",
        help="Syntax highlighting mode: full or none (default: full) / 语法高亮模式",
    )
    generate_parser.add_argument(
        "--fast",
        action="store_true",
        help="Shortcut for --syntax none (faster, smaller PDFs) / 等价 --syntax none",
    )
    generate_parser.add_argument(
        "--list-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    list_parser = subparsers.add_parser(
        "list",
        parents=[common],
        help="Print tree and language stats / 打印目录树和语言统计",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    list_parser.add_argument(
        "--top-languages",
        type=int,
        default=0,
        metavar="N",
        help="Show only top N languages by lines (0 = all) / 仅显示前 N 个语言",
    )

    help_parser = subparsers.add_parser(
        "help",
        help="Show help for commands / 查看命令帮助",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        choices=["generate", "list", "query", "onepdf", "allinone"],
        help="Command name / 命令名",
    )

    query_parser = subparsers.add_parser(
        "query",
        parents=[common],
        help="Search code and render matching snippets / 搜索代码并渲染匹配片段",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    query_parser.add_argument(
        "-q",
        "--query",
        required=True,
        help="Search pattern (regex or literal with --fixed) / 搜索模式",
    )
    query_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output file path (default: ./pixrep_output/<repo>/QUERY_<pattern>.pdf)",
    )
    query_parser.add_argument(
        "--format",
        choices=["pdf", "png"],
        default="pdf",
        help="Output format (default: pdf)",
    )
    query_parser.add_argument(
        "--png-dpi",
        type=int,
        default=150,
        help="DPI for PNG rendering",
    )
    query_parser.add_argument(
        "--fixed",
        action="store_true",
        help="Treat pattern as literal string / 将模式视为纯文本",
    )
    query_parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Case-sensitive search / 区分大小写搜索",
    )
    query_parser.add_argument(
        "--context",
        type=int,
        default=5,
        metavar="N",
        help="Context lines around each match (default: 5)",
    )
    query_parser.add_argument(
        "--max-results",
        type=int,
        default=200,
        help="Maximum match results from search engine (default: 200)",
    )
    query_parser.add_argument(
        "--max-snippet-lines",
        type=int,
        default=60,
        help="Maximum lines per snippet (default: 60)",
    )
    query_parser.add_argument(
        "--glob",
        nargs="*",
        default=[],
        metavar="PATTERN",
        help="File glob filters, e.g. '*.py' '*.js'",
    )
    query_parser.add_argument(
        "--type-filter",
        nargs="*",
        default=[],
        metavar="TYPE",
        help="ripgrep type filters, e.g. py js rust",
    )
    query_parser.add_argument(
        "--semantic",
        action="store_true",
        help="Use AST-based semantic symbol search (Python only)",
    )
    query_parser.add_argument(
        "--tui",
        action="store_true",
        help="Interactive terminal preview before rendering",
    )

    def _add_onepdf_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        p = subparsers.add_parser(
            name,
            parents=[common],
            help=help_text,
            description=(
                "Pack files into a single minimized PDF with CJK text support when a system font is available. "
                "/ 打包为单个极简 PDF；如果系统存在可用字体，则支持中文等 CJK 文本。"
            ),
            formatter_class=argparse.RawTextHelpFormatter,
        )
        p.add_argument(
            "-o",
            "--output",
            default=None,
            help="Output PDF path (default: ./pixrep_output/<repo>/ONEPDF_CORE.pdf) / 输出 PDF 路径",
        )
        p.add_argument(
            "--no-core-only",
            action="store_true",
            help="Include non-core files (docs/tests) / 也包含非核心文件（文档/测试）",
        )
        p.add_argument(
            "--no-git",
            action="store_true",
            help="Do not prefer git ls-files (fallback to walking) / 不优先使用 git ls-files",
        )
        p.add_argument(
            "--include",
            nargs="*",
            default=[],
            metavar="PATTERN",
            help="Only include paths matching these patterns / 仅包含匹配这些规则的路径",
        )
        p.add_argument(
            "--cols",
            type=int,
            default=120,
            metavar="N",
            help="Max columns per line for wrapping (default: 120) / 自动换行列数",
        )
        p.add_argument(
            "--tab-size",
            type=int,
            default=2,
            metavar="N",
            help="Tab stop width for expansion (default: 2) / Tab 展开宽度",
        )
        p.add_argument(
            "--no-wrap",
            action="store_true",
            help="Disable line wrapping / 禁用自动换行",
        )
        p.add_argument(
            "--no-tree",
            action="store_true",
            help="Do not include directory tree section / 不包含目录树",
        )
        p.add_argument(
            "--no-index",
            action="store_true",
            help="Do not include file index section / 不包含文件索引",
        )
        return p

    onepdf_parser = _add_onepdf_parser(
        "onepdf",
        "Pack core code into a single minimized PDF / 核心代码一键打包成单个极简 PDF",
    )
    allinone_parser = _add_onepdf_parser(
        "allinone",
        "Alias of onepdf / onepdf 的别名",
    )

    commands = {
        "generate": generate_parser,
        "list": list_parser,
        "query": query_parser,
        "help": help_parser,
        "onepdf": onepdf_parser,
        "allinone": allinone_parser,
    }
    return parser, commands


def _normalize_legacy_args(argv: list[str]) -> list[str]:
    if not argv:
        return ["generate"]

    known_commands = {"generate", "list", "query", "help", "onepdf", "allinone"}
    first = argv[0]
    if first in known_commands:
        return argv

    if first in {"-h", "--help", "-V", "--version"}:
        return argv

    if "--list-only" in argv:
        filtered = [arg for arg in argv if arg != "--list-only"]
        return ["list", *filtered]

    if first.startswith("-"):
        return ["generate", *argv]

    return ["generate", *argv]


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
    )
    # Keep reportlab quiet unless explicitly debugging.
    logging.getLogger("reportlab").setLevel(logging.WARNING)


def _scan_repo(
    args: argparse.Namespace, include_content: bool = True
) -> tuple[RepoInfo | None, int]:
    repo_path = Path(args.repo).resolve()
    if not repo_path.is_dir():
        log.error("Error: '%s' is not a directory", args.repo)
        return None, 1

    log.info("Scanning %s...", repo_path)
    scanner = RepoScanner(
        str(repo_path),
        max_file_size=args.max_size * 1024,
        extra_ignore=args.ignore,
    )
    repo = scanner.scan(include_content=include_content)
    if not repo.files:
        log.info("No files found.")
        return repo, 0
    stats = repo.scan_stats
    log.info(
        "Scan summary: seen=%d, loaded=%d, ignored=%d, size/empty=%d, binary=%d, errors=%d",
        stats.get("seen_files", 0),
        len(repo.files),
        stats.get("ignored_by_pattern", 0),
        stats.get("skipped_size_or_empty", 0),
        stats.get("skipped_binary", 0),
        stats.get("skipped_unreadable", 0),
    )
    return repo, 0


def _print_repo_list(repo, top_languages: int = 0):
    log.info("")
    log.info("%s (%d files)", repo.name, len(repo.files))
    log.info("")
    log.info("%s", repo.tree_str)
    log.info("")
    log.info("%-15s %6s %8s", "Language", "Files", "Lines")
    log.info("%s", "-" * 32)

    items = list(repo.language_stats.items())
    if top_languages and top_languages > 0:
        items = items[:top_languages]

    for lang, stats in items:
        log.info("%-15s %6d %8d", lang, stats["files"], stats["lines"])

    log.info("%s", "-" * 32)
    shown_lines = sum(stats["lines"] for _, stats in items)
    if top_languages and top_languages > 0 and len(items) < len(repo.language_stats):
        log.info("%-15s %6s %8d", "Shown", "", shown_lines)
        log.info("%-15s %6d %8d", "Total", len(repo.files), repo.total_lines)
    else:
        log.info("%-15s %6d %8d", "Total", len(repo.files), repo.total_lines)


def _run_generate(args: argparse.Namespace) -> int:
    include_content = False
    repo, code = _scan_repo(args, include_content=include_content)
    if code != 0 or repo is None or not repo.files:
        return code

    if args.list_only:
        _print_repo_list(repo)
        return 0

    output_dir = args.output or f"./pixrep_output/{repo.name}"
    generator = PDFGenerator(
        repo,
        output_dir,
        enable_semantic_minimap=not args.disable_semantic_minimap,
        enable_lint_heatmap=not args.disable_lint_heatmap,
        linter_timeout=args.linter_timeout,
        incremental=args.incremental,
        max_workers=args.workers,
        output_format=args.format,
        png_dpi=args.png_dpi,
        max_total_pixels=args.max_total_pixels,
        png_optimize=args.png_optimize,
        png_split=args.png_split,
        syntax_mode=("none" if args.fast else args.syntax),
    )
    if args.index_only:
        generator.generate_index_only()
        log.info("")
        log.info("Done! Generated 1 %s", args.format.upper())
        return 0

    generator.generate_all()
    return 0


def _run_list(args: argparse.Namespace) -> int:
    repo, code = _scan_repo(args, include_content=False)
    if code != 0 or repo is None or not repo.files:
        return code
    _print_repo_list(repo, top_languages=args.top_languages)
    return 0


def _run_onepdf(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).resolve()
    if not repo_path.is_dir():
        log.error("Error: '%s' is not a directory", args.repo)
        return 1

    out_pdf = args.output
    if not out_pdf:
        out_pdf = f"./pixrep_output/{repo_path.name}/ONEPDF_CORE.pdf"

    stats = pack_repo_to_one_pdf(
        repo_root=repo_path,
        out_pdf=Path(out_pdf),
        max_file_size=args.max_size * 1024,
        extra_ignore=args.ignore,
        core_only=not args.no_core_only,
        prefer_git=not args.no_git,
        include_patterns=args.include,
        max_cols=args.cols,
        wrap=not args.no_wrap,
        tab_size=args.tab_size,
        include_tree=not args.no_tree,
        include_index=not args.no_index,
    )
    log.info(
        "onepdf summary: seen=%d, included=%d, ignored=%d, size/empty=%d, binary=%d, errors=%d, pages=%d, output=%d bytes",
        stats.get("seen_files", 0),
        stats.get("included", 0),
        stats.get("ignored_by_pattern", 0),
        stats.get("skipped_size_or_empty", 0),
        stats.get("skipped_binary", 0),
        stats.get("skipped_unreadable", 0),
        stats.get("pages", 0),
        stats.get("output_bytes", 0),
    )
    log.info("PDF: %s", Path(out_pdf).resolve())
    return 0


def _run_query(args: argparse.Namespace) -> int:
    from .query import ContextExtractor, RipgrepSearcher, SemanticSearcher
    from .query_renderer import QueryResultRenderer
    from .query_tui import QueryPreviewTUI

    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        log.error("Error: '%s' is not a directory", args.repo)
        return 1

    # Semantic search needs the full Python file set to build its symbol
    # index, so it still scans the repo. Text search goes ripgrep-first and
    # only scans the hit files afterwards (P0-2).
    if args.semantic:
        repo, code = _scan_repo(args, include_content=False)
        if code != 0 or repo is None or not repo.files:
            return code
        semantic_searcher = SemanticSearcher(repo=repo, max_results=args.max_results)
        matches = semantic_searcher.search(
            args.query,
            fixed_strings=args.fixed,
            case_sensitive=args.case_sensitive,
            file_globs=args.glob or None,
        )
        if not matches:
            log.info("No matches found for: %s", args.query)
            return 0
        scanned_paths = {normalize_posix_path(info.path) for info in repo.files}
        matches = [m for m in matches if m.rel_path in scanned_paths]
        repo_name = repo.name
    else:
        searcher = RipgrepSearcher(
            repo_root=repo_root,
            max_results=args.max_results,
        )
        if not searcher.available:
            log.warning(
                "ripgrep (rg) is not installed. Install it for best performance. "
                "Falling back to basic Python search..."
            )
        matches = searcher.search(
            args.query,
            file_globs=args.glob or None,
            type_filters=args.type_filter or None,
            fixed_strings=args.fixed,
            case_sensitive=args.case_sensitive,
        )
        if not matches:
            log.info("No matches found for: %s", args.query)
            return 0
        # Scan only the hit files: applies pixrep ignore rules + line counts
        # without walking the whole repo.
        hit_rel = sorted({m.rel_path for m in matches})
        scanner = RepoScanner(
            str(repo_root),
            max_file_size=args.max_size * 1024,
            extra_ignore=args.ignore,
        )
        hit_files = scanner.scan_files(
            [repo_root / rel for rel in hit_rel],
            include_content=False,
        )
        scanned_rel = {normalize_posix_path(f.path) for f in hit_files}
        matches = [m for m in matches if m.rel_path in scanned_rel]
        if not matches:
            log.info(
                "Matches were found only in files excluded by scanner. "
                "Try adjusting --ignore/--glob options."
            )
            return 0
        repo = RepoInfo(root=repo_root, name=repo_root.name, files=hit_files)
        repo.total_lines = sum(f.line_count for f in hit_files)
        repo_name = repo_root.name

    log.info("Found %d matches", len(matches))

    extractor = ContextExtractor(
        repo=repo,
        context_lines=args.context,
        max_snippet_lines=args.max_snippet_lines,
    )
    snippets = extractor.extract(matches)
    if not snippets:
        log.info("No snippets could be extracted for: %s", args.query)
        return 0

    if args.tui:
        if not sys.stdin.isatty():
            log.warning("--tui requested but stdin is not interactive; continuing without preview")
        else:
            tui = QueryPreviewTUI(snippets=snippets, query=args.query)
            tui_result = tui.run()
            if not tui_result.should_render:
                log.info("Render canceled in TUI preview")
                return 0
            snippets = [snippets[i] for i in tui_result.selected_indices]
            if not snippets:
                log.info("No snippets selected; skipped rendering")
                return 0

    if args.output:
        out_path = Path(args.output)
    else:
        safe_q = args.query[:30].replace("/", "_").replace("\\", "_")
        safe_q = re.sub(r"[^\w\-_.]", "_", safe_q)
        out_path = Path(f"./pixrep_output/{repo_name}/QUERY_{safe_q}.{args.format}")

    renderer = QueryResultRenderer(
        repo=repo,
        query=args.query,
        snippets=snippets,
        output_path=out_path,
        output_format=args.format,
        png_dpi=args.png_dpi,
    )
    renderer.render()
    log.info("Query result: %s", out_path.resolve())
    return 0


def _run_help(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    commands: dict[str, argparse.ArgumentParser],
) -> int:
    if args.topic:
        commands[args.topic].print_help()
    else:
        parser.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else argv
    parser, commands = build_parser()

    # Better error for `pixrep some_word` where `some_word` isn't a path.
    known_commands = {"generate", "list", "query", "help", "onepdf", "allinone"}
    if raw_args:
        first = raw_args[0]
        has_windows_drive = bool(re.match(r"^[A-Za-z]:[\\/]", first))
        has_uri_scheme = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", first))
        looks_like_path = (
            first.startswith(".")
            or ("/" in first)
            or ("\\" in first)
            or has_windows_drive
            or has_uri_scheme
        )
        if (first not in known_commands) and (not first.startswith("-")):
            if looks_like_path and not Path(first).exists():
                sys.stderr.write(f"Error: '{first}' does not exist.\n\n")
                parser.print_help()
                return 2
            if (not looks_like_path) and not Path(first).exists():
                sys.stderr.write(f"Error: unknown command or path '{first}'.\n\n")
                parser.print_help()
                return 2

    args = parser.parse_args(_normalize_legacy_args(raw_args))
    _configure_logging(getattr(args, "log_level", "INFO"))

    if args.command == "list":
        return _run_list(args)
    if args.command == "query":
        return _run_query(args)
    if args.command in {"onepdf", "allinone"}:
        return _run_onepdf(args)
    if args.command == "help":
        return _run_help(args, parser, commands)
    return _run_generate(args)


if __name__ == "__main__":
    sys.exit(main())
