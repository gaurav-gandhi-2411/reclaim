# ADR-0040: Background, cancellable, low-priority dedup warm-up after a full scan

Status: Accepted (2026-10-05). Owner decision: "Dedup warm-up runs in the background AFTER a full
scan, not during it."

## Context

Every candidate-cache reader (summary, treemap, candidates, one-click, blanket apply) needs the
shared candidate list (`service._cached_all_candidates`), whose expensive part is the duplicate
hash pass (`dedup.find_duplicate_clusters`). ADR-0037 made a cold/stale cache a typed 409
`candidates_not_warm` plus a courtesy warm-up, so the user waits visibly instead of hanging the
server -- but they still wait. The estimated cost on the owner's real 5.86M-row index is about an
hour (ESTIMATE, not measured by this change). A user who scans and then opens the dashboard hits
that wait on first open.

Facts read from the code before deciding:

- Hashes were already durable per batch (ADR-0038): `find_duplicate_clusters` flushes pending
  partial/full hashes to the index after every window of up to `_WINDOW_FILES` (2048) files, each
  stamped with the file's (size, mtime) key. A killed run therefore resumes. No change was needed
  for resume; this ADR adds cancellation, which uses the same mechanism.
- The hashing work (file reads) runs in a `ThreadPoolExecutor` (16 threads), NOT in the thread
  that called `run_candidates_warm`. OS "background mode" is per-thread, so setting it only on the
  caller would lower the priority of the cheap orchestration and leave the I/O at normal priority.

## Decision

1. **Auto-start.** `run_scan`'s success path calls `auto_warm_after_scan`. A scan is "full" iff one
   of its roots is the user's profile (`Path.home()`) or an ancestor of it (`scan_roots_cover_home`:
   the SIMPLE-mode "my files" scan and a full-drive scan). A scan of a subfolder is scoped and does
   not auto-start. There is no explicit "full/partial" flag in the scan status; this definition is
   derived from the roots, so it needs no schema change. A cancelled or failed scan never starts
   one. Also once at dashboard start (FastAPI lifespan), after a 30 s delay
   (`AppState.startup_warm_delay_seconds`), if the persisted index has records and the cache is
   cold. Both paths are gated by `[dedup] warm_after_scan` (default true) and refuse to start
   while a scan is running ("after a scan, not during it").
2. **One mechanism.** Every start -- `POST /api/candidates/warm`, the 409 courtesy kick, both
   auto-starts -- goes through `service.begin_candidates_warm`, the existing check-and-set under
   `state.lock`, now stamping `source` (`auto`|`user`). There is no second warm path; auto runs
   `run_candidates_warm` on a daemon thread (`state.warm_spawner` is the test seam).
3. **Low priority.** An `auto` run enters Windows background mode
   (`SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)`; end in a `finally`)
   through the injectable `reclaim.thread_priority` seam (no-op off Windows; failures are logged,
   never raised). Threads covered: (a) the warm-up worker thread, which runs the detectors, cluster
   assembly and safety evaluation; (b) every hashing pool thread, via the pool's `initializer`
   (`find_duplicate_clusters(worker_initializer=...)`). The pool is now created per window (threads
   spawn lazily, so this is cheap) so the initializer sees current state. Pool threads leave
   background mode when their pool shuts down at the end of the window (thread exit). Deliberately
   NOT covered: the dashboard/server threads and the scan, which stay at normal priority.
4. **Promotion.** A `user` request (`POST /api/candidates/warm`, or the 409 courtesy kick) arriving
   while an `auto` run computes does not start a second run: the run is marked `promoted`
   (route answers 202 with that run's status; a second user kick on a *user* run still gets 409 as
   before). The worker leaves background mode at its next batch boundary and pool threads created
   afterwards never enter it. Best effort: the batch in flight finishes at low priority.
