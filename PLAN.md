# Reclaim — Execution Plan

**Tier:** T1 (portfolio project). Spec: `reclaim-spec.md` (authoritative — kept under that
filename rather than `spec.md` since it's referenced by that name throughout the build brief).

**Orchestration:** Opus/Fable orchestrator -> Sonnet executor -> Haiku verifier per
CLAUDE.md rules 69/70. Verifier signs off each stage against its CI gate before the next
stage starts. No auto-merge; GG merges.

**Non-negotiables (from spec, restated so they can't get lost mid-build):**
- Rules-first, no ML in Phase 1.
- SafetyValidator runs BEFORE candidate generation, every stage.
- Dry-run is the default; `--apply` is explicit and opt-in.
- No fabricated confidence scores in code or UI copy (hash match = exact, pHash = a
  reported Hamming distance, heuristics labeled "heuristic").
- Never scan or modify GG's real disk without explicit sign-off. First real-disk run is
  dry-run mode, output a report for GG's review before anything is queued for apply.

**Superseded by ADR-0001 (2026-07-16), documented not silently drifted:** "`send2trash`
only — no permanent deletes anywhere in v1" no longer holds project-wide. Rebuildable
categories (`dev_artifacts`, `package_caches`, `temp_and_browser_caches`, `crash_dumps`) now
permanently delete on apply (`retention_days=None`) because their real recovery mechanism was
always the rebuild command, never the vault — see the ADR for the full rationale and the
defense-in-depth re-checks that gate this. Every other category is unchanged: vault + 30-day
retention + restore, now with an explicit `purge` command for expired entries.

## Stage order (riskiest assumption first)

| # | Stage | Status | CI gate |
|---|-------|--------|---------|
| 1 | SafetyValidator + golden fixture tree + hard CI gate | done | zero protected files ever appear in Tier A; build fails on any hit |
| 2 | Scanner (os.scandir, SQLite index, cloud-placeholder detection) | done | placeholder-exclusion unit test passes; perf budget ≥100K files/min on SSD (real number pending GG's SSD, see checkpoint) |
| 3 | Rule detectors (dev artifacts, caches, temp, dumps, installers, archive pairs, logs) | done | detector fixtures pass, manifest-adjacency check enforced |
| 4 | Exact-duplicate pipeline (size bucket -> 64KB partial hash -> BLAKE3) | done | precision = 1.0 on fixtures |
| 5 | Executor + quarantine manifest + batch undo | done | every quarantined fixture file restorable in tests |
| 6 | FastAPI + dashboard (treemap, category cards, review queue, restore view) + visual identity | done | dashboard renders against fixture data; no prior-project branding reused |
| 7 | ADR-0001 category-tiered retention: direct-delete + purge | done | zero protected files ever reach direct-delete or purge, even adversarially |

## Checkpoints

### 2026-07-12 — Repo scaffold
- Created `C:\Users\dev\ml-projects\reclaim`, git init, `uv init --package` pinned to
  Python 3.12.12.
- Structure: `src/reclaim/` (package), `tests/`, `evals/fixtures/`, `docs/architecture/adr/`,
  `data/` (gitignored — runtime scan index + quarantine, never committed), `scripts/`,
  `.github/workflows/`.
- `pyproject.toml`: ruff (E,F,I,UP,B,SIM,S,T20,DTZ,PTH,TRY,RUF; line-length 100), mypy
  strict on `src/reclaim`, pytest + pytest-asyncio (auto mode) + pytest-cov (80% floor on
  `src/reclaim`, `cli.py` omitted), deps: fastapi, uvicorn, pydantic v2, pydantic-settings,
  structlog, send2trash, blake3, jinja2.
- Added PM header (target user / pain point / success metric / who pays) to
  `reclaim-spec.md` per rule 20a.
- Next: Stage 1 (SafetyValidator + fixtures + CI gate) via executor subagent.

### 2026-07-12 — Stage 1 complete: SafetyValidator + golden fixture tree + hard CI gate
- `src/reclaim/models.py`: `FileRecord` (frozen dataclass, slots — zero-validation-overhead
  for the future ≥100K files/min scanner hot path), `Verdict` (BLOCKED/REVIEW_ONLY/ELIGIBLE),
  `SafetyResult`.
- `src/reclaim/config.py`: pydantic-settings `Config`/`SafetyConfig`/`CategoriesConfig`,
  `tomllib`-based loader, `config.example.toml` committed, real `config.toml` gitignored.
- `src/reclaim/safety.py`: `SafetyValidator` deny-first precedence — built-in deny (protected
  roots, in-git-repo except clean-repo+category-enabled node_modules, protected extensions/
  `.ssh`, DB/VM extensions, Docker/WSL roots, cloud placeholders) > user deny-list (always
  wins) > built-in review-only (finance/tax/legal) > user allow-list (promotes review-only
  only, never overrides deny) > default eligible.
- `evals/fixtures/golden_tree.json` (44 cases) + `build_golden_tree.py` (materializes a real
  temp dir tree, runs actual `git init`/commit/dirty) + `evals/test_safety_gate.py`: per-case
  verdict match, the hard gate (zero protected-category cases ever `ELIGIBLE`, fails loudly
  listing every leak), and a gate-coverage self-check so the hardcoded protected-category set
  can't silently drift from the manifest. `.github/workflows/eval.yml` added (separate from
  `ci.yml`, same cancel-in-progress pattern).
- `tests/test_safety.py`: 26 fast precedence unit tests.
- Verification (commands run by an independent Haiku verifier, not just the executor):
  `uv run ruff check .` — pass · `uv run ruff format --check .` — pass (9 files) ·
  `uv run mypy` — pass (6 source files) · `uv run pytest tests/ -v` — 26 passed ·
  `uv run pytest evals/ -v` — 3 passed. Verifier independently re-derived the precedence
  order from spec and confirmed it matches; confirmed golden tree has real negative
  (benign-eligible) cases, not just positives; hand-checked 4 fixture cases against spec
  wording; confirmed the hard-gate assertion fails loudly with full violation details, not a
  soft warning.
- Judgment calls made by executor (accepted): `FileRecord`/`SafetyResult` as frozen
  dataclasses not pydantic (hot-path perf); protected-root/Docker-WSL patterns as globs
  matched against posix-form paths so only the 4 OS-absolute roots need test overrides;
  cloud-placeholder bit stored in the fixture manifest rather than actually set on disk
  (not settable without a real cloud filter driver — `SafetyValidator` never touches disk
  itself, so this doesn't weaken the gate); `expected_reason_contains` checked against the
  human-readable rationale (the UI-facing string), not the reason code.
- **Correction to the note above**: the scanner does NOT call `SafetyValidator` itself — that
  boundary sits between the scan index and Stage 3's candidate generation, not inside the
  scanner. The scanner's job is a complete, honest inventory (protected files included — they
  still take up disk space and belong in the treemap); `FileRecord.attributes`/
  `git_repo_root`/`git_repo_clean` are populated for real so Stage 3 can run
  `filter_candidates()` before emitting anything to Tier A/B.

### 2026-07-13 — Stage 2 complete: Scanner + SQLite index
- `src/reclaim/index.py`: `ScanIndex` (SQLite schema/CRUD), `StoredStat`, `is_unchanged`
  (size+mtime only — NTFS atime is explicitly never consulted, per spec), `physical_size_bytes`
  (dedups by `(dev, ino)`, first-seen wins) vs `logical_size_bytes` (sums all paths, double-
  counts hardlinks), `prune_missing`, two distinct inventory queries — `full_inventory`
  (includes cloud placeholders, for treemap/total-usage accounting) and `candidate_inventory`
  (excludes them — nothing downstream should ever see a placeholder as candidate-eligible).
- `src/reclaim/scanner.py`: `scan_tree` — `ThreadPoolExecutor`, one unit of work per top-level
  dir. Recursion into any entry is gated strictly on the `FILE_ATTRIBUTE_REPARSE_POINT` (0x400)
  bit from a real `stat()` call, never on `DirEntry.is_dir()` (which is unreliable for Windows
  junctions). Git-repo root detection walks upward with per-directory memoization;
  `git status --porcelain` runs at most once per distinct repo root per scan (cached), and
  defaults `git_repo_clean = False` (fail-closed) if `git` is missing or the call fails.
  Cloud-placeholder bit (`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`, 0x400000) plus a best-effort,
  explicitly-labeled-heuristic cloud-sync-root detector (OneDrive/Dropbox/Google Drive).
- `src/reclaim/models.py::FileRecord` extended (additive, all new fields defaulted) with
  `mtime`, `ctime`, `dev`, `ino`, `is_reparse_point` — Stage 1 call sites (`safety.py`,
  `build_golden_tree.py`) untouched; Stage 1's hard gate re-verified still green.
  `dev`/`ino` require a real `Path.stat()` call — `DirEntry.stat()`'s cached result does not
  populate them on Windows (empirically confirmed; the original brief assumed it would).
- `src/reclaim/cli.py`: real `reclaim scan <path> [--db] [--full] [--workers]` subcommand.
- Verification (independent Haiku verifier, re-ran everything itself):
  `uv run ruff check .` — pass · `uv run ruff format --check .` — pass (14 files) ·
  `uv run mypy` — pass (8 source files) · `uv run pytest tests/ -v` — 49 passed ·
  `uv run pytest evals/ -v` — 4 passed, **including Stage 1's safety gate still green** ·
  `uv run pytest --cov` — 94% (80% floor holds). Verifier independently created a real
  hardlink via `os.link()` and confirmed `physical_size_bytes`/`logical_size_bytes` numbers by
  hand (150 vs 250 bytes on a 100B+50B pair); confirmed the reparse-point recursion gate reads
  the attribute bit, not `is_dir()`, by quoting the actual line; confirmed cloud-placeholder
  exclusion has two distinct, separately-tested query paths.
- **Perf budget honesty (rule 65b — metric provenance)**: `evals/test_scanner_perf.py` is an
  explicitly-labeled CI smoke test, not a validation of the spec's real ≥100K files/min number.
  Measured on this dev machine (not GG's target SSD, not the real target tree size): full scan
  ~10,500–15,000 files/sec on ~4,000 synthetic files (command:
  `uv run pytest evals/test_scanner_perf.py -v`, 3 runs). The smoke floor asserted in CI is
  150 files/sec (~11x below spec target) specifically so it can't flake on a loaded runner.
  **The real ≥100K files/min number is still unmeasured and must come from a dry-run scan of
  GG's actual disk before Phase 1 is considered complete** — flagging this explicitly rather
  than letting the dev-machine number stand in for it.
- Judgment call requiring a decision later: cloud-sync-root heuristic (`is_cloud_sync_root`)
  is implemented and tested but not yet wired into any filtering decision — available for
  Stage 3 detectors to consult if useful, not authoritative on its own (soft heuristic, not a
  built-in deny signal like the placeholder attribute bit).
- Next: Stage 3 — Rule detectors (dev artifacts w/ manifest-adjacency check, caches, temp,
  crash dumps, old installers, extracted-archive pairs, large logs). Every detector must call
  `SafetyValidator.filter_candidates()` on its output before anything is tagged Tier A/B — this
  is the first stage where that boundary actually gets exercised.

### 2026-07-13 — Stage 3 complete: Rule detectors + SafetyValidator candidate boundary
- `src/reclaim/detectors.py`: all 7 spec categories (dev artifacts w/ manifest-adjacency,
  package/model caches, browser/temp/thumbnail caches, crash dumps, old installers,
  extracted-archive pairs, large logs) plus `generate_candidates()` — the single entry point
  that runs every detector, drops nested/overlapping proposals, and is the first place
  `SafetyValidator.evaluate()` actually gets called on detector output before a tier is
  assigned. `BLOCKED` → excluded entirely (not even Tier B); `REVIEW_ONLY` → forced Tier B;
  `ELIGIBLE` → Tier A only if `config.categories.*.enabled`, else degrades to Tier B (never
  silently dropped — matches spec's "no silent permanent deletion" corollary that nothing
  eligible silently vanishes either).
- `src/reclaim/models.py` extended additively: `Tier` (A/B), `RawCandidate`, `Candidate`.
  `src/reclaim/config.py` extended: per-category configs (package caches, temp/browser
  caches, crash dumps, old installers w/ its own required opt-in per spec, large logs,
  archive pairs), all defaulting to disabled. `src/reclaim/index.py` extended:
  `subtree_size_bytes()` for directory-candidate size aggregation.
  `safety.py`/`scanner.py` confirmed zero-diff from the Stage 2 commit.
- Manifest-adjacency is absolute: no adjacent manifest = not proposed at all, not even Tier B
  (node_modules→package.json, venv→pyproject.toml/requirements.txt/setup.py, target/→
  Cargo.toml/pom.xml, build|dist→JS/Python manifests, .next→package.json,
  .gradle→build.gradle*). `.m2/repository`/global `.gradle/caches` are package caches
  (no manifest check), not dev artifacts — coexists cleanly with a project-local `.gradle`
  dev-artifact match since the two checks look at different things.
- Extracted-archive pairs: only the archive is ever proposed (≥90% name-overlap via
  `difflib.SequenceMatcher`); the extracted directory and everything inside it never appears
  as a candidate — verified end-to-end, not just in unit isolation.
- Old installers: age checked against `mtime` only (never atime, consistent with Stage 2);
  defaults to Tier B unless `categories.old_installers.enabled` is explicitly set, per spec's
  explicit call-out that this category needs its own opt-in.
- Verification (independent Haiku verifier, re-ran everything): `uv run ruff check .` — pass ·
  `uv run ruff format --check .` — pass (18 files) · `uv run mypy` — pass (9 source files) ·
  `uv run pytest tests/ -v` — 83 passed · `uv run pytest evals/ -v` — 5 passed (Stage 1 safety
  gate + Stage 2 scanner tests still green) · `uv run pytest --cov` — 91%. Verifier
  independently quoted the exact BLOCKED/REVIEW_ONLY/ELIGIBLE branch logic, confirmed the
  end-to-end eval case (manifest-valid dev-artifact dir inside a protected root still gets
  excluded) is a real scanner→index→detector→SafetyValidator run, not mocked, and confirmed
  `git diff 92cfd62 -- src/reclaim/safety.py src/reclaim/scanner.py` is empty.
- Judgment call: all detectors suggest `Tier.A` uniformly; `generate_candidates()` is the sole
  place that decides final A vs. B based on category-enabled state — behaviorally identical
  to per-detector tier suggestion but avoids duplicated gating logic. Default enable state for
  every new category is `False` (conservative, matches existing `dev_artifacts` posture).
- Next: Stage 4 — Exact-duplicate pipeline (size bucket → 64KB partial hash → full BLAKE3),
  precision = 1.0 required on fixtures (byte-identical check). Duplicate clusters are Tier B
  (review queue) per spec's Decision Policy, with a keep-heuristic (prefer copy outside
  Downloads/Temp, oldest path, shortest depth) the user can override per cluster — this also
  needs to run through `SafetyValidator` before any cluster member is proposed.

### 2026-07-13 — Stage 4 complete: Exact-duplicate pipeline
- `src/reclaim/dedup.py`: staged hashing exactly per spec — size bucket (skip singletons and
  0-byte files) → 64KB first+last partial BLAKE3 hash (whole-file hash if ≤128KB, no
  double-read) → full BLAKE3 hash only for surviving `(size, partial_hash)` groups with ≥2
  members. Only files that could plausibly be duplicates are ever fully hashed.
  `select_keep()`/`find_duplicate_clusters()`/`generate_duplicate_candidates()` mirror
  `detectors.py::generate_candidates()`'s contract so Stage 5/6 combine both candidate lists
  uniformly.
- Keep-heuristic (exact priority, ties broken lexicographically for reproducibility): not
  under Downloads/Temp > earliest `ctime` (Windows creation time — the "which copy existed
  first" signal, chosen deliberately over `mtime`) > shortest path depth.
- **Tier A/B resolution** (spec has two passages in tension — flagging the resolution, not
  hiding it): "Exact duplicates" is listed under "Rule Categories (auto-quarantine eligible)"
  but Decision Policy's Tier B description names "duplicate clusters" as a review-queue
  example. Resolved consistently with every other category's contract: `duplicates` is
  Tier-A-capable via `config.categories.duplicates` (default `False`, same conservative
  posture as everything else), so by default it lands in Tier B exactly as Decision Policy's
  example describes; enabling the flag makes it behave like every other auto-quarantine
  category. One mental model across all categories, no duplicates-only special case.
  SafetyValidator gate: `BLOCKED` non-keep member excluded entirely; `REVIEW_ONLY` forced
  Tier B; `ELIGIBLE` Tier A only if enabled. The "keep" member is never evaluated or output.
- Hash cache added to `index.py` (`partial_hash`/`full_hash` + `hash_size`/`hash_mtime`
  columns, invalidated whenever a row's current size/mtime differs from what the hash was
  computed against) so repeated dedup runs don't re-hash unchanged files — matches spec's
  Scanner-section intent for a "hash cache" keyed by (path, size, mtime).
- **Precision = 1.0 proof** (`evals/test_dedup.py`): for every produced cluster, independently
  re-reads full byte content of every member from disk and asserts pairwise byte-equality —
  trusts nothing the hash pipeline computed. Adversarial fixture: two 200KB files sharing
  identical first/last 64KB but different middle bytes — partial hashes collide, full hashes
  differ, and the test asserts they never cluster (proves the full-hash disambiguation step is
  doing real work, not just the partial-hash step).
- Verification (independent Haiku verifier, re-ran everything + a hard `git diff` check):
  `uv run ruff check .` — pass · `uv run ruff format --check .` — pass (22 files) ·
  `uv run mypy` — pass (10 source files) · `uv run pytest tests/ -v` — 98 passed ·
  `uv run pytest evals/ -v` — 6 passed (Stages 1-3 evals still green) ·
  `uv run pytest --cov` — 83% (80% floor holds) ·
  `git diff 5dafbc0 -- src/reclaim/safety.py src/reclaim/scanner.py src/reclaim/detectors.py`
  — empty, confirming those three files are genuinely untouched. Verifier independently
  quoted the staged-hashing filter logic, the keep-heuristic sort key, the SafetyValidator
  branch logic, and the hash-cache invalidation check line-by-line.
- Gotcha (moved to Gotchas section below): pytest's `tmp_path` lives under the OS temp dir,
  which silently broke the Downloads-vs-Temp keep-heuristic test fixture (both paths counted
  as "under Temp"). Dedup's eval fixtures root under `data/_test_scratch/<uuid>/` instead
  (gitignored, torn down after the test) specifically to keep that test meaningful.
- Next: Stage 5 — Executor + quarantine manifest + batch undo. `send2trash` only, dry-run
  default, `--apply` explicit. Combine `detectors.py::generate_candidates()` and
  `dedup.py::generate_duplicate_candidates()` into one candidate list, apply Tier A candidates
  (or user-selected Tier B ones) via quarantine (send2trash + manifest JSONL: original path,
  size, category, rationale, batch id), batch undo, and a post-apply report using real
  filesystem results (files, bytes freed, category breakdown) — never estimates.

### 2026-07-13 — Stage 5 complete: Executor + quarantine + batch undo
- `src/reclaim/executor.py`: `apply_batch()`/`restore_batch()`, `QuarantineManifestEntry`
  (JSONL, append-only, `data/quarantine/manifest.jsonl`), `BatchApplyReport`/`RestoreReport`.
  Dry-run (`apply=False`, the default) is a true no-op — no `shutil.move`, no
  `send2trash.send2trash`, no manifest write; verified independently by the verifier
  monkeypatching all three to raise-if-called and confirming dry-run still succeeds.
- **Quarantine method decision** (judgment call, made explicitly rather than left ambiguous):
  default method is the **vault** (move into `data/quarantine/<batch_id>/` + manifest), not
  `send2trash`/Recycle Bin — because `send2trash` gives no programmatic handle to restore a
  file later, so it cannot honestly satisfy the spec's "every quarantined file restorable;
  restore verified in tests" success criterion without a new heavy dependency (pywin32 shell
  API) that isn't in spec's Stack. `method="recycle_bin"` is still supported (spec lists it
  explicitly) but `restore_batch()` refuses a batch containing recycle-bin entries with a
  clear, honest error directing the user to Windows Explorer's native restore — never
  fabricates a restore capability it doesn't have.
- Defense-in-depth: `apply_batch` independently re-checks `candidate.safety_verdict !=
  Verdict.BLOCKED` for every item and raises (does not silently skip) if violated — a last
  line of defense even though every candidate should already be safety-filtered upstream.
- No permanent delete anywhere in this stage (confirmed by verifier grep: no `os.remove`,
  `Path.unlink`, `shutil.rmtree` used destructively). `retention_until` is recorded on
  manifest entries as metadata only — nothing in v1 acts on it to purge anything, per spec's
  repeated "No permanent delete in v1" / "No Tier for silent permanent deletion" invariants.
  Partial-batch failures (one item fails) are recorded per-item and surfaced in the report,
  never silently swallowed or allowed to abort the rest of the batch.
  `bytes_freed` sums real measured `Candidate.size_bytes` from succeeded items only;
  `shutil.disk_usage()` before/after is captured only when `apply=True`, never fabricated
  during dry-run — directly serves the top-level "reclaims ≥30GB, verified via before/after
  disk-free measurement" success criterion.
- CLI: `reclaim apply <path> [--apply] [--tier A|B|both] [--method vault|recycle_bin]` (Tier A
  only by default — Tier B requires explicit `--tier B`/`--tier both` since those are
  review-queue items the user hasn't actually reviewed via CLI) and `reclaim undo <batch_id>`.
- Verification (independent Haiku verifier, wrote its own standalone checks rather than
  trusting the executor's tests): `uv run ruff check .` — pass · `uv run ruff format --check .`
  — pass (26 files) · `uv run mypy` — pass (11 source files) · `uv run pytest tests/ -v` — 110
  passed · `uv run pytest evals/ -v` — 7 passed (Stages 1-4 evals still green) ·
  `uv run pytest --cov` — 85.08% · `git diff 0e44383` on all seven Stage 1-4 source files —
  empty. Verifier independently constructed a real file, quarantined it via vault, and
  confirmed byte-identical restore by reading fresh from disk (not trusting the manifest);
  independently confirmed a BLOCKED candidate raises rather than proceeding.
- Next: Stage 6 — FastAPI + dashboard (treemap, category cards, review queue, restore view).
  Design logo/favicon/visual identity first, distinct from every other project in
  `ml-projects/`. Wires the whole pipeline (scan → candidates → dedup → apply/undo) behind a
  localhost-only web UI.

### 2026-07-13 — Stage 6 complete: FastAPI dashboard + visual identity (Phase 1 done)
- `src/reclaim/api/`: FastAPI backend (`state.py`/`schemas.py`/`service.py`/`routes.py`/
  `app.py`) + vanilla JS/HTML/CSS dashboard (no HTMX vendored in — plain `fetch`, avoids a new
  front-end dependency; spec's "vanilla JS/HTMX" wording treats it as an either/or). Binds
  `127.0.0.1` by default (verified — the CLI's `--host` default, not `0.0.0.0`). Endpoints:
  `POST /api/scan` (background task + `GET /api/scan/status` polling), `GET /api/summary`,
  `GET /api/treemap`, `GET /api/candidates`, `POST /api/apply`, `GET /api/quarantine`,
  `POST /api/restore/{batch_id}`. `reclaim serve` CLI subcommand added.
- Visual identity: "excavation/clearing space" theme — terracotta clay (occupied) vs pine
  green (reclaimed) on a warm sand neutral scale, deliberately not generic blue-SaaS, not
  shared with any other `ml-projects/` repo. Categorical palette validated via the `dataviz`
  skill's script for both light and dark surfaces (all PASS except one WARN in the 8-12 CVD
  floor band, which per the skill's own rule is legal only paired with visible text labels —
  every swatch always ships with its category label, never color alone).
- All 4 spec views implemented: Overview (summary stats + category cards, real measured
  bytes/counts, heuristic items explicitly labeled "heuristic" — no fabricated confidence
  anywhere), Storage Treemap (self-contained SVG squarified treemap, no chart library),
  Review Queue (real rationale pulled verbatim from `detectors.py`/`dedup.py`, duplicate
  clusters shown as a real side-by-side table with keep/removal status), Quarantine & Restore
  (reads the real manifest, restore-batch action). Dry-run simulation diff is a "1. Preview
  (dry-run) → 2. Confirm real apply" two-step flow inside Review Queue (reuses `POST
  /api/apply`'s `dry_run` field rather than a separate tab duplicating the same data).
- **Browser verification (per house rule — actually drove the running app, not just unit
  tests)**: started `reclaim serve`, built a disposable demo fixture tree outside the repo
  (dev artifact + exact duplicates, never GG's real disk), and exercised the full flow in a
  real Chrome tab via chrome-devtools MCP: empty state → scan → Overview/Treemap/Review Queue
  render real data → dry-run preview (confirmed via direct filesystem check that nothing was
  touched) → real apply with a confirm dialog → confirmed on disk the file was genuinely
  moved out of its original location into the vault → Quarantine & Restore → restore →
  confirmed on disk the file was genuinely back at its original path. Checked both light and
  dark themes, accessibility snapshot (skip-link, landmarks, live regions, labeled controls).
- **Bug found and fixed via this browser verification** (would not have been caught by the
  127 passing unit/API tests, which only exercise the backend): the scan-form submit handler
  called `pollScanStatus()` directly, but only `refreshScanStatus()` actually arms the
  repeating `setInterval` — a single `pollScanStatus()` call checks status once and, if it
  catches the scan mid-flight, never polls again, freezing the UI on "Scanning…" forever even
  after the backend finishes. Fixed in `src/reclaim/api/static/app.js` (submit handler now
  calls `refreshScanStatus()`); re-verified live in the browser that a triggered scan now
  correctly transitions to "complete" and the button re-enables.
- **Gitignore gap found and fixed**: `data/*.sqlite3` wasn't covered (only `.sqlite`/`.db`),
  and `reclaim serve`'s default index path is `data/reclaim_index.sqlite3` — the default
  runtime index would have been accidentally committable. Added the missing pattern.
- **Important product-level finding, surfaced honestly rather than papered over**: the
  post-apply report's `shutil.disk_usage()` delta was `0 bytes` for a real 200KB vault-quarantine
  in manual testing. This is expected, not a bug — moving a file into `data/quarantine/`
  keeps it on the same NTFS volume, so no space is actually freed until a human empties
  it later (same physics applies to Recycle Bin — nothing frees space until "Empty Recycle
  Bin"). Combined with spec's absolute "no permanent delete in v1" rule, **Phase 1 as built
  cannot literally satisfy the top-level success criterion "reclaims ≥30GB... verified via
  before/after disk-free measurement"** — quarantining moves files out of the way and makes
  them stop counting toward *logical* usage in the dashboard, but real physical disk-free
  won't move until GG manually empties the vault/Recycle Bin himself. The UI is honest about
  this (shows the real 0-byte delta separately from the summed candidate size, never
  conflates them) rather than fabricating a "30GB freed" claim it can't back up. Flagging this
  for GG's review before calling Phase 1 "done" against its own success criteria — options
  going forward: (a) accept "queued for reclaim, pending manual empty" as the real v1
  semantics and reword the success criterion, (b) add an explicit, separately-confirmed
  "empty the vault" action after the 30-day retention window (a real permanent-delete code
  path, which is a deliberate scope change from "No permanent delete in v1" and needs an ADR
  per rule 75), or (c) default new quarantines to `recycle_bin` method for the real-disk run
  specifically, since Windows still counts Recycle-Bin-held files against free space the same
  way — that doesn't free space either, so this doesn't actually resolve it; it's the same
  physics. This needs a decision before the first real-disk dry-run report is presented as
  progress toward the 30GB goal.
- Verification (independent Haiku verifier, re-ran everything): `uv run ruff check .` — pass ·
  `uv run ruff format --check .` — pass (33 files) · `uv run mypy` — pass (18 source files) ·
  `uv run pytest tests/ -v` — 127 passed · `uv run pytest evals/ -v` — 7 passed (Stages 1-5
  evals still green) · `uv run pytest --cov` — 95.90% · `git diff c74aa53` on all five
  untouched pipeline source files — empty. Verifier independently confirmed `dry_run=True`
  maps to `apply=False` (not inverted) by reading the mapping code and the specific tests.
- Judgment call: Storage Treemap colors only a top-level child by category if that child
  itself is a directly-flagged candidate — it does not recursively aggregate category
  presence from deeper in the subtree (so e.g. a project dir containing a nested
  `node_modules` shows "Uncategorized" at the top level even though it contains a real
  candidate). Disclosed by the executor as a time-boxed v1 scope decision, confirmed in manual
  browsing: honestly labeled "Uncategorized" rather than silently wrong, sizes are still
  correct. Worth a follow-up pass to recursively roll up category coloring, not blocking.
- **Phase 1 (Deterministic Engine) is now feature-complete per the spec's stage list.**
  Outstanding before calling it truly done: (1) the disk-free-delta product question above,
  (2) the real ≥100K files/min perf number on GG's actual SSD (Stage 2's smoke test only
  proves the mechanism, not the target), (3) GG's first real-disk run in dry-run mode with a
  report for review, per the explicit instruction to never scan/modify the real disk without
  that review step happening first.

### 2026-07-16 — Stage 7 complete: ADR-0001 category-tiered retention (resolves the disk-free-delta finding)
- `docs/architecture/adr/0001-category-tiered-retention.md`: the disk-free-delta finding from
  Stage 6 (vaulting on the same NTFS volume frees nothing) is resolved by making retention a
  per-category-group property instead of a project-wide policy. `dev_artifacts`,
  `package_caches`, `temp_and_browser_caches`, `crash_dumps` → `retention_days=None` (permanent
  delete on apply — their real recovery mechanism was always the rebuild command, never the
  vault). `old_installers`, `archive_pairs`, `large_logs`, `duplicates` → `retention_days=30`
  (unchanged vault+restore behavior, plus a new explicit `purge` command for expired entries).
- `src/reclaim/config.py`: `retention_days: int | None` added to every category-group config;
  `dev_artifacts`/`archive_pairs`/`duplicates` converted from bare `bool` to their own config
  models (`enabled` + `retention_days`) — a breaking schema change fixed at its 4 call sites
  (`safety.py`, 2 in `detectors.py`, 1 in `dedup.py`).
- `src/reclaim/executor.py`: `apply_batch` now branches **per-candidate** on
  `candidate.retention_days is None` — permanent `Path.unlink()`/`shutil.rmtree()` (no vault,
  no Recycle Bin) regardless of the batch's requested `method`. **Mandatory pre-delete safety
  re-check**: before deleting anything, every direct-delete candidate in the batch is
  re-evaluated against a *freshly reconstructed* `FileRecord` (live stat + live git-repo state,
  via `scanner.py`'s newly-exposed `build_record_for_path`) using the current config — any
  single fresh `BLOCKED` verdict aborts the **entire batch**, deleting nothing, mirroring the
  existing upstream `SafetyInvariantError` philosophy rather than skip-and-continue. Manifest
  gains `is_dir`, `rebuild_instruction`, `retention_days`, `purged`/`purged_at`; a
  `direct_delete` entry still records everything needed for audit (category, rationale,
  rebuild instruction) with `vault_path=None`/`retention_until=None` since nothing was vaulted.
  `restore_batch` refuses a `direct_delete` batch with a new, distinct
  `DirectDeleteRestoreImpossibleError` ("nothing to restore," not reused Recycle-Bin wording).
  `apply_batch(method="direct_delete")` is rejected outright — that value is only ever derived
  per-candidate from `Candidate.retention_days`, never requested for a whole batch.
- `src/reclaim/purge.py` (new): `purge_expired()` permanently deletes vaulted items whose
  `retention_until` has passed — a hard boundary, not a soft default; there is no parameter
  that can force an unexpired entry to purge. Dry-run by default. Pre-purge safety re-check
  mirrors the direct-delete one but is necessarily weaker (documented honestly, not
  overclaimed): by purge time the original path is long gone, so the re-check reconstructs a
  `FileRecord` from the manifest's own recorded fields (catches config drift — a newly
  tightened deny pattern or protected extension — but cannot detect that the original location
  became a git repo since vaulting, since that path no longer exists to check). Same
  whole-run-abort-on-any-BLOCKED philosophy. Real `shutil.disk_usage()` before/after against
  the vault drive — this delta is expected to be genuinely non-zero, unlike vaulting's.
  `reclaim purge [--apply] [--config] [--db] [--manifest] [--vault-dir]` CLI subcommand added.
- Verification (independent Haiku verifier — re-derived everything with its own standalone
  adversarial scripts, not just re-running the executor's tests): all 7 command-line checks
  pass (`ruff`, `format`, `mypy`, 151 unit tests, 10 evals, 95.36% coverage, and a full manual
  read of the diff on every "narrowly-scoped" file confirming no scope creep). Adversarial
  scenarios A-G independently constructed and confirmed: (A) a forged-ELIGIBLE protected `.pem`
  file survives a direct-delete attempt, batch aborts; (B) a **time-of-check-to-time-of-use**
  case — a candidate that was safe at generation time but had a git repo `git init`'d around it
  before apply — is caught by the *live* re-check and the batch aborts (proves the re-check
  derives current state, not cached state); (C) a not-yet-expired vault entry is never purged
  even with `apply=True`; (D) a protected-pattern vault entry past its retention window is
  refused by the purge safety re-check; (E) a mixed batch (one `retention_days=None` + one
  `retention_days=30` candidate) correctly permanently-deletes the first and vaults the second,
  with per-item `method` reported correctly; (F) restoring a `direct_delete` batch raises the
  new distinct error; (G) requesting `method="direct_delete"` at the batch level is rejected.
- This is the first stage where "verify + commit" itself carried real risk — treated
  accordingly: I personally read the full diff on `safety.py`/`detectors.py`/`dedup.py`/
  `scanner.py` before dispatching the verifier (confirmed each change matched the ADR's
  narrowly-scoped list exactly), then had the verifier independently re-derive all 7
  adversarial scenarios with its own scripts rather than trusting the executor's test suite.
- Judgment calls (executor's, accepted): `scanner.py`'s sanctioned exception extended to one
  small additive `build_record_for_path` wrapper (needed since `_build_record` requires a live
  `os.DirEntry`, which a `Path`-only caller like `executor.py` doesn't have — re-derives it via
  a parent-directory `scandir` lookup rather than duplicating stat/git logic); existing
  vault-round-trip tests that used `dev_artifacts` fixtures were updated to `retention_days=30`
  overrides so they can still demonstrate vault+restore mechanics now that `dev_artifacts`
  defaults to direct-delete; `api/routes.py`/`api/service.py` updated so the Stage 6 dashboard
  doesn't claim a direct-delete batch is restorable.
- Next: first real-disk dry run (Task #12) — `C:\` scan, `--apply` forbidden this run, report
  for GG's review before anything is queued for apply.

### 2026-07-16 — First real-disk dry run stalled; dedup pipeline hardened
- First attempt: `reclaim scan C:\` completed cleanly (3,139,595 entries, 454,330 dirs,
  708.83s), but the follow-on `apply` dry-run (redirected to `report.txt`) stalled with zero
  output — `report.txt` stayed 0 bytes, `index.sqlite3`'s `partial_hash`/`full_hash` columns
  stayed at 0 rows, and no `reclaim` process was left running when checked. Root cause: the
  exact-duplicate hash pass (`dedup.py::find_duplicate_clusters`) had no progress logging and
  batched every `store_partial_hashes`/`store_full_hashes` SQLite write to the very end of each
  pass — so a long hash pass was genuinely indistinguishable from a hang, even by directly
  querying the index — plus no per-file read timeout, so one locked/slow file could wedge the
  whole run. The size-uniqueness prefilter (`_size_buckets` dropping singleton-size groups) was
  already correct and already in place; it was not the cause.
- Fix (commit `80344a2`): heartbeat log every ~5s during both hash passes; hash writes flushed
  every 500 files instead of one batch at the end; a `ThreadPoolExecutor`-based 30s per-file
  read timeout (`_hash_with_guard`) that turns a timeout or `OSError` into a recorded
  `HashSkip` instead of hanging or crashing; `apply` gained `--include-duplicates` (opt-in,
  default off) so the fast rule-detector report never has to pay for the hash pass unless
  explicitly requested. 10 new tests (`tests/test_dedup.py`, `tests/test_cli.py`); 161 unit
  tests + 2 evals pass; ruff/format/mypy clean.
- `.gitignore` gap closed: `data/*.sqlite3` doesn't reach nested subdirectories, so
  `data/real-disk-run/` (this run's 1.3GB index) wasn't actually ignored — added
  `data/real-disk-run/` explicitly before it could get committed by accident.
- Next: re-run the real-disk dry run with the fixed pipeline; watch for `dedup.progress`
  heartbeat lines and confirm `SELECT COUNT(*) FROM files WHERE partial_hash IS NOT NULL`
  actually increments during the run.

### 2026-07-16 — Second stall: candidate generation doesn't scale; SQL-pushdown rewrite
- Re-run hung again, this time *before* the hash pass: `tasklist` showed the `apply` process
  alive and burning CPU (21+ min, ~4.9GB RSS) with 0 rows written to `partial_hash`/`full_hash`.
  Root cause: `detectors.py::generate_candidates()` and `dedup.py::generate_duplicate_candidates()`
  each independently called `ScanIndex.candidate_inventory()` — a full-table load materializing
  every one of 3.1M rows into a `FileRecord` object (and two whole-inventory dicts,
  `InventoryContext`) before any detector or the dedup size-prefilter ran.
- Fix (ADR-0002, `docs/architecture/adr/0002-sql-pushdown-candidate-generation.md`): every rule
  detector and the dedup size-bucket prefilter now query `ScanIndex` directly via narrow,
  indexed methods (`get_record`/`record_exists`/`files_by_name`/`files_by_ext`/
  `files_larger_than`/`files_matching_path_pattern`/`duplicate_size_candidates`) instead of
  iterating an in-memory copy of the whole table. New `name`/`path_lower` indexed columns +
  migration for pre-existing DBs (streamed backfill, never `fetchall`). `InventoryContext`/
  `build_inventory_context` deleted outright (dead code once nothing called them).
  `generate_candidates()`/`generate_duplicate_candidates()` kept their exact external
  signatures — `cli.py`/`api/service.py` needed zero changes.
- Verified: every hot query hits `SEARCH ... USING INDEX` (never `SCAN`) via `EXPLAIN QUERY
  PLAN` tests that capture the *actual* SQL each method issues
  (`sqlite3.Connection.set_trace_callback`), so the test can't drift from the implementation.
  A 500K-row synthetic-index eval (`evals/test_candidate_generation_perf.py`) measured peak
  Python memory delta at **1.16MB** (vs. several hundred MB a full materialization would cost).
  The pre-existing golden-fixture eval (`evals/test_candidate_generation.py`, written and
  passing against the old `InventoryContext` implementation) passes unmodified — parity
  evidence without needing to keep the deleted implementation around to diff against.
- Independent Haiku verifier re-ran everything from scratch (178 unit tests, 11 evals,
  ruff/format/mypy, grepped for lingering `candidate_inventory()`/`full_inventory()` calls in
  detectors/dedup, spot-checked the `.tar.gz`-vs-bare-`.gz` archive-pair edge case by reading
  code) — all pass, sign-off clean.
- Honest limits (documented in ADR-0002, not silently swept under the rug): archive-pair fuzzy
  matching and the downloads/log substring checks still run in Python (over the already-SQL-
  narrowed set only); `files_matching_path_pattern` doesn't escape literal `%`/`_` in patterns
  because an `ESCAPE` clause empirically kills SQLite's LIKE-index optimization — mirrors
  `fnmatch`'s own lack of an escape mechanism, not a new regression against default config.
- Next: re-run the real-disk dry run again with both fixes in place; this time watch process
  CPU *and* `partial_hash`/`full_hash` row counts together to confirm progress through both
  candidate generation and the hash pass.

### 2026-07-17 — Third stall: dedup still collected every candidate before hashing any
- Re-run hung a third time, now inside `find_duplicate_clusters` itself. Diagnosis via
  `Get-Process`/`tasklist`: single thread, zero disk I/O, ~78MB flat memory, climbing CPU — it
  hadn't reached the multi-threaded hashing loop at all. Root cause, confirmed by directly
  querying the live index: **2,485,410 of 3,116,478 files (80%!) share a size with at least one
  other file** on this real `C:\`. The size-uniqueness prefilter (correctly SQL-pushed per
  ADR-0002) barely narrows anything in practice here — `find_duplicate_clusters` still
  collected the *entire* `duplicate_size_candidates()` stream into one
  `dict[size, list[FileRecord]]` (via `_group_by_size`) before hashing a single file, so Python
  object materialization cost simply moved from "the whole index" to "the whole duplicate-size
  candidate set" (millions either way).
- Fix: `index.py`'s `duplicate_size_candidates()` gained `ORDER BY size` (confirmed via EXPLAIN
  QUERY PLAN to cost nothing extra — the size index already visits rows in that order) plus a
  cheap `duplicate_size_candidate_count()` for an up-front heartbeat total. `dedup.py`'s
  `find_duplicate_clusters` now processes one size bucket at a time via
  `itertools.groupby(index.duplicate_size_candidates(), key=...)` — partial-hash, then
  full-hash the survivors, immediately per bucket — so peak memory is bounded by the *largest
  single bucket*, not the total candidate count. `_group_by_size` (now dead) deleted along with
  its 2 tests.
- First eval draft (every file in a bucket sharing one fake hash) failed at 335MB — a genuinely
  useful failure: it revealed the draft was measuring "cost of returning every true duplicate
  found" (legitimate, unavoidable), not "cost of collecting a bucket before hashing it" (the
  actual bug). Redesigned with exactly one real duplicate pair per ~80K-record bucket (matching
  how same-size-but-different-content files actually fragment on a real disk) — passes at
  119.39MB against a 150MB ceiling, with `dedup.progress` heartbeat lines confirmed visibly
  incrementing (`buckets_seen`/`partial_hashed`/`full_hashed`/`clusters_found`) throughout the
  run.
- Independent Haiku verifier re-ran everything, and specifically checked the one correctness
  risk this refactor could have silently introduced: `itertools.groupby` only groups
  *contiguous* equal keys, so it's only correct if its input is sorted by size — confirmed
  `ORDER BY size` is really in the SQL text (not just assumed), which is what makes the
  groupby-based bucketing behaviorally identical to the old whole-table version rather than a
  silent correctness bug (splitting one true size-group into two if rows ever arrived
  non-contiguously).
- Next: re-run the real-disk dry run a third time. Given the 80% collision finding, expect the
  hash pass itself to take a genuinely long time (this is real disk-I/O-bound work now, not an
  artifact of a bug) — the heartbeat is the thing to watch, not a specific expected duration.

### 2026-07-17 — Materiality gate: the 80% collision finding was mostly noise
- Checked the live index during the third re-run per the debug playbook (`SELECT size,
  COUNT(*) ... GROUP BY size ORDER BY c DESC LIMIT 20`): the collision list was dominated by
  333,135 zero-byte files, 144,734 files at 4096 bytes, then a long tail of tiny sizes (17,
  110, 4, 111, 41, 83, 2 bytes). Even in the best case (every member an exact duplicate), a
  bucket of e.g. 11,018 files at 17 bytes could only ever reclaim ~183KB — and for files under
  the partial-hash whole-file threshold (128KB), a "partial" hash reads the entire file
  anyway, so there's no cheap-peek savings for tiny files either. The pipeline was about to
  spend real disk I/O on millions of files that could never yield material savings.
- Fix: a materiality gate on duplicate detection. `config.categories.duplicates
  .min_reclaim_bytes` (default 1MB) — a size bucket's theoretical best-case reclaim,
  `(member_count - 1) * size`, must clear this floor before a single file in it is even
  queried, let alone hashed. Pushed into the SQL itself (`index.py`'s
  `duplicate_size_candidates()`/`duplicate_size_candidate_count()` gained a required
  `min_reclaim_bytes` param, added to the existing `HAVING COUNT(*) >= 2` clause — confirmed
  via EXPLAIN QUERY PLAN to still hit `SEARCH ... USING INDEX`, never `SCAN`, with the
  materiality arithmetic present). New `immaterial_duplicate_bucket_stats()` reports what was
  excluded and its *theoretical* (never measured — labeled as an upper bound, not a real
  number) reclaim size, surfaced in the CLI report rather than silently dropped.
- Real bug found and fixed along the way: `api/service.py` had two separate call sites to
  `find_duplicate_clusters`/`generate_duplicate_candidates` (one for the candidate list, one
  for the UI's cluster-detail view) — only one was config-driven before this change forced the
  question. Both now pass `state.config.categories.duplicates.min_reclaim_bytes` explicitly, a
  latent inconsistency that would have surfaced as "candidate is Tier B duplicate but its
  detail view shows no cluster" the moment materiality gating existed.
- `ScanIndex`-level methods take `min_reclaim_bytes` as a *required* keyword-only param (no
  default) — policy value belongs in `dedup.py`/`config.py`, not silently defaulted in the
  data-access layer. Every existing small-fixture test that exercises dedup correctness
  (not materiality itself) now explicitly passes `min_reclaim_bytes=0` to opt out, rather than
  silently breaking against the new 1MB default — touched `test_dedup.py`, `test_api.py`,
  `test_cli.py`, `evals/test_dedup.py`, `evals/test_candidate_generation_perf.py`.
- Verifier specifically checked the reclaim-bytes arithmetic (`(member_count - 1) * size`, not
  `member_count * size` — the kept copy's own size is never "reclaimable") and the
  `api/service.py` dual-call-site fix against `git diff`, not just the claim. 184 tests + 6
  evals pass, ruff/mypy clean.
- Next: re-run the real-disk dry run a fourth time. Expect the hash pass to now skip the huge
  zero-byte/tiny-file noise entirely and spend its time only on buckets with real reclaim
  potential — `report.txt` should show the materiality-excluded stats plus whatever real
  duplicate clusters exist above the 1MB floor.

## Gotchas discovered
- `uv init --package` created a `reclaim = "reclaim:main"` script entry pointing at a stub
  `main()`; repointed to `reclaim.cli:main` (placeholder) since Stage 2+ will define the real
  CLI surface.
- Default git branch from this git version is `master`; renamed to `main` before first
  commit per house rule 35 (only needed once, first commit hadn't landed yet).
- `os.scandir`'s `DirEntry.stat()` does not populate `st_dev`/`st_ino` on Windows — only a
  direct `Path.stat()`/`os.stat()` call does (via `GetFileInformationByHandle`). Anything
  needing hardlink identity must stat the real path, not trust the scandir-cached stat.
- pytest's `tmp_path` fixture resolves under the OS `%TEMP%` directory — any future eval/test
  that needs to distinguish "under Temp" from "not under Temp" paths must root its own fixture
  tree elsewhere (e.g. `data/_test_scratch/`), or every fixture path will spuriously match.
