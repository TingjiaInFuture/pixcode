<div align="center">

# <img src="https://github.com/user-attachments/assets/2d378b5c-bfd9-4105-a8c2-ce67f8d542fa" width="60" alt="pixrep logo" style="vertical-align: middle;" /> pixrep


# 📉 SAVE TOKENS WITH VISUAL CONTEXT

### Turn Codebases into **Visual Context** for Multimodal LLMs


[![PyPI version](https://img.shields.io/pypi/v/pixrep?color=blue)](https://pypi.org/project/pixrep/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub Stars](https://img.shields.io/github/stars/TingjiaInFuture/pixrep?style=social)](https://github.com/TingjiaInFuture/pixrep)

</div>

---

## 📖 Introduction

**pixrep** is a developer tool designed to bridge the gap between large code repositories and Multimodal Large Language Models.

Instead of feeding raw text that consumes massive context windows, **pixrep** converts your repository into a **structured, hierarchical set of PDFs**. This allows you to:

*   **Save Tokens:** Visual encoding is far more efficient than text tokenization (~40% fewer tokens in our demonstrated codebase test; results vary by model, repository and rendering settings).
*   **Test for Free:** Easily share your entire codebase with premium models (like **Claude Opus 4.6**) on platforms like **arena.ai** without hitting text limits.


## 🚀 Why Visual Code?

Traditional text tokenization is expensive. Visual encoding compresses structure efficiently.

*Comparison in Google AI Studio (Gemini 3 Pro):*

<table>
  <tr>
    <th width="50%">Raw Files (Text Input)</th>
    <th width="50%">pixrep OnePDF (Visual Input)</th>
  </tr>
  <tr>
    <td><img src="https://github.com/user-attachments/assets/44dc5c5f-5913-4eb6-b20c-d020cfc57fe1" width="100%" alt="Raw Files Usage"></td>
    <td><img src="https://github.com/user-attachments/assets/822ae56b-e9d3-4c2c-847f-21bd5341971c" width="100%" alt="OnePDF Usage"></td>
  </tr>
  <tr>
    <td align="center"><b>31,812 Tokens</b> ❌<br><i>(Cluttered context)</i></td>
    <td align="center"><b>19,041 Tokens</b> ✅<br><i>(Clean, single file)</i></td>
  </tr>
</table>

## 🎓 Academic Backing 

The core philosophy of **pixrep** (rendering code → PDF with syntax highlighting + heatmaps) has been validated by top-tier papers from 2025–2026:

*   **Text or Pixels? It Takes Half** (arXiv:2510.18279): Rendering text as images saves **~50% decoder tokens** while maintaining or improving performance.
*   **DeepSeek-OCR** (arXiv:2510.18234): Visual encoding achieves **10–20× compression ratios** for dense, structured text.
*   **CodeOCR** (arXiv:2602.01785, Feb 2026): A **code-specific** study showing that visual input with syntax highlighting improves performance even at **4× compression**. In tasks like clone detection, the visual approach outperforms plain text.

**Verdict:** In the multimodal era, the optimal way to feed code is via **"visual perception" rather than "text reading."**

## ✨ Features

*   **📉 High Efficiency:** Drastically reduces context window usage for large repos.
*   **⚡ Faster Scanning:** Single-pass file loading (binary check + line count + optional content decode) to reduce I/O overhead.
*   **🎨 Syntax Highlighting:** Supports 50+ languages (Python, JS, Rust, Go, C++, etc.) with a "One Dark" inspired theme.
*   **🧠 Semantic Minimap:** Auto-generates per-file micro UML / call graph summaries to expose structure at a glance.
*   **🔥 Linter Heatmap:** Integrates `ruff` / `eslint` findings and marks risky lines with red/yellow visual overlays.
*   **🔎 Query Mode:** Search by text or semantic symbols, then render only matched snippets to PDF/PNG.
*   **🗂️ Hierarchical Output:** Generates a clean `00_INDEX.pdf` summary and separate files for granular access.
*   **🌏 CJK Support:** Built-in font fallback for Chinese/Japanese/Korean characters (Auto-detects OS fonts).
*   **🛡️ Smart Filtering:** In Git repositories, processes only tracked files by default; in plain directories it applies built-in ignore rules plus any `--ignore` globs. (`.gitignore` itself is not parsed; use `--ignore` to add custom rules.)
*   **📊 Insightful Stats:** Calculates line counts and language distribution automatically.
*   **🧾 Scan Diagnostics:** Prints scan summary (`seen/loaded/ignored/binary/errors`) for faster troubleshooting.

## 📦 Installation

```bash
pip install pixrep
```

For PNG output support (`--format png`), install optional extras:

```bash
pip install "pixrep[png]"
```

## 🛠️ Usage

### Quick Start
Convert the current directory to hierarchial PDFs in `./pixrep_output/<repo_name>`:

```bash
pixrep .
```

**Or pack everything into a single, token-optimized PDF (Recommended for LLMs):**

```bash
pixrep onepdf .
```

### Common Commands

**Generate PDFs for a specific repo:**
```bash
pixrep generate /path/to/my-project -o ./my-project-pdfs
```

**Pack core code into a single minimized PDF (all-in-one):**
```bash
pixrep onepdf /path/to/my-project -o ./ONEPDF_CORE.pdf
```
Notes:
* Defaults to `git ls-files` (tracked files) when the target is inside a Git repository (including a subdirectory of a larger monorepo).
* Defaults to "core-only" filtering (skips docs/tests, **except the root `README.md`**); use `--no-core-only` to include them.

**Preview structure and stats (without generating PDFs):**
```bash
pixrep list /path/to/my-project
```
`list` mode now uses lightweight scanning (no file content decode), so large repos respond significantly faster.

**Show only top 5 languages in the summary:**
```bash
pixrep list . --top-languages 5
```

**Query and render only matching snippets:**
```bash
pixrep query . -q "cache" --glob "*.py" --format png
```

**Semantic query (Python symbols) with interactive terminal preview:**
```bash
pixrep query . -q "CodeInsight" --semantic --tui
```

### CLI Reference

| Argument | Description | Default |
| :--- | :--- | :--- |
| `repo` | Path to the code repository. | `.` (Current Dir) |
| `-o`, `--output` | Directory to save the generated PDFs. | `./pixrep_output/<repo>` |
| `--max-size` | Max file size to process (in KB). Files larger than this are skipped. | `512` KB |
| `--ignore` | Additional glob patterns to ignore (e.g., `*.json` `test/*`). | `[]` |
| `--index-only` | Generate only the `00_INDEX.pdf` (Directory tree & stats). | `False` |
| `--disable-semantic-minimap` | Turn off per-file semantic UML/callgraph panel. | `False` |
| `--disable-lint-heatmap` | Turn off linter-based line heatmap background. | `False` |
| `--linter-timeout` | Timeout seconds for each linter command. | `20` |
| `--list-only` | Print the directory tree and stats to console, then exit. | `False` |
| `-V`, `--version` | Show version information. | - |

## ⚙️ Performance Notes

`pixrep` now applies two execution paths:

1. **Light scan path** (`pixrep list`, `pixrep generate --index-only`, `--list-only`):
   only metadata and line counts are collected; file content is not loaded.
2. **Full scan path** (regular `pixrep generate`):
   file content is decoded only when needed for PDF rendering.

This reduces memory pressure and disk I/O for repository exploration workflows.

Lint/semantic caches are stored in user cache directories by default, namespaced per-repo as `<repo_name>-<resolved_path_sha256[:12]>` so two repos that share a basename never collide:

* Windows: `%LOCALAPPDATA%/pixrep/cache/<repo_name>-<hash>`
* Linux/macOS: `$XDG_CACHE_HOME/pixrep/<repo_name>-<hash>` or `~/.cache/pixrep/<repo_name>-<hash>`

Override the cache root with `PIXREP_CACHE_DIR`.

## 📂 Output Structure

After running `pixrep .`, you will get a folder structure optimized for LLM upload:

```text
pixrep_output/pixrep/
├── 00_INDEX.pdf                  # <--- Upload this first! Contains tree & stats
├── LICENSE__a1b2c3d4.pdf
├── README.md__5e6f7a8b.pdf
├── pixrep___init__.py__9a0b1c2d.pdf
├── pixrep_cli.py__3f4e5d6c.pdf
└── ...                           # names follow <safe_path>__<short_hash>.pdf
```

## 🧩 Supported Languages
pixrep automatically detects and highlights syntax for:
*   **Core:** Python, C, C++, Java, Rust, Go
*   **Web:** HTML, CSS, JavaScript, TypeScript, Vue, Svelte
*   **Config:** JSON, YAML, TOML, XML, Dockerfile, Ini
*   **Scripting:** Bash, Lua, Perl, Ruby, PHP
*   **And more:** Swift, Kotlin, Scala, Haskell, OCaml, etc.

## 🤝 Contributing

We welcome contributions! Please feel free to submit a Pull Request.

1.  Fork the repository.
2.  Create your feature branch (`git checkout -b feature/AmazingFeature`).
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4.  Push to the branch (`git push origin feature/AmazingFeature`).
5.  Open a Pull Request.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
