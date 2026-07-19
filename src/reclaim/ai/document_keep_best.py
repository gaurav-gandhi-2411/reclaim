from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

# Feature 1b's keep-best equivalent for plain near-dup document clusters (not version chains,
# which use version_chain.py's ordering instead). Documents have no sharpness/resolution
# signal the way images do — the classical, transparent, directional heuristic here is
# "larger + more recently modified wins": a bigger file is more likely to be the complete,
# unedited-down version, and a more recent mtime is more likely to be the final save. Neither
# signal is a calibrated quality score (spec §0.6) — this is a raw tiebreak rule, documented
# as exactly that.


def select_document_keep(paths: Sequence[Path]) -> Path:
    if not paths:
        raise ValueError("cannot select a keeper from an empty path list")
    return max(paths, key=lambda p: (p.stat().st_size, p.stat().st_mtime))
