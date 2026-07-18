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

### 2026-07-17 — Fourth stall: `direct_children()`'s LIKE query was a full scan all along
- Re-run still took ~80 minutes of CPU with a single thread, zero disk I/O, flat ~78MB memory
  — not the memory-scaling bug (already fixed), something else CPU-bound. Wrote a diagnostic
  script that directly timed each of the 7 rule detectors + the 2 materiality queries against
  a read-only copy of the live 3.1M-row index: `detect_archive_pairs` alone took **1309.19s
  (21.8 min)**; everything else combined took under 7s.
- Root cause: `detect_archive_pairs` calls `ScanIndex.direct_children(parent)` once per
  archive-extension file (6,793 of them on this disk, many sharing parents — e.g. 455 files in
  one `gradio/.../chunks` dir from various venvs/uv caches) to find sibling directories for the
  fuzzy-match check. `direct_children()`'s SQL used `path LIKE ? ESCAPE '\'` — and an `ESCAPE`
  clause unconditionally defeats SQLite's LIKE-to-index-range-scan optimization (the same
  SQLite behavior already found and documented for `files_matching_path_pattern` — confirmed
  again via `EXPLAIN QUERY PLAN`: bare `SCAN files`, not `SEARCH ... USING INDEX`). Every call
  was a full 3.1M-row scan, measured at ~1.5s each.
- Fix: new `_prefix_range(prefix)` helper returns `(prefix + "/", prefix + "0")` as
  `(lower, upper)` bounds for `path >= lower AND path < upper` — `'0'` (0x30) is the ASCII code
  point immediately after `'/'` (0x2F), a standard index-friendly prefix-range trick needing
  *no escaping at all* (a plain BINARY-collation range comparison treats every character
  literally). Rewrote `direct_children`/`subtree_size_bytes`/`_query_inventory`/
  `load_stat_cache`/`load_hash_cache` to use it for their primary "under this path" bound.
  `direct_children`'s residual "exclude grandchildren" check stays LIKE-based (no clean range
  equivalent) but now only filters the already-narrowed range-scanned rows. A latent
  pre-existing bug fixed as a side effect: these methods previously passed the *escaped*
  prefix to both the exact-match (`path = ?`) and the LIKE pattern, meaning an exact match
  would never have worked for a real path containing literal `%`/`_` (which this disk has:
  `.../immutable/_app`) — now both use the plain unescaped prefix, correctly.
  `tests/test_index.py` gained EXPLAIN QUERY PLAN tests for all 5 rewritten methods plus a
  correctness regression test reproducing the exact real-disk `_app` scenario.
  Measured against the real index: `detect_archive_pairs` 1309.19s -> 3.78s (346x); all 7
  detectors + 2 materiality queries now complete in well under 15s combined. Verifier signed
  off, specifically re-deriving the prefix-range boundary math (confirmed `/` = 0x2F, `0` =
  0x30) rather than trusting the claim.
- Next: re-run the real-disk dry run a fourth time. Candidate generation should now be fast
  (~15s); the dedup hash pass (137,001 files after the materiality gate) is genuine disk-I/O-
  bound work — the heartbeat is what to watch, not a specific expected duration.

### 2026-07-17 — Mechanical guard against the ESCAPE-defeats-index bug recurring
- The `LIKE ... ESCAPE`-unconditionally-defeats-index bug hit twice (once caught at design time
  for `files_matching_path_pattern`, once shipped and measured on the real disk for
  `direct_children`) — added a guard so a third occurrence can't ship silently.
