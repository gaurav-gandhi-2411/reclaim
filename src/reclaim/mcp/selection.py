from __future__ import annotations

import json
from collections.abc import Sequence

import blake3

# Pure, self-contained: zero imports from `reclaim.executor`, `send2trash`, or
# `reclaim.api.service` — see `reclaim.mcp`'s module docstring for why that separation matters.
# `compute_selection_hash` is the one commitment function both `preview_apply` and `delete`
# call, so there is exactly one place either side of that round trip can drift.


def compute_selection_hash(
    *, scan_id: str, tier: str, rule_id_or_category: str, paths: Sequence[str]
) -> str:
    """Server-computed commitment over the exact `(scan_id, sorted candidate paths, tier,
    rule_id_or_category)` tuple a `preview_apply` call resolved. `delete` re-derives the SAME
    tuple fresh at call time (a brand-new `select_candidates_for_selector` call, never the
    `preview_apply` result cached anywhere) and recomputes this hash; any mismatch — a stale
    scan, a race with a manual apply that changed the underlying candidate set, or a tampered
    hash — is refused via `SelectionMismatchError`/`StaleScanError`, never silently executed
    against a different selection than the one previewed.

    Canonicalized as a JSON object with sorted keys over an explicitly `sorted()` path list, so
    the result is stable regardless of dict/set/candidate-list iteration order on either side of
    the round trip — the whole point of a commitment hash is that two independently-computed
    calls over the same logical selection must agree byte-for-byte.

    BLAKE3 (not a general hashlib digest): `reclaim.dedup` already uses it for content hashing
    elsewhere in this codebase (one hashing dependency, not a second), and it's already a
    pinned, always-installed dependency (`pyproject.toml`) — no new dependency for this.
    """
    payload = json.dumps(
        {
            "scan_id": scan_id,
            "tier": tier,
            "rule_id_or_category": rule_id_or_category,
            "paths": sorted(paths),
        },
        sort_keys=True,
    ).encode("utf-8")
    return blake3.blake3(payload).hexdigest()


class StaleScanError(RuntimeError):
    """Raised by `reclaim.mcp.server` when a caller-supplied `scan_id` no longer names the scan
    generation the live index reflects — a newer scan has completed since `preview_apply` (or
    even since a caller merely reads it) was called. Covers "genuinely unknown id" and "real but
    superseded id" identically: both are refused, never guessed at."""


class SelectionMismatchError(RuntimeError):
    """Raised by `reclaim.mcp.server` when a `delete` call's `selection_hash` does not match a
    fresh recomputation of `compute_selection_hash` over the SAME `(scan_id, tier,
    rule_id_or_category)` triple. A live scan_id (see `StaleScanError` for a dead one) but a
    mismatched hash means the underlying candidate set changed since `preview_apply` ran (a
    manual apply/restore, a background purge, or files changing on disk) or the hash itself was
    tampered with — either way, this is a hard refusal: `delete` executes nothing when this is
    raised, never a partial or best-effort selection."""


class ConcurrentDeleteError(RuntimeError):
    """Raised by `reclaim.mcp.server.delete` when another `delete` call is already in-flight on
    this process (`AppState.mcp_delete_in_progress`, checked-and-set under `AppState.lock` --
    same idiom `POST /api/apply` already uses for `apply_status`). Without this guard, two
    concurrent `delete` calls for the identical selection can both pass hash validation and both
    reach `apply_batch`: the scan index doesn't reflect the first call's file move, so the
    second's fresh re-derivation still matches the same `selection_hash` -- it then fails at the
    filesystem level (the path is already gone) with `files_succeeded=0`, but the tool call
    itself still returns `isError=False`, an easy-to-miss "the call succeeded" shape on a call
    that deleted nothing. This error refuses the SECOND call immediately, before it ever
    re-derives candidates or computes a hash -- an unambiguous, impossible-to-miss refusal
    instead of a technically-honest-but-easy-to-misread empty success."""
