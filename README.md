# Reclaim

Rules-first Windows disk-cleanup tool. Deterministic detection for provably-safe categories, a
hard safety gate that runs before any candidate is generated, and fully recoverable actions
(vault + manifest, dry-run by default). No ML — see `docs/CASE_STUDY.md` for what's actually
wired in vs. specced for later. Full design: `reclaim-spec.md`. Build history: `PLAN.md`.

**Status:** single-user / technical-reviewer install. Not yet packaged for a general public
audience — see "Distribution status" below.

## Install

Requires Python 3.12 and Windows (the scanner and executor are NTFS-specific by design — junction/
reparse-point handling, `\\?\` long-path moves, Recycle Bin integration).

```powershell
# from a released version once published, or from a local clone either way:
uv tool install .
# or, if you use pipx instead of uv:
pipx install .
```

This installs one `reclaim` executable on your PATH. Verify it:

```powershell
reclaim --help
```

## First run

```powershell
# 1. Scan a directory (read-only — builds a local SQLite inventory index, touches nothing else).
reclaim scan "C:\Users\you\Downloads"

# 2. Launch the dashboard — opens your browser to the review UI automatically.
reclaim dashboard
```

The dashboard binds to `127.0.0.1` only (hard-enforced — see "Security" below) and shows, per
category: exact measured size, a plain-language rationale, and (where relevant) a rebuild
instruction. Everything defaults to dry-run: nothing on disk is touched until you explicitly
preview an apply and then confirm it a second time.

Prefer the CLI over the dashboard for a batch run:

```powershell
# Dry-run report only — never touches disk.
reclaim apply "C:\Users\you\Downloads"

# Real apply, Tier A (auto-quarantine-eligible) candidates only.
reclaim apply "C:\Users\you\Downloads" --apply --tier A
```

## Restoring a batch

Every `apply` prints a `batch_id`. Most categories vault into `data/quarantine/` with a 30-day
(or category-specific) retention window before permanent deletion; a few deterministically
rebuildable categories (package caches, dev artifacts, browser/temp caches, crash dumps) delete
immediately since their real recovery path was always "rebuild it," not "restore it" — see
ADR-0001 for the full rationale and `reclaim apply --help`/the dashboard's per-item recovery note
for which is which before you apply.

```powershell
# From the CLI:
reclaim undo <batch_id>

# Or from the dashboard's Quarantine & Restore tab — same manifest, same guarantee.
```

Restore refuses to write outside the file's original location or a manifest entry whose vault
path doesn't resolve inside the configured vault directory (defense against a corrupted or
hand-edited `manifest.jsonl` — see the security notes below). A batch that mixes vaulted and
permanently-deleted items restores what's restorable and reports the rest as
`restore_unsupported`, never silently.

## Security posture

Reclaim moves and deletes files on your machine, so this is treated as a hard security boundary,
not an afterthought:

- **Loopback-only.** The dashboard server can only bind to `127.0.0.1`/`::1` — enforced at the
  argument-parsing layer, not just a default (`--host 0.0.0.0` is a hard parse error).
- **CSRF + DNS-rebinding defense.** Every mutating API call requires a per-process token the
  dashboard page itself carries (unreadable by a cross-origin page), and every API request's
  `Host`/`Origin` headers are checked against the exact loopback address the server is bound to.
- **No elevation.** Every mutating command refuses to run if the process holds an elevated
  (Administrator) token — an ordinary user's own filesystem permissions are part of what keeps
  this tool off protected system paths, and running elevated would silently remove that backstop.
- **Restore path-traversal guard.** `reclaim undo`/the dashboard's restore never writes to a
  protected system root and never trusts a manifest entry's vault path without first confirming
  it resolves inside the configured vault directory.
- **XSS-hardened dashboard.** File/directory names are attacker-controllable input (this tool's
  whole job is walking a real disk) — every render path treats them as text, never markup; see
  `tests/frontend/xss.test.mjs` for the regression test and `docs/CASE_STUDY.md` for the finding
  this closed.
- **`pip-audit` in CI**, failing the build on any known dependency vulnerability.

None of this is a substitute for reading what a category actually proposes before you apply it —
`SafetyValidator`'s deny-list is a floor, not a guarantee that every possible file you'd regret
losing is covered.

## Distribution status

**Stage 2** (ADR-0023, ADR-0024) turned this from "a CLI tool you clone and run yourself" into a
double-click Windows installer aimed at people who won't read the source first:

- **Safe mode is the default for every fresh install.** Recommend/review-only, Recycle-Bin-only
  deletes, and the highest-risk categories (`duplicates`/`model_caches`/`dev_artifacts`) forced
  off — structurally enforced (`evals/test_safe_mode_gate.py`), not a convention. Full behavior
  ("power mode") requires an explicit, typed, logged confirmation
  (`reclaim mode power --confirm "I understand this can permanently delete files"`) — see
  ADR-0023.
- **A double-click installer** (`packaging/reclaim.iss`, built with Nuitka `--standalone` +
  Inno Setup — see ADR-0024 for why this pair over Briefcase/MSI) installs per-user, no admin
  prompt (matches `reclaim.elevation.assert_not_elevated`'s "never runs elevated" invariant end
  to end), and adds a Start Menu / optional desktop shortcut that launches
  `reclaim.exe dashboard`.
- **Core-only, by design.** The installer ships the deterministic engine only — no AI-layer
  dependencies. Measured (clean `uv venv` install, this session): core `site-packages` is
  **13.6 MB**; the `[ai]` extra adds **~1,028 MB** (`torch` alone is 464 MB, shared by both
  Feature 1b's document dedup and Track B's CLIP grouping — there's no way to get "semantic
  grouping" without it). Every AI feature is recommend-only or browse-only, so a fresh install
  loses nothing essential by not carrying it; if you want the AI layer, install from source with
  `uv sync --extra ai` / `pip install reclaim[ai]` (a separate Python environment — the
  Nuitka-compiled `reclaim.exe` cannot `pip install` into itself; see ADR-0024's consequences
  section for this disclosed gap).
### First run: SmartScreen and antivirus prompts (expected, not a compromise signal)

**This installer and `reclaim.exe` are unsigned.** Stage 2 Part C assessed code-signing options
(Azure Trusted Signing, ~$9.99/month, vs. staying unsigned) and the project shipped unsigned —
there's no revenue or user base yet to justify a recurring cost, and nothing about staying
unsigned today blocks signing later (see "Staying signing-agnostic" below). Two prompts are
expected as a direct consequence, and neither means the binary is unsafe:

- **Windows SmartScreen**, on first launch of `reclaim-setup.exe` (and/or `reclaim.exe`
  directly): *"Windows protected your PC" -> "Microsoft Defender SmartScreen prevented an
  unrecognized app from starting."* Click **More info**, then **Run anyway**. This is not a
  virus scan verdict — it's SmartScreen's reputation check, which any freshly-built or
  low-download-count binary fails regardless of actual safety, signed or not.
- **Antivirus false positives.** Some AV engines flag freshly-compiled, unsigned Nuitka/
  PyInstaller-style binaries heuristically (packed-executable + no publisher signature is a
  common malware shape, even though this build is neither packed nor obfuscated) — this project
  has already hit one AV/quarantine false-positive on a freshly-built binary during earlier
  testing. If your AV quarantines `reclaim.exe` or `reclaim-setup.exe`: restore it from
  quarantine, then add an exclusion for the install folder (Windows Security ->
  Virus & threat protection -> Manage settings -> Add or remove exclusions -> Folder). Only do
  this for a binary you built yourself from this source or downloaded from this repository —
  never for a binary from an untrusted source.

**Staying signing-agnostic.** The packaging pipeline (`packaging/reclaim.iss`) has no
`SignTool`/`SignedUninstaller` directive today — it builds and runs unsigned as-is. Adding
signing later needs no rework: a `signtool.exe` step on `entry_point.dist/reclaim.exe` before
Inno Setup packages it, plus a `SignTool=` line in `reclaim.iss` to also sign
`reclaim-setup.exe` itself. Neither `entry_point.py`, the build command, nor any safety-relevant
code changes either way.

Build it yourself:

```powershell
uv add --dev nuitka   # already recorded in pyproject.toml's dev group
uv run python -m nuitka --standalone --assume-yes-for-downloads `
  --company-name="Gaurav Gandhi" --product-name="Reclaim" --product-version=0.1.0 `
  --include-package=reclaim --include-package=uvicorn --include-package=fastapi `
  --include-package=starlette `
  --include-data-dir=src/reclaim/api/static=reclaim/api/static `
  --include-data-dir=src/reclaim/api/templates=reclaim/api/templates `
  --output-dir=packaging/build --output-filename=reclaim.exe `
  packaging/entry_point.py

# Build from a CORE-ONLY environment (no [ai] extra) so nothing AI-related can leak into the
# installer — Nuitka's static import analysis won't follow reclaim.ai's lazy
# importlib.import_module() calls anyway, but a clean venv makes the guarantee airtight rather
# than incidental. Then package it:
"C:\Program Files\Inno Setup 7\ISCC.exe" packaging\reclaim.iss
# -> packaging\dist\reclaim-setup.exe
```

`packaging/test_packaged_safe_mode.ps1` is the safety proof that runs against the **actual
compiled artifact** (not the dev tree): fresh-install defaults to safe mode, a real `--apply`
batch against a config.toml that tries to re-enable the force-off categories still resolves to
`method=recycle_bin` (never vault/direct_delete), and typed confirmation is the only door to
power mode — all verified against both the raw Nuitka `--standalone` build and a real
Inno-Setup-installed copy (silent install → run → silent uninstall, no admin prompt at any
step).

## Development

```powershell
uv sync --all-groups
uv run pytest              # unit tests
uv run pytest evals/ -v    # SafetyValidator hard gate + perf smoke tests (slower — real git ops)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Frontend regression tests (jsdom, no browser download required):

```powershell
cd tests/frontend
npm ci
npm test
```

See `PLAN.md` for the full build history (including the real-disk validation runs and the bugs
they found), `docs/architecture/adr/` for every architectural decision, and `docs/CASE_STUDY.md`
for the narrative writeup.
