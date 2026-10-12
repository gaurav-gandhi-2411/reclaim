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
- ~~`GET /api/duplicate-clusters/review` does not read this cache~~ -- superseded, see the
  addendum "review clusters" below.
- A category toggle changes the key only; the Settings view itself does not read the cache, so the
  other views re-warm when they are next activated.

## Alternatives

- Warm in the request on demand (the old behaviour): blocks the request thread for minutes.
- Trigger the warm and return 202 with a body: breaks the response schema of four endpoints.
- Warm at the end of every scan inside `run_scan`: delays "scan complete" by the compute time.

## Addendum 2026-10-08: review clusters

`GET /api/duplicate-clusters/review` was the one candidate-cache reader this ADR missed: it ran
`find_duplicate_clusters` and `generate_duplicate_candidates` itself on every request (an uncached
whole-index BLAKE3 pass on its own connection, in the request thread; about 30 minutes for the 1.6
million candidate files of the owner's real index) and the Review Queue tab calls it on load.
Incident, owner's installed build 2026-10-08: opening the Review Queue while the background warm-up
was computing started a second concurrent dedup pass (three `dedup.start` lines about 29 minutes
apart for two warm-ups); both wrote hashes into the same index and the warm-up failed after 2,051 s
with `database table is locked` (`ScanIndex.close()`'s checkpoint raised after the failed write and
masked the real `database is locked`). Now the warm pass computes the clusters once, stores them in
`AppState.candidates_clusters_cache` next to `candidates_cache` (same key, same lock, assigned only
after a complete compute), and the endpoint answers the typed 409 `candidates_not_warm` (with the
courtesy warm-up) until then, and builds its rows from the cached clusters and candidates without
hashing anything. Visible difference: the rows now honour the same scope filter as every other
view (the cached candidates are scope-filtered). `run_candidates_warm` also gets a 60 s busy
timeout, bounded retry of hash flushes on `database is locked`, a non-masking `close()` and a
logged traceback.

Other private-pass readers (same addendum): `GET /api/ai/category-explanation/{group}` now reads
the warm cache (typed 409). The MCP selector (`select_candidates_for_selector`) goes through
`_cached_all_candidates` instead, serializing on `candidates_cache_lock` and reusing an in-flight
warm-up's result, because a scoped MCP scan never auto-warms and a refusal would make
`preview_apply` unusable. Two consequences, stated plainly: (1) a user cancel of the warm-up does
not stop a selector already blocked on that lock (when the warm-up ends cancelled, the selector
computes the cache itself); (2) the MCP `selection_hash` is now derived from the cache, so it can no
longer notice an on-disk change since the scan -- `delete` therefore re-stats each selected file
(`service.stale_selected_candidates`: identity, size, mtime, same comparison as the executor's
pre-flight) and refuses with `SelectionMismatchError` before executing anything.
