from __future__ import annotations

# reclaim.ai — the applied-AI layer (reclaim-ai-features-spec.md).
#
# HARD BOUNDARY, enforced structurally and re-verified every CI run
# (evals/test_ai_safety_gate.py): nothing under this package may ever import
# `reclaim.executor` (or `send2trash`) — the AI layer is recommend-only by construction,
# not by convention. AI output lands exclusively in `reclaim.ai.review_queue.AIReviewQueue`
# as `AICluster`/`AIClusterMember` objects (`reclaim.ai.models`), which are deliberately NOT
# `reclaim.models.Candidate` and share none of its fields — there is no code path, accidental
# or deliberate, by which an AI-produced object can reach `apply_batch`/`restore_batch`.
#
# This module itself imports nothing heavy — individual submodules guard their own optional
# dependencies (imagehash, opencv, torch, ...) via `reclaim.ai._optional.require`, raised
# lazily inside the functions that need them, never at import time. `import reclaim.ai` (and
# every submodule under it) must always succeed even when the `ai` extra isn't installed;
# only calling a feature that needs a specific dependency fails, with a clear, actionable
# "install reclaim[ai]" message instead of a raw ImportError.
