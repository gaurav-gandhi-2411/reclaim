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
- `send2trash` only — no permanent deletes anywhere in v1.
- No fabricated confidence scores in code or UI copy (hash match = exact, pHash = a
  reported Hamming distance, heuristics labeled "heuristic").
- Never scan or modify GG's real disk during development — fixtures only. First real-disk
  run is dry-run mode, output a report for GG's review before anything is queued for apply.

## Stage order (riskiest assumption first)

| # | Stage | Status | CI gate |
|---|-------|--------|---------|
| 1 | SafetyValidator + golden fixture tree + hard CI gate | done | zero protected files ever appear in Tier A; build fails on any hit |
| 2 | Scanner (os.scandir, SQLite index, cloud-placeholder detection) | done | placeholder-exclusion unit test passes; perf budget ≥100K files/min on SSD (real number pending GG's SSD, see checkpoint) |
| 3 | Rule detectors (dev artifacts, caches, temp, dumps, installers, archive pairs, logs) | not started | detector fixtures pass, manifest-adjacency check enforced |
| 4 | Exact-duplicate pipeline (size bucket -> 64KB partial hash -> BLAKE3) | not started | precision = 1.0 on fixtures |
| 5 | Executor + quarantine manifest + batch undo | not started | every quarantined fixture file restorable in tests |
| 6 | FastAPI + dashboard (treemap, category cards, review queue, restore view) + visual identity | not started | dashboard renders against fixture data; no prior-project branding reused |

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

## Gotchas discovered
- `uv init --package` created a `reclaim = "reclaim:main"` script entry pointing at a stub
  `main()`; repointed to `reclaim.cli:main` (placeholder) since Stage 2+ will define the real
  CLI surface.
- Default git branch from this git version is `master`; renamed to `main` before first
  commit per house rule 35 (only needed once, first commit hadn't landed yet).
- `os.scandir`'s `DirEntry.stat()` does not populate `st_dev`/`st_ino` on Windows — only a
  direct `Path.stat()`/`os.stat()` call does (via `GetFileInformationByHandle`). Anything
  needing hardlink identity must stat the real path, not trust the scandir-cached stat.
