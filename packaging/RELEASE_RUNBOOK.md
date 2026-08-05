# Release build runbook

How to build `reclaim-setup.exe` reliably — written after a real 2026-07-30 incident where an
unmonitored build stalled for hours under machine contention with no signal anything was wrong.
See PLAN.md's Wave 1 P0-B checkpoint for the full incident writeup.

## Preconditions — check BEFORE starting

| Precondition | Why |
|---|---|
| **Close other heavy sessions** (other Claude Code sessions, training runs, other builds, browser tabs with many open dev-tools) | The AI-bundled build compiles `onnxruntime`/`faiss`/`lightgbm`/`scipy`/`opencv` from source in several places — real CPU- and memory-hungry work. A shared machine under contention doesn't fail loudly on its own; it just gets very slow (observed: trivial files taking 20-50 minutes each instead of seconds), and `build_installer.ps1`'s stall detector will abort the build rather than let it run for hours, but that's a wasted attempt either way. |
| **≥8GB free RAM** before starting | `build_installer.ps1` checks this and refuses to start below it. `faiss`'s SWIG-generated C++ wrapper alone needs several GB to compile in one pass. |
| **≥15GB free disk** under `packaging/build/` | The Nuitka dist folder + intermediate `.build` artifacts + the Nuitka compiler cache together run several GB; the `.venv-build` scratch venv (torch-free `[ai]` extras) is a few hundred MB more. |
| **Git LFS installed** (`git lfs version`) | The bundled `clip_vision_fp16.onnx` (175.8MB) is tracked via Git LFS (exceeds GitHub's 100MB plain-git limit) — a checkout without `git lfs pull` gets a pointer file, not the real model, and the build will bundle a broken/tiny file without any error until someone actually tries to use CLIP. |
| **Inno Setup 7 installed** at `C:\Program Files\Inno Setup 7\ISCC.exe` (or pass `-InnoSetupCompiler` with the real path) | `build_installer.ps1`'s packaging step. |
| **Elevated (Run as Administrator) PowerShell, if you want the Defender-exclusion speedup** | Step 4 tries `Add-MpPreference -ExclusionPath` for `packaging/build/` and Nuitka's own compiler-cache dir — real-time scanning of thousands of generated `.c`/`.o` files is a measurable multiplier. Best-effort only: a non-elevated session logs a SKIPPED line and the build still succeeds, just without the speedup. Pass `-SkipDefenderExclusions` to opt out entirely. |

## Expected numbers

| Metric | Expected | Status |
|---|---|---|
| CLIP fp16 model | 175.8 MB | Confirmed (2026-07-30) |
| MiniLM int8 model + tokenizer | 24.3 MB | Confirmed (2026-07-30) |
| `[ai]` extras Python dependency closure (torch-free) | several hundred MB (onnxruntime/faiss/lightgbm/opencv/scipy) | Confirmed (2026-08-05) — dist folder total below includes this closure |
| Dist folder total | 884.0 MB | Confirmed (2026-08-05), `packaging/build_installer.ps1`. Grew from an initial 762.7MB measurement once `--nofollow-import-to=*.tests`/`*.testing` was removed entirely (see wall-time row) — the larger number is the functionally-correct one. |
| Final `reclaim-setup.exe` | 309.3 MB | Confirmed (2026-08-05) — comfortably bundleable, no fallback download needed |
| Wall time, quiet machine, `--jobs=1`, `-O2`-patched Nuitka | 180.4 minutes clean / ~15-100+ minutes incremental depending on what changed | Confirmed (2026-08-05). Not `--jobs=4`-`6` as originally planned: a real, reproducible `cc1.exe` OOM was found compiling faiss's SWIG-generated `swigfaiss.c` (a single ~20GB+-peak-memory translation unit) even on a fully quiet machine with 24GB+ free RAM — `--jobs` alone can't fix a single-file ceiling. Fix required `--low-memory` (drops gcc's `-pipe`) + `--jobs=1` (no other file competing for RAM while it compiles) + patching the build venv's bundled Nuitka to use `-O2` instead of `-O3` (Nuitka hardcodes `-O3` for every gcc compile on Windows with no working CLI/env override — verified by reading `SconsCompilerSettings.py`). A SECOND real bug surfaced after that first successful build: `--nofollow-import-to=*.tests`/`*.testing` (meant only to skip numpy/scipy's own test suites and save compile time) also excluded `structlog.testing`, `jinja2.tests`, and `scipy._external.array_api_extra.testing` — all three unconditionally imported by their package's normal runtime code, not test-only. The packaged CLI/server crashed on every invocation despite `test_packaged_safe_mode.ps1` passing (that test never actually starts `reclaim.exe serve`, only CLI subcommands that happened not to touch the broken import paths). Fixed by removing the test-exclusion flags entirely — verified via `ast`-scanning the whole build venv's site-packages for this exact pattern (10 hits, 3 genuine runtime deps) before committing to the fix. The `-SkipCleanBuildDirs` flag (new) lets a narrowly-scoped fix reuse Nuitka/Scons' incremental object-file cache instead of a full clean rebuild — use it for anything that doesn't change `--low-memory`/`--jobs`/compiler-flag-level settings. See `build_installer.ps1`'s inline comments for the full incident history and `git log` on that file for the sequence of fixes. Budget 3+ hours for a from-scratch build until/unless this moves to a beefier CI runner (deferred roadmap item). |
| AI path end-to-end (packaged binary, not dev venv) | Verified (2026-08-05) | `serve` + `/api/scan` + `/api/ai/analyze` against generated gold fixtures (15 near-dup images, 14 near-dup docs, deterministic seed=42 via `evals/ai_fixtures/build_*_fixtures.py`). `tracks_run` included both `semantic_image` (CLIP) and `near_dup_document_and_version_chain` (MiniLM), `tracks_skipped` empty. Results matched ground truth exactly: 3/3 image near-dup clusters with correct membership, 3/3 document version chains with correct membership, all distractors correctly left ungrouped (except a reasonable `semantic_image` grouping of the 3 image distractors — a different, broader-similarity track, not a false positive). |
| CLI cold start, dev venv (**not** the packaged binary — see note) | `reclaim --version`: min 695.5 ms / median 794.2 ms / p95 1007.4 ms (n=15) | Confirmed (2026-08-05), commit `667fe02` (`.venv\Scripts\reclaim.exe --version`, `uv sync --frozen` dev venv, timed via a PowerShell `[System.Diagnostics.Stopwatch]` loop, one process launch per sample). **This worktree has no packaged binary** — `packaging/build/` and `packaging/dist/` are gitignored and were genuinely absent on disk at measurement time (a fresh worktree checkout, not a stale-but-present build dir) — so this is the dev-venv number, explicitly not the Nuitka `.exe`; re-measure against the real packaged binary once one exists in this worktree. Before adding `--version`, the fastest available subcommand (`reclaim mode`) measured min 667.5 ms / median 692.5 ms / p95 873.5 ms (n=10, same commit's import chain, commit `dcfa8c2`) — statistically indistinguishable from the `--version` number above, as expected: no import-deferral change was made, since total import overhead (`python -X importtime -c "from reclaim.cli import main; main(['mode'])"`) was measured at ~628ms cumulative for `reclaim.cli`, comfortably under the ~2s budget. That importtime trace also confirms `reclaim.ai` (onnxruntime/torch/PIL/faiss) is never imported for a `mode`/`--version`-class invocation — the ~628ms is dominated by `reclaim.config`'s `pydantic_settings`→`importlib.metadata` chain (~466ms cumulative) and `structlog` (~161ms cumulative), both core/architectural imports per the eager-import list in cli.py, not the AI layer (matches ADR-0025's isolation boundary). `--version` exists specifically so a future packaged-binary/CI cold-start measurement doesn't have to pick an arbitrary subcommand as a stand-in. |

**Do not treat a number in this table as validated until it's been updated with a real
uncontended measurement and the commit SHA that produced it** (rule 65b — metric provenance).

## Running the build

```powershell
pwsh packaging/build_installer.ps1
```

Runs unattended in 6 steps: reports current machine state (free RAM, instantaneous CPU%, any
process with >30s cumulative CPU time) so contention is visible before committing to a build;
provisions a clean build venv (`uv sync --extra ai --no-dev`, asserts the dev toolchain is
genuinely absent); pre-flight-checks free RAM and picks `--jobs` (6 if free RAM ≥16GB at
preflight, else 4 — only meaningful on a verified-quiet machine, since raising parallelism under
real contention just adds more processes competing for the same starved cores); best-effort adds
Windows Defender exclusions; runs the monitored Nuitka compile (aborts within
`StallTimeoutSeconds` — default 5 minutes — of detected contention rather than hanging for
hours); then packages with Inno Setup and reports the final installer size.

Progress/telemetry: `packaging/build/nuitka_build.log.stderr` (Nuitka writes almost everything
here, not to stdout) and `packaging/build/nuitka_build_telemetry.csv` (elapsed time / free RAM /
CPU% / last log line, sampled every 15s — this is what makes a stalled build visible instead of
silent).

If it aborts with a contention message: check the Step 1 process listing it printed at the
start (or re-run to get a fresh one), confirm free RAM per the table above, and retry only once
you can see the contention is actually gone — don't retry blind. If it fails with a
"contaminated" message: something is wrong with the venv provisioning itself (not contention) —
investigate before retrying, don't just re-run.

## After a successful build

1. **Verify the dist folder actually contains the AI models**:
   `Test-Path packaging\build\entry_point.dist\reclaim\ai\models\clip_vision_fp16.onnx` should
   be `True` and the file should be ~175.8MB, not a Git LFS pointer stub (~130 bytes) — the
   single most likely silent-failure mode if Git LFS wasn't pulled before the build.
2. **Run `packaging/test_packaged_safe_mode.ps1`** against the fresh dist folder — the existing
   safe-mode-survives-packaging proof, unaffected by this change but still the right gate before
   calling a build releasable.
3. **Run the fresh-Windows-VM gate** (a real machine/VM only GG can do, per the standing
   AUTONOMY MANDATE's escalation list — needs a clean Windows install with no Python, no dev
   tools, nothing this session's own testing already touched). This is the actual bar for
   "P0-B is done," not just a successful compile or a scripted API check. Copy
   `packaging\dist\reclaim-setup.exe` to the VM (USB/share/whatever — not git, not this repo) and
   walk through, in order, stopping and reporting back at the first thing that doesn't match:

   a. **Install.** Double-click `reclaim-setup.exe`. No admin/UAC prompt should appear (per-user
      install). Leave "Create a desktop shortcut" unchecked (its default) unless you want one.
      Finish the wizard with "Launch Reclaim" checked (its default) — the app should start and
      your default browser should open to the dashboard within a couple seconds
      (`webbrowser.open` fires ~1s after the server starts).
   b. **First-run overlay.** A "Before you start" modal should block the page until you click
      "I understand, continue." It should say: starts in Simple mode, Safe mode is on by
      default, deletes go to the Recycle Bin in Safe mode, some categories are off by default,
      Power mode is opt-in. If this overlay is missing, garbled, or the page is blank/errors —
      stop, that's a packaging bug (e.g. a missing static asset), not a "which button do I
      click" question.
   c. **First successful full scan.** Click the big "Clean My Computer" button (Simple mode,
      no path to type). Progress should render inline and finish without an error banner.
   d. **Switch to Advanced mode** (top-right "Switch to Advanced" link) to reach the AI/vault
      features. You should see tabs: Overview, Storage Treemap, Review Queue, AI Suggestions,
      Quarantine & Restore.
   e. **AI features, zero developer knowledge.** On the "AI Suggestions" tab, click "Analyze
      with AI." This should complete (not spin forever, not error) and list groups — near-
      duplicate photos, possible document drafts, screenshot bursts, or similar-scene groups,
      depending on what's actually on this machine. If it reports nothing found, that's fine IF
      the machine genuinely has no such files; if you know it does and nothing shows up, that's
      a real bug to report, not a "maybe it's just quiet" shrug.
   f. **Duplicates found → vault move.** On the "Review Queue" tab (or acting on an AI
      suggestion), pick a small, low-stakes batch of items. Note the "Quarantine method"
      dropdown — in Safe mode (the default) it's forced to Windows Recycle Bin regardless of
      what you pick, by design (rule: safe mode never uses the vault or a direct delete). Click
      "1. Preview (dry-run)" first and actually read what it says it's about to do, THEN
      "2. Confirm real apply." Verify the file(s) are actually gone from their original
      location afterward.
   g. **Restore.** Go to "Quarantine & Restore." You should see the batch you just created
      (id, method, timestamp, item/byte counts). Click "Restore batch" and verify the file(s)
      reappear at their original location, byte-identical (open one and confirm it's not
      truncated/corrupted).
   h. **(Optional, only if you want to exercise the vault path specifically, not just Recycle
      Bin):** switch to Power mode via the mode badge in the top header — this requires typing
      an exact confirmation phrase, by design, so it can't happen by accident. Repeat f–g with
      "Vault (restorable)" selected in the Quarantine method dropdown instead of Recycle Bin.
      Switch back to Safe mode afterward (no confirmation needed for that direction).
   i. **Uninstall.** From the Start Menu, run "Uninstall Reclaim." It should prompt "Also delete
      this data folder now?" — default answer is No (data under `data\` including anything
      still in the vault survives uninstall by default, matching the "never lose data by
      surprise" posture). Confirm the app is actually removed from Programs/Start Menu after.

   Report pass/fail per step, not just an overall verdict — a step that "mostly worked but the
   wording was a little off" is still useful signal, not a failure to hide.
4. Update the "Expected numbers" table above with the real measurement + commit SHA.

## Publishing: SHA-256 checksum sidecar (manual — the build script does NOT do this)

`build_installer.ps1` does not produce a checksum. Verified on 2026-08-05 against the 590-line
script: it contains no `Get-FileHash`, no `sha256`, and no checksum step at all — its last action
is to print the installer size. **If you forget this step, the release ships without a checksum
and nobody will notice until a user asks how to verify the download.**

Every release so far has one, produced by hand at publish time. Confirmed via
`api.github.com/repos/gaurav-gandhi-2411/reclaim/releases` on 2026-08-05: v1.0.0, v1.1.0, v1.2.0
and v1.3.0 each carry exactly two assets, `reclaim-setup.exe` and `reclaim-setup.exe.sha256`
(84 bytes every time).

Generate the sidecar in the `sha256sum`-compatible format the existing releases use — lowercase
hex, **two** spaces, the bare filename with no path, and a trailing LF:

```powershell
$exe = "packaging\dist\reclaim-setup.exe"
$h = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
# WriteAllText with a BOM-less UTF8Encoding and an explicit `n (not `r`n, not Set-Content):
# reproduces the published sidecars byte-for-byte at 84 bytes. A BOM or a CRLF would make
# `sha256sum -c` fail for anyone verifying on Linux/macOS/WSL.
[System.IO.File]::WriteAllText("$exe.sha256", "$h  reclaim-setup.exe`n", [System.Text.UTF8Encoding]::new($false))
```

v1.3.0's published sidecar is exactly
`7f02ab7b...d7c3` + two spaces + `reclaim-setup.exe` + `\n` = 84 bytes, no BOM (fetched and
byte-inspected 2026-08-05); the command above was run against a dummy file on the same day and
produced the identical 84-byte shape.

Upload **both** files as release assets, named exactly `reclaim-setup.exe` and
`reclaim-setup.exe.sha256`. Then re-download the published asset and hash it again — the point is
to catch a corrupted or truncated upload, so hashing the local file you just built proves nothing.
v1.3.0's published sidecar reads
`7f02ab7b488e51212e7bde0e686c742b448d90073df103da9ce2885f6460d7c3  reclaim-setup.exe`; that
round-trip check is what PLAN.md's "verified byte-identical after downloading the published asset
back" notes refer to, and it is not optional.

### Verification instructions to give end users

Put this in the release notes (and keep the README's download section pointing at it). A checksum
nobody is told how to use is decoration, not integrity.

```powershell
# In PowerShell, in the folder you downloaded both files to:
(Get-FileHash .\reclaim-setup.exe -Algorithm SHA256).Hash.ToLower()
Get-Content .\reclaim-setup.exe.sha256
# The 64-character hash printed by the first command must appear in the second command's output.
# If it doesn't match, delete the download and do not run it.
```

Be honest in the release notes about what this does and doesn't prove: the checksum confirms the
file arrived intact and matches what was published on this repo's releases page. It is **not** a
publisher signature — it does not prove who built it, and a user who fetched both the installer
and the sidecar from the same tampered mirror learns nothing. Only code signing gives that, and
Reclaim is unsigned (see below and ADR-0031).

## Publishing: what a user actually sees on an unsigned installer

`reclaim-setup.exe` and the `reclaim.exe` it installs are **unsigned**. ADR-0031 records the
current code-signing options, the recommendation, and the fact that nothing is purchased pending
GG's go-ahead. Until that changes, every first-time user hits the flow below, and release notes,
SUPPORT.md answers and the landing page should describe it accurately rather than paraphrase it.

**1. Browser download warning (before SmartScreen ever runs).** Because the file is a rarely
downloaded `.exe`, the browser's own reputation check fires first — in Microsoft Edge the download
is flagged as not commonly downloaded and must be kept via the download flyout's overflow (`...`)
menu; Chrome shows an equivalent keep/discard prompt. Wording here changes between browser
releases, so don't quote it verbatim in user-facing docs; describe the action ("choose to keep the
file") instead.

**2. Microsoft Defender SmartScreen's unrecognised-app dialog**, when the downloaded installer is
launched. A blue full-screen-style dialog appears, titled **"Windows protected your PC"**, with
the body **"Microsoft Defender SmartScreen prevented an unrecognized app from starting. Running
this app might put your PC at risk."** The only visible button is **"Don't run"** and it holds
keyboard focus by default — there is deliberately no "run" button until the user expands the
**"More info"** link. Doing so reveals a small details table (`App:` = `reclaim-setup.exe`,
`Publisher:` = **`Unknown publisher`**) and *then* a **"Run anyway"** button. Two clicks, past a
red-flag-shaped screen, before Reclaim's own installer wizard is ever seen.

> Provenance for the wording above, stated precisely because it is user-facing copy. The
> `Publisher:`/`App:` field labels, the literal default value `Unknown Publisher`, and the
> two-button run/don't-run pattern with focus defaulting to don't-run were extracted directly on
> 2026-08-05 from this machine's own `C:\Windows\System32\en-US\smartscreen.exe.mui`
> (FileDescription "Windows Defender SmartScreen", version 10.0.26100.8117) — although the
> template found there is the *"SmartScreen can't be reached right now"* offline variant, not the
> unrecognised-app variant. A byte search for the UTF-16 strings `Windows protected your PC` and
> `unrecognized app` across `System32`, `System32\en-US`, `SystemResources`, `SystemApps` and
> `Program Files\WindowsApps` returned **zero** hits, so the title and body sentences above are
> **not** sourced from a Microsoft artifact in this pass — they carry over from README.md's
> existing SmartScreen section, which was written from observing the real prompt during earlier
> packaging work. Treat them as observed-and-plausible rather than extracted, and re-check against
> a live prompt on the fresh-Windows-VM gate (step 3a above) before quoting them in new
> user-facing copy.

**3. Antivirus false positives.** Independent of SmartScreen, some engines heuristically flag
freshly compiled, unsigned Nuitka output. This project has already hit one real AV quarantine on a
freshly built binary (PLAN.md, 2026-07-23) — treat it as a known cost of shipping unsigned, not a
hypothetical one. README.md's "First run: SmartScreen and antivirus prompts" section carries the
restore-from-quarantine and folder-exclusion instructions for users; keep the two in sync.

**What signing would and would not fix.** Do not write release notes promising that a future
signed build installs silently. Per ADR-0031's research, current Microsoft documentation describes
SmartScreen as reputation-based over the file, app and certificate, and no certificate type — EV
included — grants instant reputation any more. Signing replaces `Unknown publisher` with a
verified name and starts a reputation clock; the warning itself clears with download volume over
time, not at purchase.