- New `tests/test_query_plan_coverage.py`: discovers every `ScanIndex` method whose source
  references `self._conn.execute` via `inspect.getsource` (not a hand-maintained list), and
  `test_every_sql_issuing_method_is_classified` fails if any discovered method has no entry in
  a `_CASES` registry — so a *new* query method with the bug baked in fails CI immediately
  (forced to be classified, which forces the SCAN-vs-SEARCH question to be asked), rather than
  silently shipping uncovered. Each entry is `expect_index=True` (must show
  `SEARCH ... USING INDEX`/index-assisted `SCAN ... USING (COVERING) INDEX`, never a bare
  `SCAN files`) or `expect_index=False` with a named justification, itself verified to still
  show a genuine bare scan (catches a stale exception left over from a design that's since
  been fixed and should be promoted). A second check, `test_no_unmarked_like_escape_in_query_
  layer`, statically greps `index.py` for the executable pattern `LIKE\s*\?\s*ESCAPE` (tuned to
  avoid false-positiving on this file's own docstrings discussing the bug) and fails unless a
  `LIKE-ESCAPE-OK:` marker comment justifies it — added to `direct_children`'s one remaining
  legitimate use (a residual filter over an already-range-scanned row set).
- Validated both checks actually catch the bug: temporarily reintroduced the exact regression
  (`files_by_ext` switched from indexed `IN (...)` to `LIKE ? ESCAPE`), confirmed both new tests
  failed as expected, then restored from a backup and reran clean. Along the way, corrected two
  of my own initial classifications after seeing the *real* query plans rather than assuming:
  `candidate_inventory()` (even with `under=None`) and `_backfill_name_and_path_lower`'s
  `IS NULL` check both turned out to already use an index (`is_cloud_placeholder`/name+
  path_lower respectively) — SQLite is more capable here than the naive assumption "no WHERE
  clause or IS NULL means no index can help."
- Verifier independently re-ran EXPLAIN QUERY PLAN itself (not just trusted the claim) for the
  trickiest cases (`has_any_records`'s COVERING INDEX scan, `immaterial_duplicate_bucket_stats`'s
  subquery scan) and confirmed the marker/lookback-window math. 213 tests + 2 intentional skips
  pass, ruff/mypy clean.
- Next: still waiting on the real-disk re-run from the previous checkpoint (unaffected by this
  test-only change — background process confirmed untouched throughout).

### 2026-07-17 — Fifth stall: `_drop_nested_candidates` was O(candidates * kept_dirs * depth)
- Checked on the still-running real-disk apply (unrelated to the mechanical-guard work above):
  still 0 rows hashed after 1h35m. A diagnostic script timing each stage of
  `generate_candidates()` found `_run_all_detectors` completes in 3.42s with 42,185 raw
  candidates (dominated by many sibling, non-nested `__pycache__`/dev-artifact directories —
  separate Python packages don't nest their bytecode caches inside each other), but the very
  next stage, `_drop_nested_candidates` — pure Python, no SQL at all — ran 3.5+ minutes without
  finishing (confirmed alive via `tasklist`: climbing CPU, not hung).
- Root cause: `any(directory in candidate.path.parents for directory in kept_dirs)` re-scanned
  the *entire* `kept_dirs` list for every candidate — O(candidates * kept_dirs * depth). This
  only shows up once `kept_dirs` itself grows large (many non-nested directory candidates,
  exactly this real-disk shape); no prior test/eval fixture ever used more than a few dozen
  candidates.
- Fix: `kept_dirs` is now a `set[Path]` (not `list[Path]`); the check inverts to
  `any(ancestor in kept_dirs for ancestor in candidate.path.parents)` — O(1) hash lookup per
  ancestor instead of an O(kept_dirs) scan, making the whole pass O(candidates * depth).
  `Path.__eq__`/`__hash__` is case-insensitive on Windows (`os.path.normcase`), so the `set`
  preserves the exact equality semantics the old `list`-based `in` check had. New eval
  (`evals/test_candidate_generation_perf.py`) constructs 40,000 non-nested sibling directories
  (the old algorithm's worst case) plus a genuinely-nested pair, asserting both correctness and
  a <5s ceiling. Measured against the real disk: 3.5+ minutes (never finished) -> 1.55s; full
  candidate generation (detectors + drop-nested + per-candidate safety/size lookups) now totals
  ~12.4s end to end.
- Verifier independently constructed two of its own adversarial cases (a 20-level-deep chain
  mixed with 5,000 siblings; a single 30-level chain) neither of which was in the delivered
  test, confirming both correctness and speed under stress the specific test didn't cover.
  213 tests + 2 intentional skips pass, ruff/mypy clean.
- Next: re-run the real-disk dry run a fifth time. Five real bottlenecks found and fixed in
  this chain now (whole-table materialization, whole-candidate-set materialization before
  hashing, an O(n) materiality-blind hash pass, ESCAPE-defeated indexes, and this O(n²) nested-
  candidate pass) — each only became visible at real-disk scale, never at fixture scale.
- Sixth (successful) real-disk dry run: scan 3,117,471 entries in 239.84s; dedup 137,492
  candidates -> 27,486 clusters, 97,724 immaterial buckets excluded (materiality gate working),
  563 hash-unreadable skips (all benign permission-denied on locked system files); apply
  [DRY-RUN] processed=57,373 succeeded=57,373 failed=0, bytes_freed≈212.4GiB. First-ever clean,
  complete real-disk report — closes the original stall-diagnosis ask from earlier in this
  session.

### 2026-07-17 — ADR-0003: model-cache category + cost-aware size guard
- Real-disk report showed `.cache/huggingface/hub` (124.9GB) classified `package_cache` ->
  `retention=none` -> permanent delete — wrong: re-acquiring 100+GB is not the same recovery
  cost as `npm ci`, and gated/private/fine-tuned/manually-pushed models may be unrecoverable.
- New `model_caches` category (HuggingFace hub, torch hub, Ollama models, plus
  `*.safetensors`/`*.ckpt`/`*.bin` scoped to those roots): defaults to vaulted 30-day retention
  (never direct-delete) and hardcoded `Tier.B` (falls out of the existing tier formula for free
  once `suggested_tier=Tier.B` is set at the detector — never auto-quarantine-eligible
  regardless of `enabled`).
- General cost-aware guard in `apply_batch`: any `retention_days=None` candidate at/above
  `config.safety.direct_delete_size_guard_bytes` (default 1GB) downgrades to vault with its own
  retention window, regardless of category — protects every direct-delete category, not just
  model caches.
- `Candidate`/`RawCandidate` gain `recovery_cost_note`, surfaced in the CLI report and dashboard
  alongside `rebuild_instruction`. Verifier signed off (adversarial-tested the 1GB boundary,
  confirmed guard-vaulted items restore correctly since `restore_batch` keys off `entry.method`
  not `retention_days`); 222 tests + evals pass, ruff/mypy clean. Committed `b286509`.
- New `--include-categories` CLI flag on `reclaim apply`: fine-grained category filter narrowing
  an already tier/root-filtered selection — needed because group-level `enabled` flags (e.g.
  `dev_artifacts`) can't isolate one category (pycache) from its siblings (node_modules,
  dist_output) within the same apply run. Verifier signed off; committed `ae550ff`.

### 2026-07-17 — First real-disk scoped apply: 6 categories, 10.8GB freed, 2 bugs found
- Scoped apply (`windows_temp`, `package_cache`, `crash_dump_file`, `crash_dump_wer_report`,
  `browser_cache`, `dev_artifact_pycache`; explicitly excluding `exact_duplicate`/`model_caches`/
  `dev_artifact_node_modules`/`dist_output`/`archive_pairs` for later reviewed applies), after
  user confirmation given the size guard would vault ~40.5GB of the ~53.5GB total rather than
  direct-delete it.
- Result: 23,815 processed, 23,572 succeeded, 243 failed. Real measured disk-free delta ~10.8GB
  (manifest: 23,565 direct_delete entries / 9.79GiB + 7 vault entries / 36.07GiB). Restore
  mechanism pre-validated via 3 throwaway files (byte-identical) — did NOT restore-test a real
  vaulted item since `restore_batch` operates at whole-batch granularity (would have undone all
  7 real vault gains just to prove what the throwaway test already proved).
- Two bugs discovered from real production data, both root-caused and fixed same day — see the
  ADR-0004 entry below and the size-guard-exemption entry after it.

### 2026-07-17 — ADR-0004: long-path-safe, atomic-or-nothing vault/restore moves
- Bug 1: one guard-vaulted directory (`Temp\claude`, 5,064 dirs/27,403 files) failed with
  `WinError 3` mid-move. Root cause: the vault destination path is always longer than the
  source (adds a batch dir + UUID prefix), pushing already-near-260-char nested paths over
  Windows' legacy MAX_PATH; `shutil.move` fell back to `copytree`+`rmtree`, `copytree` failed
  partway, and — since `rmtree(src)` only runs after `copytree` succeeds — the source stayed
  fully intact (verified: all 5,064/27,403 present) but left an ORPHANED PARTIAL copy (1,705/
  3,838) in the vault with no manifest entry, silently wasting ~673MB. Manually cleaned up;
  no data was lost, but the mechanism needed hardening before the much-larger duplicates/
  model-cache apply (124.9GB HF hub, same failure mode at bigger scale).
- Empirically confirmed on this machine (no `LongPathsEnabled`): a bare `os.makedirs`/`open()`
  fails past 260 chars unprefixed, succeeds with a `\\?\` extended-length prefix.
- Fix: `_long_path()` helper (`\\?\`-prefixed absolute string; `pathlib.Path` doesn't reliably
  round-trip that prefix, so this section deliberately uses `os.*`/string paths throughout, not
  `Path` methods — every PTH-rule noqa here is intentional). New `_atomic_move(src, dst,
  is_dir)` replaces raw `shutil.move` in both `apply_batch`'s vault branch and `restore_batch`'s
  move-back: tries atomic `os.rename` first (now almost always usable since both paths are
  long-path-safe), falls back to copy-verify-delete only if rename raises, verifies file-count/
  total-bytes parity before ever removing the source, and owns creating/cleaning-up `dst`'s
  parent directory (an empty batch-dir shell made just for one failed item is removed too,
  never left as debris — a shared parent with successful siblings is left alone).
  `VaultIntegrityError` (new) is caught by the same per-item handler as any other filesystem
  error. Tests: a >260-char deep-tree fixture proving vault→restore round-trips byte-identical
  (the old throwaway-file test only proved short paths); two injected-failure tests (copytree
  raises partway; copytree returns normally but silently incomplete) both proving source stays
  untouched, vault gets zero orphaned bytes/dirs, and the item lands in the failed list.
- Bug 2 (same real apply, unrelated mechanism): `detect_crash_dumps` proposes a `.dmp` file
  both by extension (anywhere) and as a CrashDumps/WER root's direct child — two
  `RawCandidate`s for the same path, two different categories. No data loss (the file gets
  deleted either way) but whichever candidate applies second finds it already gone and is
  recorded as a spurious failure — explains all 10 of the real apply's `crash_dump_wer_report`
  "failures" (the same 10 files as 10 of the 12 `crash_dump_file` successes). Fix: new
  `_dedupe_by_path` (first-seen wins) runs in `generate_candidates` right after combining all
  detectors' output — general/detector-agnostic, not a `detect_crash_dumps`-specific patch.
- 235 tests pass (12 new), ruff/mypy clean.

### 2026-07-17 — ADR-0003 addendum: package caches exempt from the size guard
- The scoped apply's real numbers showed the cost of the ADR-0003 size guard concretely: 33.3GB
  of `package_cache` (uv 14.3GB, pip 11.8GB, gradle 7.2GB) vaulted instead of direct-deleting,
  purely because each cleared 1GB — but a package-manager cache is exactly as cheap to rebuild
  at 20GB as at 20MB (deterministic re-fetch of public artifacts), so gating its permanence on
  size was the wrong axis for this specific category.
- `PackageCachesConfig.size_guard_exempt: bool = True` (new); `Candidate.size_guard_exempt:
  bool = False` (new), resolved in `generate_candidates` via a new
  `_CATEGORY_GROUP_SIZE_GUARD_EXEMPT_GETTERS` dict mirroring the existing retention/enabled
  getter dicts — hardcoded `False` for every other group. `_effective_method_and_retention_days`
  skips the guard entirely when exempt, regardless of size. Also added the default Yarn cache
  path (previously not covered by any default) since it was named explicitly in scope.
- Tests: a 20GB `uv`-cache-style fixture direct-deletes; a 5GB `model_cache`-style fixture still
  vaults (exemption is category-scoped, not a blanket bypass). Full suite green, ruff/mypy clean.
- Next: dispatch verifier for ADR-0004 + the size-guard exemption together, then commit both.
  After that, the deferred categories (`exact_duplicate`, `model_caches`, `dev_artifact_
  node_modules`, `dist_output`, `archive_pairs`) get their own reviewed, scoped apply — now with
  the vault-move robustness fix in place before the 124.9GB HuggingFace hub tree goes through it.

### 2026-07-17 — restore_batch's whole-batch-refusal blocked the exemption from being usable
- Tried to re-apply `package_cache` alone with the exemption in place; the pip/uv/gradle
  vault copies from the prior scoped apply were still parked in the vault (not restored, not
  purge-eligible for ~30 days), so a plain re-scan found nothing there to re-detect — only
  `uv/cache`'s own auto-regenerated 175MB (real, working as advertised) showed up.
- Tried `reclaim undo` on the prior batch to bring the 3 package_cache + 4 windows_temp vault
  entries back to their original locations so the fixed pipeline could re-process them —
  `restore_batch` refused the WHOLE call, because that batch_id also has 23,565
  `direct_delete` entries sharing it (one `apply_batch` call, ADR-0001's per-candidate
  retention override), and the function raised `DirectDeleteRestoreImpossibleError` for the
  entire batch_id the instant ANY entry in it was `direct_delete` — not just the ones that
  are actually unrestorable. This made restoring vault entries from a mixed batch structurally
  impossible with the tool as built, independent of anything about package_cache specifically.
- Fix: `restore_batch` now partitions entries into vault/direct_delete/recycle_bin. Zero vault
  entries (pure non-vault batch) still raises the same loud whole-call refusal as before —
  unaffected, still tested. At least one vault entry restores every vault entry normally and
  reports each direct_delete/recycle_bin entry per-item as `restore_unsupported=True` with a
  descriptive message, never raising for those. New `RestoreReport.files_unsupported`;
  `files_failed` redefined to mean "genuinely attempted and failed," not "didn't succeed for
  any reason" — a mixed batch where everything restorable succeeds now reports
  `files_failed=0`/exit 0. CLI/API updated to match (`can_restore` in the quarantine listing
  now only blocks when there are zero vault entries, not whenever there's any non-vault one).
  Verifier (after one retry due to a mid-run session token-limit reset) confirmed 236 tests
  pass, all three method combinations (pure direct_delete, pure recycle_bin, every mixed
  combination including all-three-at-once) behave correctly, idempotency holds, and
  `files_failed`/`files_unsupported` never conflate a real failure with a clean skip. Committed
  `49d2e2b`.
- Real restore: 6 of 7 vault entries restored (`uv/cache` correctly refused — its own
  auto-regenerated copy already occupies the original path, never clobbered). Rescan, dry-run
  confirmed 0 size-guard downgrades for `package_cache` (exemption verified working against
  real data), real apply: `package_cache` 3/3 items succeeded via `direct_delete`, 19.2GB
  (pip 11.8GB + gradle 7.2GB + uv 175MB). Overall batch: processed=992, succeeded=762,
  failed=230, bytes_freed=25.9GB. Real measured disk-free delta: 20.3GB (this apply) — total
  since the original 6-category apply began: independently measured before=178.17GiB,
  final=190.82GiB, net +12.6GiB freed across the whole restore+redo sequence (on top of the
  10.8GB already freed by the first apply, since some of what's freed now was already
  vaulted-not-deleted before and is now genuinely gone).

### 2026-07-17 — ADR-0004 addendum: read-only git-object files broke rmtree/unlink cleanup
- The real re-apply above hit ANOTHER new bug live: `Temp\claude` (this session's own
  actively-in-use scratch directory, full of cloned git repos) failed with a genuine
  parity-mismatch (a real concurrent-modification race — the tree changed mid-copy, since it
  was being actively written to by this very session throughout the run; unrelated to
  path-length or read-only files) — but the ADR-0004 cleanup that ran afterward left 42
  files/41MB of read-only git packfiles/loose-objects behind as genuinely orphaned vault
  debris, because `shutil.rmtree(..., ignore_errors=True)` silently gives up on read-only
  files on Windows instead of raising or retrying (a well-known stdlib gotcha, not exotic to
  this codebase) — exactly the failure mode ADR-0004 was written to prevent, just via a
  different root cause (read-only attribute, not MAX_PATH) than the one it originally fixed.
- Fix: `rmtree_clear_readonly` (a `shutil.rmtree` `onexc` callback, Python 3.12+) clears the
  read-only attribute and retries; used by every `shutil.rmtree` call in executor.py and
  purge.py instead of `ignore_errors=True`. Cleanup now logs a warning
  (`executor.vault_cleanup_incomplete`) if it's still incomplete afterward (e.g. a genuinely
  locked file from another live process) rather than silently swallowing. A first verifier
  pass caught a second instance of the SAME defect class I'd missed: `purge.py`'s
  non-directory (single-file) branch had NO error handling at all for a read-only vaulted
  file. Fixed with a parallel `unlink_clear_readonly` helper (no `onexc`-style hook exists for
  a standalone `os.unlink`, so the retry is wrapped manually) applied to every single-file
  deletion across `_atomic_move`'s cleanup and success paths, `apply_batch`'s direct_delete
  branch, and `purge.py`'s single-file branch. purge.py also gained the same long-path
  prefixing (`_long_path` made public as `long_path`) every other vault operation already had
  — it had none before, same MAX_PATH exposure as the original bug.
- Two independent verifier passes: the first found the purge.py single-file gap; the second
  (after the fix) confirmed all 4 affected call sites handle read-only files correctly, with
  Windows-empirically-confirmed `PermissionError` semantics, and re-ran every original
  ADR-0004 regression test to confirm no disturbance. Manually cleaned up the real 41MB
  orphaned debris this bug had left behind (safe: unlinked from any manifest entry, source
  fully intact). 241 tests pass, ruff/mypy clean. Committed `6ea84be`.
- `Temp\claude` itself remains un-vaulted (2.37GB) — its failure this run was a genuine
  concurrent-modification race (the directory is this session's own live scratch space, still
  being written to throughout this very conversation), not a defect this fix addresses; not
  re-attempted this session since doing so carries the same race risk while this session
  remains active.
- Next: the deferred categories (`exact_duplicate`, `model_caches`, `dev_artifact_
  node_modules`, `dist_output`, `archive_pairs`) get their own reviewed, scoped apply — the
  vault mechanism has now been hardened against MAX_PATH, atomicity/parity, and read-only-file
  failures across two rounds of real-production-data discovery before the 124.9GB HuggingFace
  hub tree (blobs/snapshots symlink structure — ADR-0004 follow-up still needed, see the
  explicit hold below) goes anywhere near it.
- **Hold, explicit user instruction:** do NOT run the `model_cache`/HuggingFace-hub apply
  until link-structure handling (symlinks/hardlinks in `blobs/`/`snapshots/`) is verified —
  a copytree-based vault move may dereference links (inflating far past 124.9GB) or flatten
  the link topology (restore yields a tree the HF library can't load), and byte/count parity
  alone can pass while the result is functionally broken. Required before that apply: detect
  the real link type on this machine, decide a preserve-links policy, extend the parity check
  to assert link-structure (not just bytes), and a deep+linked fixture proving a vault→restore
  round-trip reproduces the topology (a smoke check resolving one snapshot file through its
  link to its blob). Not started — model_cache stays review-only, never applied, until this
  lands.

### 2026-07-18 — Security audit + single-user/technical-reviewer packaging pass
- Scope explicitly narrowed by GG mid-session to Stage 1 only: security-harden + installable for
  self/technical reviewers. Safe-mode-by-default and a signed public installer are Stage 2,
  pending a separate go-public decision — not attempted this pass.
- Item 0 (ML/AI positioning check): grep-confirmed zero code references anywhere in `src/reclaim`
  to any Phase-2 similarity component named in `reclaim-spec.md` (pHash/dHash, CLIP embeddings,
  MinHash/SimHash) — the running app is 100% rules-first, exact BLAKE3 hash dedup only. Worth
  restating for anyone reading the spec cold: Phase 2's ML section describes a future stage, not
  current behavior.
- **Top finding — filename-driven XSS in the Review Queue's duplicate-cluster table**
  (`renderClusterTable`, `app.js`): built each row via `row.innerHTML` with an unescaped
  `member.path`. Since the dashboard now carries its own CSRF token in the page (this same
  pass), a script executing via this XSS could have read that token and called
  `/api/apply`/`/api/restore` directly — a display bug that was one step from a delete primitive.
  Fixed to build every cell via `textContent`/DOM APIs; `tests/frontend/xss.test.mjs` (jsdom,
  `node --test`, new CI job) feeds the real render function `<img onerror>`/`<script>` payloads
  and asserts zero markup elements survive. Full detail and the "why this is worse than the
  eleven engine bugs" argument: `docs/CASE_STUDY.md`'s new Security audit section.
- Four more findings, each fixed and tested, not just documented: hard loopback-only `--host`
  gate (argparse-level, `127.0.0.1`/`::1` only — `localhost` deliberately excluded too, since
  it's a DNS/hosts-file lookup); per-process CSRF token + Host/Origin DNS-rebinding guard on
  every mutating `/api/*` call (`reclaim.api.security`); a manifest-integrity/zip-slip-equivalent
  guard on `restore_batch` (refuses the whole restore if a vault entry's `vault_path` escapes the
  configured vault dir or `original_path` matches a protected root — new `RestoreIntegrityError`);
  a no-elevation guard (`reclaim.elevation`) refusing every mutating command if the process holds
  an elevated Windows token. `pip-audit` added to CI (zero vulnerabilities found in current
  locked deps).
- Packaging: `pyproject.toml`'s existing `reclaim = "reclaim.cli:main"` entry point was already
  `uv tool install`-ready — verified end to end (`uv tool install .` → real `reclaim` executable
  on PATH → `reclaim scan` against a real directory). Added `reclaim dashboard` (serve + auto-open
  browser) as the one-command launch path. README rewritten for the install target (uv
  tool/pipx install, first-run, restore, security posture, explicit distribution-status
  boundaries).
- Nuitka one-folder standalone build: attempted and verified, not just documented. `--standalone`
  (not `--onefile`, per the "one-folder" ask) against this project's own dev venv (which pulls in
  the full `--all-groups` closure, not just runtime deps — `dist/cli.dist/` came to 121MB,
  ~858 C files compiled via an auto-downloaded MinGW64 since no MSVC was present) produced a
  working `reclaim.exe`: `--help` and a real `scan` against a scratch directory both verified.
  Unsigned — AV/SmartScreen false-positive is expected and documented, not chased. `dist/` added
  to `.gitignore`.
- 6 atomic commits (`e3dbe34`..`431deac`): XSS fix, CSRF/Origin guard, restore-integrity guard,
  CLI hardening (bind/elevation/dashboard, bundled — cli.py accumulated all three and splitting
  further via manual hunk surgery wasn't worth the risk), CI jobs, docs. Independent Haiku
  verifier re-ran the full suite + spot-checked every fix against the source (not just the diff)
  before commit: 14/14 checks passed. Final state: 327 tests pass (2 skipped, both pre-existing
  non-Windows skips), 95% coverage, ruff/mypy clean, eval suite (including the Stage 1 safety hard
  gate) green.
- **Correction, mid-session**: accidentally ran the CI workflow's `git config --global user.email/
  user.name` step (fixture identity, meant for an ephemeral CI runner) directly against GG's real
  global git config. Caught within the same turn before any commit was made under the wrong
  identity; restored to `gaurav.gandhi2411@gmail.com`/`Gaurav Gandhi` immediately. No commits,
  pushes, or repo state were affected — logged here per house rule 53 (honest about failures).
- Next: the deferred `model_cache` apply (link-structure hold from the 2026-07-17 checkpoint)
  and Stage 2 (safe-mode default, first-run disclaimer, signed public installer) both remain
  open, pending GG's go-public call on the latter.

### 2026-07-18 — git_guard.py: closing the accidental --global mutation gap
- Follow-up requested after the mid-session identity mishap above: `scripts/git_guard.py` now
  refuses any `git config --global`/`--system` invocation routed through it unless
  `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` is set, or `RECLAIM_GIT_SANDBOX_HOME` plus a redirected
  `HOME`/`USERPROFILE`. `eval.yml`'s fixture-identity step now routes through it with
  `GIT_CONFIG_GLOBAL` pointed at a runner-temp path — copying just the `run:` block to a local
  machine (without the `env:` block) now gets refused instead of repeating the incident.
  `evals/fixtures/build_golden_tree.py`/`tests/test_scanner.py` no longer call `git config` at
  all, even locally — identity is `-c`-scoped to the single `commit` invocation instead.
- 22 new tests (`tests/test_git_guard.py`), including an adversarial proof that a sandboxed
  write lands only in the redirected file and the real global `user.email` is provably
  unchanged before/after. Independent Haiku verifier re-ran the exact refusal scenario itself
  (not trusting the claim) and did a final real-identity integrity check before sign-off.
  Committed `0f2385e`.

### 2026-07-18 — Applied-AI layer kicks off: hard gates 1-3 complete
- New track, spec-driven by `reclaim-ai-features-spec.md` (committed as-is, `6d03cdd`). Work
  happens on `feat/ai-layer` — branch-only per explicit instruction, never merged past branch
  protection without GG's review. New ADR series continues at 0011 (deterministic-engine ADRs
  0001-0010 are a separate, already-shipped track).
- Build order requires the EvalHarness + the §7.5 adversarial safety eval to land and pass,
  independently verified, BEFORE any model is wired in — done first, against scaffolding only
  (no pHash/CLIP/etc. exists yet at this point). Gate 1: `src/reclaim/ai/` package
  (`AICluster`/`AIClusterMember`/`AIReviewQueue`, deliberately field-disjoint from
  `reclaim.models.Candidate`) + `eval_harness.py` (BCubed, PR-curve, provenance) +
  `evals/test_ai_safety_gate.py` (13 cases: AST import-graph scan, AttributeError-before-any-
  disk-io proof, two adversarial-config injection attempts rejected by pydantic
  `extra="forbid"`, reserved-namespace grep, construction-time invariants). Gate 2 (structural
  separation): same AST scan + `tests/test_ai_safety_reuse.py` proving the AI layer reuses the
  real `SafetyValidator`, not a reimplementation, against real fixture files. Gate 3 (optional
  extra): `pyproject.toml`'s `[project.optional-dependencies] ai`, lazy-guarded imports
  (`reclaim.ai._optional.require`), both install profiles tested
  (`tests/test_ai_optional_extra.py`), new CI job `ai-layer-with-extras`.
- Independent verifier pass on Gate 1 found one real gap before sign-off: the AST import-scan
  helper missed the `from reclaim import executor` form (only checked `ImportFrom.module`, not
  `module.alias` combinations) — fixed immediately with a regression test
  (`test_imported_module_names_catches_the_from_reclaim_import_executor_form`), re-verified,
  then committed. Verifier also independently re-derived §0/§6/§7.5 from the spec itself before
  judging the code against it, and tried its own adversarial case (an indirect re-export chain)
  — found the AST scan is per-file, not full transitive-closure, and documented that as a known
  limitation (mitigated by the type-level AttributeError proof, which holds regardless of how
  a stray reference to the executor might arrive).
- ADR-0011 records the architecture + verifies Feature 1a Track A's three new dependencies'
  licenses via real `importlib.metadata` (not memory): imagehash BSD-2-Clause,
  opencv-python-headless Apache-2.0, pillow MIT-CMU, all transitive deps (numpy/scipy/
  pywavelets) also permissive. Zero model weights bundled by this feature (pHash/dHash are
  algorithms, not learned weights), so the license table is exhaustive for it.
- All 386 tests green (was 327 before this session started), ruff/mypy clean on both install
  profiles. Committed `4c738e7`.
- Next: Feature 1a Track A (pHash/dHash near-identical clustering + classical keep-best
  scorer) — the first real feature, per build order.

### 2026-07-18 — Feature 1a Track A + gold-set labeling tool
- Feature 1a Track A shipped: `src/reclaim/ai/{phash,keep_best,image_similarity}.py` (pHash/
  dHash prefilter + Hamming union-find clustering + classical sharpness/resolution/exposure
  keep-best scorer, no NIMA — spec says add only if measured to beat classical, nothing did
  that measurement). `evals/ai_fixtures/build_image_similarity_fixtures.py` (deterministic,
  seed=42, no binary images committed) + `evals/test_ai_image_similarity.py` (PR-curve
  operating-point derivation at target precision >=0.95, BCubed clustering floor, keep-best
  safety metric + top-1 agreement, end-to-end safety-filtered proof). An early fixture
  revision zeroed out the resolution signal by resizing every variant back to identical
  dimensions (produced an arbitrary 0.667 top-1 agreement); fixed the fixture, not the
  scorer's weights — see CASE_STUDY's new AI-layer section for why that distinction mattered.
  ADR-0012 records the measured-but-provisional threshold (Hamming distance 2, precision 1.0
  on synthetic fixtures) and the CI gate's deliberately looser, margined value (10).
  Independent verifier constructed adversarial cases outside the test suite (1x1 pixel
  images, all-black/all-white, corrupted files, threshold-boundary sweeps) — all handled
  sanely. Committed `ae57723`.
- Gold-set labeling tool shipped (delivered per the autonomy boundary, NOT run against real
  photos): `src/reclaim/ai/labeling.py` (LabelStore, append-only JSONL; `discover_label_
  candidates` reuses the real Track A pipeline, not a separate implementation, at a looser
  default threshold so borderline cases get reviewed) + `src/reclaim/ai/labeling_app.py`
  (loopback-only FastAPI review UI reusing `reclaim.api.security` wholesale — same Host/
  Origin/CSRF guard as the main dashboard, not a lesser bar for "just a dev tool") +
  `scripts/ai_label_tool.py` (CLI launcher). ADR-0013.
  **Caught a real vulnerability in this feature's own first draft**, same class as the
  dashboard XSS fixed earlier this session: an inline `onclick="selectKeep('...', i, '...')"`
  handler with `html.escape()`-wrapped filenames interpolated into the JS string literal —
  HTML-escaping a quote doesn't protect a JS string literal in an inline event-handler
  attribute (the browser HTML-decodes the attribute before parsing it as JS). Fixed to
  data-*-attributes-plus-delegated-listener before any test was written against the
  vulnerable version.
  **Verified live, not just unit-tested**: launched the tool against a synthetic photo
  directory, drove it through chrome-devtools in a real browser end to end (select keeper,
  confirm, reload, confirm persistence and updated counts, reject a different cluster,
  confirm empty state), then confirmed via `curl` that the running server actually rejects a
  spoofed Host header and a missing CSRF token. Independent verifier separately constructed
  its own adversarial filenames and image-route inputs (path traversal, negative/non-numeric
  indices) against the fixed version. Committed `9656760`.
- 402 tests green (was 327 before this session's AI-layer work started), all 4 AI ADRs
  (0011-0013 plus the earlier architecture one) verified independently before landing. Full
  eval suite (deterministic safety hard gate + all AI evals) reconfirmed green as a final
  checkpoint. CASE_STUDY gained an AI-layer section.
- Remaining build order (not attempted this session, per "one feature at a time, report
  before next"): Feature 1b (MinHash doc near-dup + version-chain), Feature 1a Track B (CLIP
  semantic grouping, only after Track A's pipeline is proven — it is), Feature 2 (screenshot
  burst + OCR, privacy-locked), Feature 3 (feedback logging + LambdaMART ranker, label-gated).
  Real gold-set labeling (running `scripts/ai_label_tool.py` against GG's actual photos) is
  an explicit follow-up requiring GG's own time, not something this session can or should do
  on his behalf.

### 2026-07-19 — Labeling protocol audit, before real labeling started
- GG asked for the protocol to be confirmed against 4 statistical-soundness requirements
  BEFORE running it for real — sampling coverage of the full distance range (not just easy
  positives), a diagnosable keep-best label schema (WHY, not just WHICH), a volume/balance
  target, and commit-keyed/versioned persistence. Audited against the actual code, not memory:
  all four were real gaps, not just documentation gaps. Fixed, verified independently, then
  reported — per GG's explicit "don't change the tool unless a gap exists."
- `discover_label_candidates` now samples three independent strata (`near_duplicate` —
  unchanged existing pipeline; `boundary` — pairwise Hamming 11-25; `negative_control` —
  pairwise >=26), `LabelDecision` gained `keep_reasons` (human-checked, never auto-derived
  from the classical scorer), `compute_progress()` tracks total + per-stratum counts against
  documented targets (300 total, 40 minimum per non-near_duplicate stratum), and every
  decision is stamped with a real `commit_sha` + `schema_version`. ADR-0014.
- Verified live a second time (chrome-devtools): a 16-image synthetic directory correctly
  produced candidates in all three strata with real measured distances; a confirmed label with
  two reason codes round-tripped correctly including a `commit_sha` matching real repo HEAD;
  progress persisted correctly across a reload. Independent verifier separately re-confirmed
  all four gaps were genuinely real (not invented busywork) and probed the two subtlest risks
  itself (stale/memoized commit_sha; old-schema label files crashing the reader) — both clean.
  Committed `a40ba70`.
- Still not run against GG's real photos — that's next, on him, now that the protocol is
  confirmed sound.

### 2026-07-19 — Feature 1a operating point measured on a real public dataset (INRIA Copydays)
- GG redirected: source ground truth from public human-labeled datasets FIRST, before GG's own
  (still-empty) gold set, and reserve LLM-as-labeler only for features with no public dataset
  available, always with a measured error rate — never as the source of a shipped number.
- Evaluated 5 candidates (California-ND, UKBench, INRIA Holidays, Copydays, MIR-Flickr/NUS-WIDE
  near-dup subsets) against license + task-match + scriptable-download criteria. California-ND
  was the best conceptual match but disqualified on download: password-gated zip, password by
  emailing a 2013-era author, not scriptable. **INRIA Copydays selected** — purpose-built for
  copy detection, INRIA "as-is" research license, graduated attack severities. Original host
  (`pascal.inrialpes.fr`) is dead (real TCP timeout); used Meta/FAIR's mirror instead
  (`dl.fbaipublicfiles.com`) — rejected an unofficial Hugging Face mirror that shipped
  `trust_remote_code=True` with zero actual image bytes. ADR-0015.
- Downloaded `original` (157 photos) + `strong` (229 adversarial-attacked derivatives) — the
  milder graduated `jpeg`/`crop` splits weren't reachable on the FAIR mirror; disclosed as a
  real coverage gap, not smoothed over.
- **Real PR curve, 74,305 pairwise Hamming distances (314 positive / 73,991 negative), zero
  synthetic data, zero LLM labels: operating point = max_hamming_distance 14, precision 0.9600,
  recall 0.0764.** ADR-0012 promoted from PROVISIONAL to MEASURED. The low recall is honestly
  flagged as a floor measured against Copydays' single hardest attack tier (print-and-scan/
  blur/paint), not a representative estimate of ordinary consumer-duplicate recall — precision
  carries no such caveat. CI's fast synthetic-fixture regression gate relocked at 14 (was an
  arbitrary-margin 10); confirmed still passes cleanly against the synthetic fixtures' clean
  separation.
- **Keep-best measured against the same real dataset**: each Copydays block's untouched
  original vs. its attacked derivatives is real (non-fabricated) preference ground truth.
  0.8726 top-1 agreement, 1.0000 never-worst-quartile safety rate across all 157 blocks. The 20
  disagreement blocks written to `reports/ai/copydays_keep_best_disagreements.json` for GG's
  optional one-click review — not auto-resolved.
- AVA (general-aesthetic-correlation check) explicitly skipped and the reasoning recorded, not
  silently dropped: 32GB torrent / 49GB HF zip of individual photographers' contest images, two
  orders of magnitude larger than Copydays for a secondary check, licensing murkier than
  Copydays' blanket INRIA grant. The more operationally important half of the instruction (real
  preference ground truth, disagreements surfaced not fabricated) was fully delivered via
  Copydays instead.
- LLM-as-labeler fallback assessed and found unnecessary for Feature 1a: both required signals
  (near-dup ground truth, keep-best preference ground truth) are fully covered by Copydays' own
  construction-verified labels. Zero LLM involvement anywhere in this feature's ground truth.
- Full suite reconfirmed green: `evals/` 33 passed (446.70s, includes the new real-dataset
  eval), `tests/` unaffected. New files: `evals/ai_fixtures/fetch_copydays.py` (idempotent,
  checksum-verified downloader), `evals/ai_fixtures/copydays_loader.py` (pair discovery),
  `evals/test_ai_copydays_gold.py` (the real measurement — deliberately NOT in the default CI
  sweep; local/on-demand only, same posture as `data/real-disk-run/`'s real-disk validation).
  ADR-0015 (new), ADR-0012 (promoted).
- Per GG's explicit "hold build-order 1b/Track B/Feature 2/Feature 3 until 1a's operating point
  closes on the public dataset" — 1a's operating point is now closed on the public dataset.
  Next build-order item unblocked but not started this session.

### 2026-07-19 — Recall 0.0764 was a dataset artifact, not a pHash limitation; resolved
- GG caught a real problem in the measurement above before trusting it: 0.0764 recall was
  measured ONLY against Copydays' `strong` split — its single hardest, deliberately adversarial
  attack tier (print-and-scan/blur/paint), not Feature 1a's actual target (ordinary consumer
  duplicate accumulation). Flagged as not shippable until resolved: recover the milder
  graduated splits, or generate the realistic transformations programmatically with known
  ground truth, and report recall per tier plus the full precision/recall tradeoff, not just
  the single ≥0.95 point.
- Second search for Copydays' `jpeg`/`crop` splits (Kaggle, Zenodo, Academic Torrents, Wayback
  Machine for those two specific files, two more mirror platforms) confirmed ADR-0015's
  original finding — still unreachable. Went with programmatic generation instead, applied to
  Copydays' own 157 REAL original photos (not synthetic drawn shapes):
  `evals/ai_fixtures/build_realistic_recompression_tiers.py` — 5 deterministic, named profiles
  (mild recompress, mild resize, moderate resize+recompress, moderate PNG round-trip,
  messaging-app-style resave: downscale to ≤1600px, quality 75, metadata stripped) = 785
  realistic positive pairs from real photographic content.
- **Result: at the same locked threshold (14), recall on mild/moderate/messaging_app is
  1.0000/1.0000/1.0000 (was 0.0764 measuring the wrong distribution) with precision 0.9987.**
  The `hard` (Copydays `strong`) tier's 9.6% recall is real, kept, reported separately — and
  confirmed irrelevant to Feature 1a's actual target failure mode, not a feature gap.
- Full realistic-distribution PR tradeoff computed at precision ≥0.95/0.90/0.85 — all three
  collapse to the same point (distance 2, precision 1.0, recall 1.0), since recall saturates at
  1.0 well before precision would need to drop that far. Conclusion: no case for loosening
  toward 0.90 precision — there's no recall left to buy, only false positives to add.
  `max_hamming_distance = 14` reaffirmed, now justified as deliberate margin (0.13 points of
  precision) beyond the bare-minimum-2 needed for the 5 tested profiles, not as a
  precision/recall compromise.
- Track B (CLIP) trigger assessed and NOT triggered: pHash already achieves near-perfect
  precision+recall on the realistic distribution, so there's no Track-A recall gap for
  embeddings to close. Track B remains independently justified by its own semantic-grouping
  mission, not by a rescue need that doesn't exist. Recorded as the actual answer to the "do
  embeddings earn their compute" question for Track A specifically.
- ADR-0012 rewritten with the realistic-distribution section, per-tier table, full tradeoff,
  and the operating-point rationale tied to the recommend-only review-queue design. New files:
  `evals/ai_fixtures/build_realistic_recompression_tiers.py`,
  `evals/test_ai_copydays_realistic_distribution.py`.

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
