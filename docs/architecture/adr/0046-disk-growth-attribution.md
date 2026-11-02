# ADR-0046: Disk growth attribution

Status: accepted (pass 1: module, store, CLI; API, dashboard panel and toast follow in pass 2).
Spec: `docs/specs/disk-growth-attribution.md`.

## Context

When a drive fills, finding what grew took hours of manual folder sizing (another session's
scratch at 81.5 GiB, a 58.5 GiB overnight drop, the pagefile moving between drives). Reclaim only
knew the 80% threshold, not the cause. A full index scan takes 7-80 min, the USN journal needs
admin, and per-process write counters do not measure growth.

## Decision

`reclaim.growth` takes a **budgeted, stalest-first folder-size snapshot** (default 120 s wall,
3M listed entries, one budget shared by all roots) of volumes, root system files, known caches,
top-level folders, agent scratch sessions and (from the index, no IO) opaque dependency trees,
and stores it in `data/growth.sqlite3`. Analysis is pure functions over the stored series:

- window growth = latest complete value minus latest complete value at or before the window
  start; a `partial` (budget-cut) value is a lower bound and never an endpoint;
- the volume delta is shown with the non-nested attributed sum and a "not attributed" remainder;
- the anomaly rule is spec §1.5 (abs floor 5 GiB, share floor 2%, robust z >= 4 against the
  key's previous 14 days, ε = 0.1 GiB/day; volume-level rule over 24/72 h). Attribution drills
  down from an ancestor to a descendant that explains >= 60% of its growth, so the toast names
  `%TEMP%\claude\<project>\<session>` and not the profile.

Safety: read-only (listing only, no file is opened), reparse points are never followed or counted,
cloud placeholders are skipped, and excluded paths are matched by name before any listing, so
they are never walked or named; they appear only as one "Excluded projects" aggregate from the
index (unknown, stored as partial, when no index is given).

Deviations from the spec text: `sizes` has an extra nullable `observed_at` (index-sourced rows
are as old as the last index scan); no secondary index on `sizes` (the primary key already
serves the only access path).

## Consequences

- Growth inside a root the budget never finishes stays "partial" and invisible; it shows up as
  "not attributed". A single huge top-level folder (e.g. `AppData`) can stay partial for many
  snapshots. Nested roots are memoised so a cache inside a top-level folder is listed once.
- Unreadable subtrees count 0 in every sample (growth there is "not attributed").
- Sizes are logical bytes (not allocation size); hardlinks and NTFS compression are not modelled.
- Measured store size for a simulated year (250 keys, 6 snapshots/day, 547,500 rows, 30% of
  folders change per snapshot): 11.6 MB. This is under the spec's BELIEVED 15 MB but **above its
  5 MB success metric**; reaching 5 MB needs storing only changed values (forward-fill on read).
- Per-week precision/recall claims need >= 4 weeks of continuous snapshots; the existing logs
  are fragments (see the eval, still to do), so no such claims are made here.

## Alternatives

Diffing successive index scans (too rare, cannot explain an overnight drop); USN journal
(needs admin); per-process write counters (writes are not growth, no folder attribution).
