"""Lightweight interactive preview for query snippets."""

from __future__ import annotations

from dataclasses import dataclass

from .query import CodeSnippet


@dataclass
class TUIResult:
    selected_indices: list[int]
    should_render: bool


class QueryPreviewTUI:
    """Simple stdin/stdout based interactive preview."""

    def __init__(self, snippets: list[CodeSnippet], query: str):
        self.snippets = snippets
        self.query = query
        self._selected = set(range(len(snippets)))

    def run(self) -> TUIResult:
        if not self.snippets:
            return TUIResult(selected_indices=[], should_render=False)

        print(f"\n[query] {self.query}")
        print(f"[snippets] {len(self.snippets)}")
        self._print_list()
        print(
            "\nCommands: p <idx>=preview, t <idx>=toggle, a=all, n=none, l=list, r=render, q=quit"
        )

        while True:
            try:
                raw = input("query-tui> ").strip()
            except EOFError:
                return TUIResult(selected_indices=sorted(self._selected), should_render=False)
            if not raw:
                continue

            if raw == "q":
                return TUIResult(selected_indices=sorted(self._selected), should_render=False)
            if raw == "r":
                return TUIResult(selected_indices=sorted(self._selected), should_render=True)
            if raw == "a":
                self._selected = set(range(len(self.snippets)))
                print("Selected all snippets.")
                continue
            if raw == "n":
                self._selected.clear()
                print("Cleared all selections.")
                continue
            if raw == "l":
                self._print_list()
                continue

            parts = raw.split(maxsplit=1)
            if len(parts) != 2:
                print("Invalid command.")
                continue

            cmd, idx_raw = parts
            try:
                idx = int(idx_raw)
            except ValueError:
                print("Index must be an integer.")
                continue
            if idx < 1 or idx > len(self.snippets):
                print("Index out of range.")
                continue

            pos = idx - 1
            if cmd == "t":
                if pos in self._selected:
                    self._selected.remove(pos)
                    print(f"Deselected #{idx}.")
                else:
                    self._selected.add(pos)
                    print(f"Selected #{idx}.")
            elif cmd == "p":
                self._print_preview(pos)
            else:
                print("Unknown command.")

    def _print_list(self) -> None:
        for idx, snip in enumerate(self.snippets, 1):
            mark = "*" if (idx - 1) in self._selected else " "
            print(
                f"[{mark}] {idx:03d} {snip.rel_path} "
                f"({snip.language} {snip.start_line}-{snip.end_line}, matches={len(snip.match_lines)})"
            )

    def _print_preview(self, index: int) -> None:
        snip = self.snippets[index]
        match_set = set(snip.match_lines)
        print(f"\n--- [{index + 1}] {snip.rel_path}:{snip.start_line}-{snip.end_line} ---")
        for offset, line in enumerate(snip.lines):
            line_no = snip.start_line + offset
            marker = ">>" if line_no in match_set else "  "
            print(f"{marker} {line_no:4d} | {line}")
        print("--- end ---\n")
