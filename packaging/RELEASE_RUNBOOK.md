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

## Expected numbers (measured 2026-07-30, this machine, uncontended)

| Metric | Expected |
|---|---|
| CLIP fp16 model | 175.8 MB |
| MiniLM int8 model + tokenizer | 24.3 MB |
| `[ai]` extras Python dependency closure (torch-free) | several hundred MB (onnxruntime/faiss/lightgbm/opencv/scipy — not yet precisely measured post-build; update this line once a clean build completes) |
| Dist folder total | TBD — fill in from the first successful `build_installer.ps1` run |
| Final `reclaim-setup.exe` | TBD — target: comfortably bundleable; if it lands somewhere unacceptable, the documented fallback is an in-app "Enable AI features" one-click download (progress/resume/checksum) instead of bundling |
| Wall time, uncontended | Not yet measured end-to-end (both real attempts hit build-environment problems before finishing) — expect this section to be filled in from the first clean run; `faiss`'s own wrapper alone took ~750s in the contended run, so budget at least 20-30 minutes total, likely more |

**Do not treat a number in this table as validated until it's been updated with a real
uncontended measurement and the commit SHA that produced it** (rule 65b — metric provenance).

## Running the build

```powershell
pwsh packaging/build_installer.ps1
```

Runs unattended: provisions a clean build venv (`uv sync --extra ai --no-dev`, asserts the dev
toolchain is genuinely absent), pre-flight-checks free RAM, runs the monitored Nuitka compile
(aborts within `StallTimeoutSeconds` — default 5 minutes — of detected contention rather than
hanging for hours), then packages with Inno Setup and reports the final installer size.

Progress/telemetry: `packaging/build/nuitka_build.log` (raw Nuitka output) and
`packaging/build/nuitka_build_telemetry.csv` (elapsed time / free RAM / CPU% / last log line,
sampled every 15s — this is what makes a stalled build visible instead of silent).

If it aborts with a contention message: close other workloads, confirm free RAM per the table
above, and retry. If it fails with a "contaminated" message: something is wrong with the venv
provisioning itself (not contention) — investigate before retrying, don't just re-run.

## After a successful build

1. **Verify the dist folder actually contains the AI models**:
   `Test-Path packaging\build\entry_point.dist\reclaim\ai\models\clip_vision_fp16.onnx` should
   be `True` and the file should be ~175.8MB, not a Git LFS pointer stub (~130 bytes) — the
   single most likely silent-failure mode if Git LFS wasn't pulled before the build.
2. **Run `packaging/test_packaged_safe_mode.ps1`** against the fresh dist folder — the existing
   safe-mode-survives-packaging proof, unaffected by this change but still the right gate before
   calling a build releasable.
3. **Run the fresh-Windows-VM gate** (a real machine/VM only GG can do, per the standing
   AUTONOMY MANDATE's escalation list): install → first successful full scan → AI features
   working with zero developer steps → duplicates found → vault move → restore. This is the
   actual bar for "P0-B is done," not just a successful compile.
4. Update the "Expected numbers" table above with the real measurement + commit SHA.
