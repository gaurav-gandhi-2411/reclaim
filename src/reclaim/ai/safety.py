from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from reclaim.models import Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import GitRepoCache, build_record_for_path

# Reuses the SAME SafetyValidator the deterministic engine uses — protected system roots,
# git repositories, protected extensions, database/VM files, Docker/WSL roots, cloud-sync
# placeholders, and (once ADR-0009/0010's structural detection is in scope for this module)
# conda/venv environments. AI candidates get zero exemption or special-case treatment; a
# path this rejects is dropped before it can ever reach the AIReviewQueue.
#
# This module imports `reclaim.safety` (a pure, read-only pattern/stat validator) and
# `reclaim.scanner` (stat/git-repo detection helpers) — both read-only. It never imports
# `reclaim.executor`.


def filter_paths_through_safety_validator(
    paths: Iterable[Path], safety: SafetyValidator
) -> list[Path]:
    """Returns only the paths from `paths` that a fresh `SafetyValidator.evaluate()` call
    reports as `ELIGIBLE` — a BLOCKED or REVIEW_ONLY path is dropped silently from the AI
    candidate set (not surfaced as an AI suggestion at all), matching the deterministic
    engine's own "BLOCKED means excluded entirely" precedent (see
    `reclaim.detectors.generate_candidates`).

    Builds a fresh `FileRecord` per path (real current stat + git-repo state) rather than
    trusting caller-supplied metadata — the same "never trust a possibly-stale record"
    discipline `executor._reverify_direct_delete_candidates` already applies before a real
    delete, applied here before a path is even allowed into the review queue.
    """
    git_cache = GitRepoCache()
    eligible: list[Path] = []
    for path in paths:
        record = build_record_for_path(path, git_cache)
        if record is None:
            continue  # path no longer exists — nothing to propose
        result = safety.evaluate(record)
        if result.verdict == Verdict.ELIGIBLE:
            eligible.append(path)
    return eligible
