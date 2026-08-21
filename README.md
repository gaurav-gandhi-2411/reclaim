<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/logo-lockup.png" alt="Reclaim" width="300" />
</p>

# Reclaim

Reclaim finds files on your Windows PC that are safe to remove — caches your tools rebuild
automatically, temp files, and duplicate copies — and shows you exactly why each one is safe
before anything happens. Nothing is ever deleted without your review, and by default everything
Reclaim removes goes to the Windows Recycle Bin, so it's always recoverable the same way as if
you'd deleted it yourself.

## Download

**[Download Reclaim for Windows -> latest release](https://github.com/gaurav-gandhi-2411/reclaim/releases/latest)**
&nbsp;—&nbsp; **Windows only (Windows 10/11).**

Download `reclaim-setup.exe` from the page above and run it. No admin prompt, no account, no
sign-up, nothing installed beyond the app itself.

Windows will likely show a SmartScreen warning the first time you run it ("Windows protected your
PC") — this is expected for a small, unsigned, low-download-count app, not a virus verdict. Click
**More info** -> **Run anyway**. Full explanation, including the antivirus false-positive some
scanners raise on freshly-built binaries: see
["First run: SmartScreen and antivirus prompts"](#first-run-smartscreen-and-antivirus-prompts-expected-not-a-compromise-signal)
further down.

## First run

Reclaim starts in **Simple mode** — one button, no path typing, no decisions to make:

1. Run the installer — no admin prompt, installs into your own user folder.
2. Open **Reclaim** from the Start Menu.
3. Your browser opens automatically to the Reclaim dashboard.
4. A one-time screen explains safe mode before anything else is usable — read it, then continue.
5. Click **Clean My Computer**. Reclaim scans every fixed drive on your machine, showing a live
   progress bar and time estimate the whole way through — never a silent wait.
6. Once it's done, you'll see exactly what it found, in plain language and real measured sizes
   (e.g. "Temporary & browser cache files — 890 MB — safe, recreated automatically as you
   browse"). Only categorically-safe clutter is ever offered here.
7. Click **Clean now** — one confirmation, then it's done.
8. Whatever you clean is **moved to the Recycle Bin** — empty the Recycle Bin afterward to
   actually free up the disk space.

Want more control — custom folders, duplicate detection, AI-powered suggestions, individual
review of every item before it's touched? Click **Switch to Advanced** in the header at any
time. Nothing in Simple mode is a one-way door; you can move between the two freely.

## Safe mode

Safe mode is on for every fresh install, and it isn't just a default that could quietly slip —
it's a structural guarantee (see [ADR-0023](docs/architecture/adr/0023-stage2-safe-mode-safety-boundary.md)
for the full technical proof):

- **Every delete goes to the Recycle Bin.** Never a permanent delete, no matter what you select.
- **Nothing applies automatically.** You always pick what to clean and confirm it — Reclaim never
  acts on its own.
- **The riskiest categories stay off** (exact-duplicate detection, ML model caches, and
  dev-environment folders) until you explicitly opt in to power mode.
- **Power mode is a typed opt-in, and reversible.** It unlocks the full toolset (permanent delete
  for rebuildable caches, auto-apply) only after you type an exact confirmation phrase in the
  dashboard. You can switch back to safe mode at any time, with no confirmation required.

## How to restore something

**Primary path: Windows' own Recycle Bin.** In safe mode (the default for every install),
everything Reclaim removes goes to the Recycle Bin — restoring it is exactly like recovering
anything else you deleted yourself: open the **Recycle Bin**, find the file, right-click ->
**Restore**.

The dashboard's **Quarantine & Restore** tab restores a different thing: Reclaim's own internal
vault, which is only used in power mode. It cannot restore Recycle-Bin batches (safe mode's
default method) by design — Windows already owns that recovery path, and Reclaim doesn't
duplicate it. If you're on safe mode (the default), the Recycle Bin above is the path you want.

## Uninstalling

Uninstalling Reclaim from Windows' "Add or remove programs" leaves your `data` folder (scan
history, the quarantine vault, logs) in place by default, and asks whether you also want to
delete it — because that folder can still hold files parked in Reclaim's vault from power mode
that you haven't restored yet. Choose **No** (the default) if you're at all unsure; choose
**Yes** to remove everything Reclaim ever wrote to disk. Running the uninstaller silently
(`/VERYSILENT`) always preserves your data, regardless of that prompt.

## Screenshots

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/01-first-run.png" alt="First-run screen explaining Simple mode and safe mode before anything else is usable" width="800" />
</p>

**First run** — Simple mode and safe mode explained before you can do anything else, including
what's restricted and a link to the license.

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/02-simple-idle.png" alt="Simple mode idle screen with a single Clean My Computer button" width="800" />
</p>

**Simple mode** — one button. No path typing, no categories to configure, no decisions before
you've seen anything.

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/03-simple-scanning.png" alt="Simple mode live progress bar with a plain-language time estimate while scanning every fixed drive" width="800" />
</p>

**Live progress, never a silent wait** — an animated progress bar and a plain-language time
estimate while every fixed drive is scanned.

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/04-simple-results.png" alt="Simple mode results screen showing plain-language categories with real measured sizes and a Clean now button" width="800" />
</p>

**Plain-language results** — real measured sizes and *why* each category is safe (never an
invented confidence score), scoped to categorically-safe clutter only.

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/05-quickclean-success.png" alt="Quick Clean success message stating files were moved to the Recycle Bin, not freed" width="800" />
</p>

**Moved vs. freed, never conflated** — a real clean reports what actually happened: moved to
the Recycle Bin, with an explicit reminder that emptying it is a separate, manual step.

<details>
<summary>More screenshots (Advanced mode: Overview, Review Queue, AI Suggestions, Storage Treemap, Quarantine & Restore, light theme)</summary>

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/06-overview-advanced.png" alt="Advanced mode Overview tab with Quick Clean and full category breakdown" width="800" />
</p>

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/07-review-queue.png" alt="Review Queue showing individual candidates with keep vs. delete rationale" width="800" />
</p>

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/08-storage-treemap.png" alt="Storage treemap view" width="800" />
</p>

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/09-quarantine-restore.png" alt="Quarantine and Restore tab pointing to the Windows Recycle Bin for safe-mode batches" width="800" />
</p>

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/10-ai-suggestions.png" alt="AI Suggestions tab explaining that AI features are not included in the installer and are recommend-only when available" width="800" />
</p>

**AI stays recommend-only, and honest about what's installed** — the packaged installer doesn't
bundle the optional AI/ML layer, and the dashboard says so plainly instead of showing a broken
or empty tab. When the AI extra *is* installed (source checkouts only, `uv sync --extra ai`),
every suggestion still goes through the exact same apply/safety check as everything else in
Reclaim — recommend-only, never auto-applied.

<p>
  <img src="https://raw.githubusercontent.com/gaurav-gandhi-2411/reclaim/main/docs/assets/screenshots/11-light-theme.png" alt="Overview tab in light theme" width="800" />
</p>

</details>

Crash-recovery and live-apply-progress screenshots (introduced in v1.2.0) aren't retaken this
release — only the color tokens changed underneath them, not the behavior — see the v1.2.0
screenshots in git history for those two states.

## Questions, bugs, and privacy

- **Something go wrong, or found a bug?** See [SUPPORT.md](SUPPORT.md) for how to report it and
  what to include.
- **Want to know exactly what Reclaim does and doesn't send anywhere?** See
  [PRIVACY.md](PRIVACY.md) — short version: nothing leaves your machine.
- **Want to contribute code?** See [CONTRIBUTING.md](CONTRIBUTING.md).

---

# For developers

Rules-first Windows disk-cleanup tool. Deterministic detection for provably-safe categories, a
hard safety gate that runs before any candidate is generated, and fully recoverable actions
(vault + manifest, dry-run by default). No ML — see `docs/CASE_STUDY.md` for what's actually
wired in vs. specced for later. Full design: `reclaim-spec.md`. Build history: `PLAN.md`.

**Status:** Stage 2 shipped a double-click Windows installer with safe mode on by default —
see "Distribution status" below for the full picture, including what safe mode restricts and
why the installer is unsigned.

## Install from source

Requires Windows (the scanner and executor are NTFS-specific by design — junction/reparse-point
handling, `\\?\` long-path moves, Recycle Bin integration). For the double-click installer, use
the [Download](#download) section above instead — this is for development or the optional AI
layer, which the installer doesn't ship (see "Distribution status" below for why).

```powershell
uv tool install .
# or, if you use pipx instead of uv:
pipx install .
# for the applied-AI layer too (near-dup detection, semantic grouping, the clutter ranker):
uv tool install ".[ai]"
```

This installs one `reclaim` executable on your PATH. Verify it:

```powershell
reclaim --help
```

## CLI quick start

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

## Restoring a batch (CLI / power-mode detail)

Every `apply` prints a `batch_id`. Most categories vault into `data/quarantine/` with a 30-day
(or category-specific) retention window before permanent deletion; a few deterministically
rebuildable categories (package caches, dev artifacts, browser/temp caches, crash dumps) delete
immediately since their real recovery path was always "rebuild it," not "restore it" — see
ADR-0001 for the full rationale and `reclaim apply --help`/the dashboard's per-item recovery note
for which is which before you apply. An unusually large member of one of these rebuildable
categories may briefly show `method=vault` in a per-item report instead of `direct_delete` — a
cost-budget guard (ADR-0003/ADR-0032) routes it through the vault and then purges it back out
within the same `apply` call, rather than skipping its identity re-verification. The end result
is identical from your perspective (bytes gone by the time `apply` returns, real, measured
disk-free delta), just via a different internal mechanism for that one item.

```powershell
# From the CLI:
reclaim undo <batch_id>

# Or from the dashboard's Quarantine & Restore tab — same manifest, same guarantee, but only for
# vault-quarantined batches (power mode's default method). Under safe mode's default
# recycle_bin method, restore is Windows' own Recycle Bin — see "How to restore something" above.
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
- **Prebuilt releases are published** on [GitHub Releases](https://github.com/gaurav-gandhi-2411/reclaim/releases/latest)
  (`reclaim-setup.exe`, starting at v1.0.0) — that's the artifact the Download section at the
  top of this README links to. The build-it-yourself instructions below remain useful for
  verifying the binary yourself from source, or for building a fresh copy.
- **AI layer bundled, zero user steps** (Wave 1 P0-B, [ADR-0030](docs/architecture/adr/0030-onnx-conversion-and-bundled-ai-installer.md) — supersedes ADR-0024's
  original core-only decision). CLIP and MiniLM ship as pre-converted, pinned, SHA256-verified
  ONNX files (CLIP fp16, 175.8MB; MiniLM int8, 23.6MB — **199.4MB total**, vs. the old
  torch-based `[ai]` extra's ~1,028MB delta) bundled directly into the installer — no separate
  `pip install reclaim[ai]` step, no terminal, no Python knowledge required. Quality-parity
  measurement against the original torch models (on this project's own real gold eval sets, not
  a proxy): `reports/ai/onnx_quality_parity/`. Models load lazily (first AI-analysis click, never
  at app startup) and degrade gracefully — if a bundled model file is ever missing or corrupted,
  that specific AI track is skipped with a clear reason, the rest of the app (including every
  other AI track) keeps working.
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
uv run python packaging/build_brand_assets.py   # regenerates packaging/reclaim.ico + wizard bitmaps

# Requires Git LFS (git lfs pull) so the bundled clip_vision_fp16.onnx (175.8MB, exceeds
# GitHub's 100MB plain-git limit) is a real file, not a pointer stub — see
# packaging/RELEASE_RUNBOOK.md's preconditions (free RAM, disk space, closing other heavy
# sessions) before running a real release build.
pwsh packaging/build_installer.ps1
# -> packaging\dist\reclaim-setup.exe
```

`build_installer.ps1` runs the whole pipeline unattended and reproducibly: provisions a clean,
dev-toolchain-free build venv (`uv sync --extra ai --no-dev`, with a hard assertion that
`mypy`/`pytest`/`ruff` are genuinely unimportable before Nuitka starts — a contaminated venv
previously caused an out-of-memory Nuitka failure trying to compile mypy's own internals),
compiles with Nuitka `--standalone` (every AI-layer package needs an explicit
`--include-package` — Nuitka's static analysis doesn't follow this codebase's lazy
`importlib.import_module()` calls), monitors the compile for machine-contention stalls (aborts
within minutes rather than hanging for hours if compiler output stops while system CPU isn't
actually busy — a real incident on a shared dev machine, see PLAN.md's Wave 1 P0-B checkpoint),
then packages with Inno Setup and reports the final installer size. Full preconditions and
expected numbers: `packaging/RELEASE_RUNBOOK.md`.

(`--windows-console-mode=attach`, baked into the script: the Start Menu/desktop shortcut
launches `reclaim.exe dashboard` with no console around it, so `attach` means no console window
pops up for that path; `reclaim.exe scan ...` run from an existing terminal still gets its
stdout in that terminal — `disable` would silently drop it, `attach` uses whatever console
already exists and creates none otherwise.)

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
