# Reclaim — AI-Assisted Storage Cleanup (Windows)

**Target user:** GG (solo dev/power user, Windows 11) first; Windows power users with
disk-space pressure as a portfolio-demonstrable audience second.
**Pain point:** Disk is ~94% full; manual cleanup is slow, and manual deletion risks
taking out something that matters (repo internals, credentials, cloud placeholders).
**Success metric:** Reclaims ≥30 GB on GG's machine in first run (measured via
before/after disk-free), zero protected-file incidents, every quarantined file restorable.
**Who pays:** No one in Phase 1 — personal tool + portfolio piece. Not monetized; no
paid-tier design constraints apply.

Local desktop tool that reclaims disk space with a rules-first engine, AI-assisted
similarity detection for the long tail, hard safety gates, and fully recoverable
actions. Target machine: Windows 11, disk ~94% full.

Repo: `C:\Users\dev\ml-projects\reclaim`

---

## Design Principles (non-negotiable)

1. **Rules-first, AI second.** Deterministic detection for provably-safe categories;
   ML/embeddings only for the similarity long tail. Never let a model output alone
   authorize a deletion.
2. **No fabricated confidence.** Only report scores that are measurable
   (hash match = exact; pHash Hamming distance = a distance, reported as such).
   Heuristic scores are labeled "heuristic", never presented as calibrated probability.
3. **Safety gate runs first.** SafetyValidator filters files *before* they enter the
   candidate pipeline, not after scoring.
4. **Every action recoverable.** Quarantine → Recycle Bin (via `send2trash`) or app
   vault with manifest. No permanent delete in v1. Restore-by-batch supported.
5. **Dry-run is the default mode.** Executor requires an explicit `--apply` flag.

---

## Phase 1 — Deterministic Engine (MVP; solves the 94% problem)

### Scanner
- Full-volume walk with `os.scandir`, multi-threaded per top-level dir.
- Persist scan index in SQLite (path, size, mtime, ctime, ext, attrs, hash cache).
- Incremental rescans via mtime + size diff against index. Do **not** rely on
  last-access time (disabled by default on NTFS).
- Hardlink/junction/symlink aware — count physical size once, never follow
  reparse points into loops.
- **Cloud-sync detection:** identify OneDrive/Dropbox/Google Drive roots; flag
  files with `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` (cloud-only placeholders).
  Placeholders are excluded entirely — deleting them frees no local space and
  destroys the cloud copy.

### Rule Categories (auto-quarantine eligible)
Each category = detector + rationale template + rebuild instruction shown to user.
- Dev artifacts: `node_modules`, `__pycache__`, `.venv`/`venv`, `target/`, `build/`,
  `dist/`, `.next`, `.gradle`, `.m2/repository` — **only when a manifest proving
  rebuildability sits adjacent** (package.json, pyproject.toml, pom.xml, etc.).
- Package/model caches: pip, npm, uv, HuggingFace hub, torch hub, conda pkgs.
- Browser caches, Windows temp (`%TEMP%`, `C:\Windows\Temp`), thumbnail caches.
- Crash dumps, `.dmp`, memory dumps, WER reports.
- Old installers in Downloads: `.exe`/`.msi`/`.iso` older than N days (default 90)
  — review-queue by default, auto only if user enables the category.
- Extracted-archive pairs: archive + sibling dir with matching name and ≥90%
  filename overlap → recommend deleting the archive (never the extraction).
- Exact duplicates: size bucket → first/last 64KB partial hash → full BLAKE3.
  Keep-heuristic: prefer copy outside Downloads/Temp, oldest path, shortest depth;
  user can override per cluster.
- Large logs (> configurable size, not modified in N days).

### SafetyValidator (hard gate, deny-first)
Never enters candidate list:
- `C:\Windows`, `Program Files*`, ProgramData, AppData binaries dirs, drivers.
- Any path inside a git repo (detect `.git` upward) — including its node_modules
  only exempted if repo is clean AND category explicitly enabled.
