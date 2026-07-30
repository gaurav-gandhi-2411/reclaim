# Reclaim — Reliability, Performance & Packaging Overhaul

## Why this
Reclaim works on the developer's machine but not as a shipped Windows product. Live problems: the app is slow to load, **scans take a long time and then fail**, E2E coverage is inadequate, the AI features require manual installation steps an ordinary user cannot perform, and the logo lacks the dimensional quality of a real product mark. This spec fixes all of it and adds the production-grade items currently missing.

Priority: P0 scan reliability + speed → P0 AI packaging → P1 E2E + startup → P2 identity + distribution hardening.

---

## P0-A — Scan must never fail, and must be fast

### Diagnose first (mandatory, before fixing)
Reproduce the failure on a real disk with full logging. Report the actual exception, the path it died on, elapsed time, memory at failure, and file count reached. Do not fix blind.

### Known Windows failure modes to handle explicitly
1. **Cloud placeholders (critical).** OneDrive/Dropbox/GDrive files carry `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` / `FILE_ATTRIBUTE_RECALL_ON_OPEN` / `FILE_ATTRIBUTE_OFFLINE`. Reading them **triggers hydration** — downloading gigabytes and hanging the scan. Detect these attributes and skip content reads (metadata only), surfaced in the UI as "cloud files skipped".
2. **Reparse points / junctions / symlinks.** Skip or follow-once with a visited-inode set. The legacy `AppData\Local\Application Data` junction loop must not recurse infinitely.
3. **Long paths.** Use `\\?\`-prefixed paths and set `longPathAware` in the app manifest. Test paths >260 chars.
4. **Permission denied / locked files.** Catch per-entry and continue; never abort the scan. Count and report skipped items.
5. **Unicode / surrogate filenames.** Must not crash encoding.
6. **Special dirs:** `System Volume Information`, `$Recycle.Bin`, `WindowsApps`, pagefile/hiberfil, network drives (skip by default, opt-in), and BitLocker-locked volumes.

### Performance architecture (the big win)
- **Never hash everything.** Staged dedup pipeline: (1) group by exact size → discard singletons; (2) partial hash (first 64KB + last 64KB) within size groups; (3) full hash only on partial-hash collisions. This typically cuts I/O by 10–100×.
- **Metadata pass is enumeration-only** — no file content reads.
- **SQLite:** WAL mode, batched transactions (e.g. 5–10k inserts), prepared statements, indices created *after* bulk insert, `synchronous=NORMAL`.
- **Bounded memory:** stream results; never materialise all file records in a list.
- **Parallelism:** thread pool sized to storage type (NVMe benefits from parallel reads; HDD does not — detect and adapt). Enumeration and hashing on separate stages.
- **Incremental rescan:** use the NTFS **USN change journal** where available so a rescan is a delta, not a full walk. Fall back to mtime+size comparison.
- **Cancellable + resumable:** cancellation must leave the index consistent; a killed scan must resume, not restart.
- **Accurate progress**: phase-aware (enumerating / grouping / hashing / analysing) with real counts, not a spinner.

### Performance budgets (enforced in CI)
Define and gate on measured targets, e.g.: cold app start < 2s; enumerate 500k files < N min; full duplicate pass on a 200GB fixture < M min; peak RSS < X MB. Numbers to be set from the first honest measurement, then regressions fail CI.

---

## P0-B — AI features must ship in the box

### The problem
AI features (pHash near-dupe, MinHash+MiniLM doc dupe, LightGBM ranker, CLIP semantic grouping) require manual dependency installation. A normal user cannot and will not do this. Almost certainly caused by `torch` (2–4GB) being a hard dependency.

### The fix: drop torch, ship ONNX
- Convert CLIP (image+text encoders) and the MiniLM sentence encoder to **ONNX Runtime**, **int8-quantised**. Indicative sizes: CLIP ≈150–180MB, MiniLM ≈20–30MB, onnxruntime CPU ≈15MB. LightGBM is already native and small; pHash needs no model.
- **Total AI payload target < 250MB** — small enough to bundle in the installer.
- Verify quality parity after quantisation: the ONNX int8 models must match the current torch models within an acceptable tolerance on a fixed evaluation set (near-dupe detection accuracy, semantic grouping quality). Report the delta; do not ship a silent quality regression.
- **Zero user steps.** Either bundled in the installer, or a single in-app "Enable AI features" button with a progress bar, resume, checksum verification, and clear failure messaging. No pip, no Python, no terminal, ever.
- **Lazy loading:** models load on first AI use, never at startup.
- **Graceful degradation:** if the AI pack is absent/failed, rules-first features work fully and the UI explains what's unavailable.

---

## P1-A — Startup performance
- If the build is Nuitka `--onefile`, it extracts the whole payload to temp **on every launch**. Switch to standalone/onedir installed via the installer.
- Audit import cost; defer every heavy import (ML, imaging, DB migrations) behind first use.
- Show the window immediately; populate asynchronously. No blank/frozen window.

## P1-B — Real end-to-end testing
Current E2E is inadequate. Build a **synthetic fixture filesystem generator** covering the hostile cases and run the full pipeline against it in CI:
- Known duplicate sets (exact + near-dupe images + near-dupe docs) with known-correct answers.
- Long paths (>260 chars), unicode/emoji names, zero-byte files, very large files, sparse files.
- Junctions, symlinks, and a deliberate recursion loop.
- Simulated cloud-placeholder attributes.
- Permission-denied directories.
- A large-scale tier (100k+ files) for performance gating.

Assertions: scan completes without error; finds exactly the known duplicates (precision/recall reported); **vault move is atomic**; restore returns files bit-identical to original paths; cancel mid-scan leaves a consistent index; resume completes correctly; a crashed run recovers.

Also: SafetyValidator must be adversarially tested — attempts to quarantine system/critical files must be blocked, with tests proving it.

---

## P2-A — Product identity
- Adopt the approved 3D-style mark (faceted planes with a consistent light source — the VS Code / Edge approach, not bevels or drop shadows).
- Produce the full asset set: `.ico` with all sizes (16/24/32/48/64/128/256), installer branding, in-app header, GitHub social preview, README banner.
- **Verify legibility at 16px and 32px** — the mark must remain readable in the taskbar and Explorer.

## P2-B — Distribution hardening (currently missing)
- **Code signing.** An unsigned installer for a tool that moves/deletes files triggers SmartScreen "unknown publisher" — a serious trust and adoption blocker. Evaluate OV certificate options and cost; document the decision. (Until signed, document the warning and provide checksums.)
- **Crash diagnostics:** structured logs to a known location, plus a one-click "copy diagnostics" that bundles logs + system info for issue reports. A failed scan must be reportable.
- **Auto-update** mechanism (or at minimum an in-app "new version available" check).
- **Uninstall behaviour:** define and implement what happens to quarantined vault contents (prompt to restore/purge — never silently orphan user files).
- **DPI/scaling and accessibility:** correct rendering at 125/150/200%; keyboard navigation; screen-reader labels on primary actions.
- **First-run safety:** dry-run/preview default, explicit confirmation before the first destructive action, obvious one-click restore.

---

## Acceptance criteria
1. Root cause of the scan failure identified with evidence, then fixed; scan completes on a real full-disk run.
2. All listed Windows failure modes handled with tests (cloud placeholders, reparse loops, long paths, permissions, unicode).
3. Staged hashing implemented; measured before/after scan time reported on the same corpus.
4. Incremental rescan working (USN or fallback); cancel and resume proven.
5. AI features work with **zero manual user steps**; torch removed; ONNX int8 models with reported quality parity; lazy-loaded; graceful degradation without the pack.
6. Cold start meets the agreed budget; onefile-extraction issue resolved.
7. E2E suite on the fixture filesystem green, including atomic vault move, bit-identical restore, crash/cancel recovery, and SafetyValidator adversarial tests; performance budgets gated in CI.
8. New 3D identity applied everywhere; legible at 16px.
9. Crash diagnostics, uninstall vault handling, DPI/accessibility, and update check in place; code-signing decision documented.
10. A fresh Windows VM install-to-first-successful-scan runs clean with no developer knowledge required.

## Risks
- **Fixing scan by skipping too much** — skipping cloud/reparse content must not silently miss real duplicates; report what was skipped and why.
- **Quantisation quality loss** — must be measured, not assumed.
- **Installer size** — keep the bundled AI payload under target; if it exceeds, use the one-click in-app download instead.
- **Destructive-operation safety** — every change to the vault/move path needs its adversarial tests re-run; this tool deletes user data.
