# 0037. Every reader of the candidate cache checks warm status; a cold or stale read is a typed 409

## Context

`_cached_all_candidates` (detectors + duplicate hashing, minutes on a large index) is read by
four request paths: `GET /api/summary`, `/api/treemap`, `/api/candidates` and
`/api/clean/one-click-summary` (plus the apply-selection paths, which already refused a cold
cache with `CandidatesNotWarmError`). Only the Overview screen ran the warm-status check
(`ensureCandidatesWarm`). After a mode switch, category toggle, new scan or scope change (the
cache key components, `candidates_cache_stale_reason`) the other screens either blocked the
request thread for the whole recompute or showed data from a different cache generation than
Overview.

## Decision

Backend: the four readers call `require_warm_candidates(state)` right before reading the cache
(`require_warm=True` by default). A cold or stale cache raises `CandidatesNotWarmError`, which the
routes turn into `409 {"detail", "code": "candidates_not_warm", "stale_reason"}` and start the same
single-flight courtesy warm-up `POST /api/apply` already started (shared `_not_warm_response`).
`stale_reason` is `cold | scan | mode | config | scope | computing`. The check never runs a detector.
This extends, and does not remove, any response: a warm cache answers exactly as before, and
`has_scan=false` (empty index) is still a 200. The MCP `list_candidates` tool passes
`require_warm=False` and keeps its blocking behaviour (it has no polling loop).

Frontend: one helper, `readCandidateCache(stateEl, path)`, used by Overview, Quick Clean, treemap,
review queue and SIMPLE results: `ensureCandidatesWarm` (one re-warm per stale detection, stale
cause shown as `textContent`), then the read; a 409 in the gap between check and read gets exactly
one retry after waiting for the warm-up, then the view's normal error state. The Quick Clean confirm
dialog reloads instead of opening on a list fetched before a stale event; a mode switch also
refreshes the SIMPLE results screen.

## Consequences

- Clients that read these four endpoints with a cold cache now see 409 + a started warm-up where
  they previously got a (slow) 200; they must poll `GET /api/candidates/warm-status` and retry.
  The dashboard does. Existing Python tests use a retrying `WarmingTestClient`.
- `GET /api/duplicate-clusters/review` does not read this cache (it computes its own clusters,
  uncached) and is unchanged; it can still be slow on a large index.
- A category toggle changes the key only; the Settings view itself does not read the cache, so the
  other views re-warm when they are next activated.

## Alternatives

- Warm in the request on demand (the old behaviour): blocks the request thread for minutes.
- Trigger the warm and return 202 with a body: breaks the response schema of four endpoints.
- Warm at the end of every scan inside `run_scan`: delays "scan complete" by the compute time.