- Extensions: `.kdbx`, `.ppk`, `.pem`, `.key`, `.pfx`, `.crt`, `.gpg`, ssh dir.
- Databases (`.db`, `.sqlite`, `.mdf`), VM images (`.vhdx`, `.vmdk`, `.qcow2`),
  Docker/WSL data roots.
- Documents matching finance/tax/legal filename patterns → review-only, never auto.
- Cloud-only placeholders (see above).
- User-editable allow/deny lists in `config.toml`, applied after built-ins
  (user deny always wins; user allow cannot override built-in denies).

### Decision Policy (v1)
- **Tier A — auto-quarantine:** rule categories above, passing SafetyValidator,
  category enabled in config. Moved to Recycle Bin/vault with manifest entry
  (original path, size, category, rationale, batch id). Retention default 30 days.
- **Tier B — review queue:** everything else (installers, duplicate clusters,
  large old files). Dashboard shows rationale, size, cluster members, restore info.
- **No Tier for silent permanent deletion. It does not exist in v1.**

### Executor + Recovery
- `send2trash` for Recycle Bin; app vault (move + manifest JSONL) as alternative.
- Batch apply with per-batch undo. Post-apply report: files, bytes freed,
  category breakdown — numbers from actual filesystem results, not estimates.

### UI (Phase 1)
- FastAPI backend + single-page local web dashboard (localhost only).
- Views: storage treemap (largest dirs), category cards with size + count,
  review queue with side-by-side duplicate comparison, quarantine/restore view,
  dry-run simulation diff.
- Distinct visual identity: logo/favicon/header designed first, then applied.

---

## Phase 2 — Similarity Intelligence (recommendation-only, never auto)

- **Images:** perceptual hash (pHash/dHash) clustering; report Hamming distance.
  Screenshot burst detection: same dimensions + capture-time proximity + pHash
  distance ≤ threshold. Keep-best heuristic: largest file / least blur (Laplacian
  variance). CLIP embeddings only if pHash proves insufficient — measure first.
- **Documents:** MinHash/SimHash for near-duplicate text first (cheap);
  sentence embeddings only for clusters MinHash can't resolve. Version-chain
  detection via filename patterns (`v2`, `final`, `(1)`) + content similarity.
- **Video:** duration + size + sampled-keyframe pHash. No frame-level ML in v2.
- All Phase 2 outputs land in the review queue with distances/overlap percentages
  shown as raw numbers.
- Embedding/hash cache in SQLite keyed by (path, size, mtime).

## Phase 3 — Preference Layer (deferred)

- Log every accept/reject decision with feature vector.
- Per-category toggles + per-directory policies are the v1 "personalization".
- Train a ranking/gating model only after ≥500 labeled decisions exist; evaluate
  with time-split validation before it influences anything. Until then: no
  online learning, no adaptive confidence.

---

## Evaluation & CI

- **Golden fixture tree:** synthetic filesystem (protected files, dupes, caches,
  git repos, cloud placeholders simulated via attribute mocks) built by test setup.
- **Hard CI gate: zero protected-category files may ever appear in Tier A.**
  Any hit fails the build. (Same posture as TriageIQ fabrication gate.)
- Duplicate detector: precision = 1.0 required on fixtures (byte-identical check).
- Similarity detectors: precision/recall on a small hand-labeled local image set;
  thresholds chosen from that data, recorded in an ADR.
- Perf budget: ≥100K files/min scan on SSD; incremental rescan <10% of full scan.
- Report every metric with the command that produced it.

## Stack

Python 3.12 + uv · FastAPI + vanilla JS/HTMX dashboard · SQLite · BLAKE3 ·
send2trash · Pillow/imagehash (Phase 2) · pytest + fixture builder.
No Electron/Tauri in v1 — local web UI is sufficient and ships faster.

## Success Criteria

- Reclaims ≥30 GB on GG's machine in first run (verified via before/after
  disk-free measurement) with zero protected-file incidents.
- Every quarantined file restorable; restore verified in tests.
- Every recommendation carries a concrete, category-specific rationale.
