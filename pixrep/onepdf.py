"""
Backwards-compatible facade for the ONEPDF_CORE feature.

The implementation lives in smaller modules to keep responsibilities clear.
"""

from .onepdf_pack import (
    DEFAULT_CORE_IGNORE_PATTERNS,
    PackedFile,
    collect_core_files,
    pack_repo_to_one_pdf,
)

__all__ = [
    "DEFAULT_CORE_IGNORE_PATTERNS",
    "PackedFile",
    "collect_core_files",
    "pack_repo_to_one_pdf",
]
