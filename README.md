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

This pass targets **you and technical reviewers** — anyone comfortable running a CLI tool they
built or cloned themselves. Explicitly out of scope for this pass, pending a separate go-public
decision:

- A safe-mode-by-default posture for non-technical users (review-only, Recycle-Bin-only, no
  batch-auto-apply) — current behavior assumes you've read what you're enabling.
- A signed, double-click public installer (MSI/Tauri). A documented, reproducible Nuitka
  one-folder build (below) produces a double-clickable local `reclaim.exe`, but it is unsigned;
  expect an antivirus/SmartScreen false-positive prompt on first run (common for freshly-built,
  unsigned PyInstaller/Nuitka binaries) and treat that as expected, not as a compromise signal,
  for a binary you built yourself from this source. Public code signing is deliberately deferred
  to the Stage 2 public-distribution pass.

### Optional: standalone `reclaim.exe` (Nuitka)

For a double-clickable local build that doesn't require a Python environment on the target
machine — **one-folder mode** (`--standalone`, not `--onefile`): produces a `dist/cli.dist/`
folder containing `reclaim.exe` plus its dependencies as separate files, not a single packed
binary, per this pass's explicit scope (a public single-file/signed installer is Stage 2):

```powershell
uv pip install nuitka
uv run python -m nuitka --standalone --output-dir=dist `
  --output-filename=reclaim.exe `
  --company-name="Gaurav Gandhi" --product-name="Reclaim" --product-version=0.1.0 `
  --include-package=fastapi --include-package=starlette --include-package=pydantic `
  --include-package=uvicorn --include-package=jinja2 --include-package=structlog `
  --include-data-dir=src/reclaim/api/templates=reclaim/api/templates `
  --include-data-dir=src/reclaim/api/static=reclaim/api/static `
  --windows-console-mode=force `
  src/reclaim/cli.py
```

`dist/cli.dist/reclaim.exe` launches the same CLI (`reclaim.exe dashboard` opens the browser
dashboard) — **this exact command was run and verified** (`reclaim.exe --help` and
`reclaim.exe scan <dir>` both work correctly against a real directory) as part of this pass. No C
compiler is required up front: Nuitka downloads a private MinGW64 toolchain on first run if
neither MSVC nor an existing MinGW is found (this build did — the whole compile, ~858 C files,
took several minutes). Building against this project's own dev venv (which has `--all-groups`
installed, including `mypy`/`ruff`/`pytest`) pulls in more than the runtime dependency closure —
the verified build came to 121MB in `dist/cli.dist/`; build against a runtime-only venv
(`uv sync --no-dev` in a fresh `.venv`) for a leaner result if you're not also iterating on the
compile itself. This is **not code-signed** — Windows SmartScreen and some antivirus engines flag
unsigned, freshly-compiled executables by default, independent of whether the binary is actually
malicious; this build is meant for you and reviewers who trust the source it was built from, not
for public distribution.

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