5. **Cancellable.** `AppState.candidates_warm_cancel_event` is checked through a `checkpoint`
   callback passed down (`run_candidates_warm` -> `_cached_all_candidates` -> `_all_candidates` ->
   `generate_duplicate_candidates` -> `find_duplicate_clusters`), not a global: before the
   detectors, between detectors and duplicates, before each hash window, between a window's
   partial and full stages, and while a stage collects results (one check per file group). On cancel pending hashes are flushed first, `DedupCancelled`
   propagates, the status becomes `cancelled`, and because `_cached_all_candidates` assigns the
   cache only after a complete compute, nothing partial is cached and the cache stays cold.
   `POST /api/candidates/warm/cancel` is idempotent (200 + current status; a no-op when idle);
   while the worker unwinds the status is `computing` with `cancel_requested: true`. Cancelled
   automatically when a new scan starts (all three scan start sites: `POST /api/scan`,
   `/api/scan/full-drive`, `/api/scan/my-files`, and the MCP `scan` tool) and on app shutdown.
6. **Resume** is the existing per-window flush (ADR-0038), proven again for cancel: a second pass
   hashes exactly `total - already_hashed` files; a changed file's committed hash is not trusted
   (its (size, mtime) key no longer matches).
7. **Bug found and fixed on the way.** An early exit from the hash loop (cancel, or any error) left
   the `duplicate_size_candidates` SELECT cursor open via the traceback, so `ScanIndex.close()`'s
   `PRAGMA wal_checkpoint(TRUNCATE)` failed with "database table is locked" and a cancel surfaced
   as a failed warm-up. The loop now closes the generator in a `finally`.

Status additions (all additive): `status` may be `cancelled`; fields `source`, `promoted`,
`cancel_requested`.

## Consequences

- A user who scans their profile and opens the dashboard later finds the cache warm (or partly
  hashed, so the remainder is shorter) instead of waiting from zero. Cancelling or quitting never
  loses committed hashes.
- The hash pass writes to the index (one commit per window) while a user may be reading it; this
  was already true of the on-demand warm-up. No new locking.
- The pool is created per window instead of once per pass: negligible thread-spawn overhead, but
  a deliberate change to `find_duplicate_clusters`'s structure.
- The MCP server process also auto-warms after a full-home MCP scan (it shares `run_scan`).
- The warm-up never competes with another dedup pass: every reader of the clusters, including
  `GET /api/duplicate-clusters/review`, uses the cache the warm-up fills (ADR-0037 addendum
  "review clusters").
- **NOT done / NOT measured (be honest):**
  - Real-drive wall-clock was not measured. The "about an hour" figure is the owner's earlier
    estimate, not re-measured here. Tests count hash calls, never time.
  - The effect of background mode on a CPU-contended (100% CPU) machine was not measured. Windows
    background mode lowers CPU, I/O and memory priority of a thread; whether that actually keeps
    the foreground responsive (or starves the warm-up) on the owner's box is unverified. The seam
    is injectable and `[dedup] warm_after_scan = false` disables the feature.
  - Promotion is best effort at batch granularity (up to 2048 files of I/O may complete at low
    priority after a user asks).
  - Cancellation latency is bounded by the reads already in flight (at most the pool size, 16):
    not-yet-started reads of the window are cancelled, in-flight ones finish and are flushed, not
    wasted. A single huge file in flight still has to finish reading.
  - A thread already blocked on a stuck read is not interrupted (existing 30 s per-file guard).
  - No UI for the new status fields or a cancel button; the API and status are the contract.
  - Scoped scans never auto-warm, so a user who only scans subfolders still warms on first open.

## Alternatives

- **Warm during the scan** (rejected by the owner): competes with the scan for the same I/O and
  works against a moving index.
- **Process-level priority (`SetPriorityClass`)** instead of thread mode: would also slow the
  dashboard server and scan threads in the same process.
- **A separate worker process** for hashing: real isolation and a cheap hard kill, but a second
  process lifecycle, IPC and packaging surface for a single-user localhost tool; the per-window
  checkpoint already gives bounded-latency cancel.
- **Cancel by thread interruption / abandoning the thread:** can corrupt the index mid-write and
  hides the resume story; cooperative boundaries are safe because every boundary is a commit point.
- **Explicit `full`/`partial` flag on `ScanStatus`:** more precise than deriving it from roots but
  a wider change for no extra behavior today.
