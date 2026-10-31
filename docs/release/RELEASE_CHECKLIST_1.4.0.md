# Release checklist: Reclaim 1.4.0

Status: **prepared, nothing published.** No tag, no GitHub release, no announcement exists for
1.4.0. Steps marked **[OWNER ONLY]** must be done by the repository owner, not by a session or an
agent.

This list sits on top of [`packaging/RELEASE_RUNBOOK.md`](../../packaging/RELEASE_RUNBOOK.md)
(preconditions, build, "After a successful build", the VM gate, the SHA-256 sidecar format). It
does not repeat those procedures; follow the runbook section named in each step. Release notes
source: [`CHANGELOG.md`](../../CHANGELOG.md).

Every step records its result in the table at the end (command, output, date, SHA). A step with no
recorded result is not done.

## 0. Before anything

- [ ] **[OWNER ONLY] Merge #199** (`integration/run-2026-10` into `main`, merge commit so every
      branch stays traceable). It touches the delete path and fails the merge gate on size, so it
      is not self-merged. After it merges, close #193, #202 and #203 (stated in #199).
- [ ] **[OWNER ONLY] Merge the release-prep PR** (`chore/release-prep-1.4.0`, version 1.4.0,
      changelog, docs, templates). Retarget its base to `main` first if #199 has merged, then
      merge. Do not merge it before #199 content is on `main`: the version bump alone would make
      an older `main` claim 1.4.0.
- [ ] `main`'s CI is green on the merge commit (all required checks; pip-audit included). Record the
      run URL.
- [ ] Note the exact **main SHA** to build: `git rev-parse origin/main` after `git fetch`. The build
      and everything below refer to this one SHA.
- [ ] `uv.lock` records the project as 1.3.0 (it was not edited in the prep branch). Regenerate it
      with `uv lock` in a throwaway checkout, confirm the only change is the project version line,
      and land it as its own small PR before building if `uv sync --frozen` in the build rejects
      the mismatch. Record what happened.

## 1. Build from the merged main SHA

Follow runbook sections "Preconditions" and "Running the build".

- [ ] Preconditions met: other heavy sessions closed, at least 8 GB free RAM, Git LFS pulled
      (`git lfs pull`; the CLIP `.onnx` must be a real file, not a pointer), a current Microsoft
      Visual C++ Redistributable (x64) installed, no soak or other heavy job running.
- [ ] Clean checkout of the main SHA from step 0 (not a feature branch, not a dirty tree).
- [ ] `pwsh packaging/build_installer.ps1` completes. Record wall time, dist folder size and installer
      size (runbook "Expected numbers" is the table to update, with the SHA).
- [ ] The build's source-commit sidecar names the main SHA (`packaging\dist`, written by the build;
      PR #73).
- [ ] `reclaim.exe --version` on the built exe prints `reclaim 1.4.0`.
- [ ] Runbook "After a successful build" item 1: the bundled CLIP model is a real file.
- [ ] Runbook item 2b: copy `packaging\build\compile_breakdown\summary.md` under
      `reports/build-timing/<date>-<label>/` if this build is the one being shipped.

## 2. Packaged-artifact smokes (the compiled build, not the dev tree)

Both take the built dist folder; run them from the repository root. Each must exit 0.

- [ ] `pwsh -File packaging\test_packaged_safe_mode.ps1 -DistDir packaging\build\entry_point.dist`
      (safe mode survives packaging; typed confirmation is the only door to power mode).
- [ ] `pwsh -File packaging\test_packaged_serve.ps1 -DistDir packaging\build\entry_point.dist`
      (serve starts, a scan completes, an AI analysis completes with no import-shaped skip).
- [ ] The frozen smoke suite (PR #55): `pwsh packaging\smoke\run_frozen_smoke_suite.ps1 -InstallPath
      <the folder under test>`. Its default `-InstallPath` is the real installed app, so pass the
      path explicitly (read its header for what that folder must contain); it includes an MCP stdio
      round trip (`packaging\smoke\mcp_stdio_probe.py`).
- [ ] Record each result as PASS/FAIL with the command line and the printed summary. A FAIL means
      fix the code or the build, never edit the test to pass.

## 3. Two-hour soak (frozen exe)

Procedure and verdict rules: `packaging/smoke/SOAK.md`. Run on a quiet machine, alone: it
competes with any build or other heavy job for the same memory.

- [ ] `python packaging\smoke\soak_serve.py --exe <path to the built reclaim.exe> --duration-minutes 120`
- [ ] Exit code 0 (PASS). 1 is a leak signal, 2 is inconclusive, 3 is a harness error; none of
      them is a pass. Keep `soak_report.md` and `memory_curve.svg` (copy a summary into the
      release record; do not commit raw samples).
- [ ] The soak used the built exe from step 1, not the installed copy and not a dev venv.

## 4. Clean machine or VM: install, first run, upgrade, uninstall

Needs a clean Windows install with no Python and no dev tools. Runbook "After a successful build"
item 3 (the VM gate, steps a to k) is the script to walk. 1.4.0 additions are below.
**[OWNER ONLY]** if the owner's VM or machine is the only one available.

- [ ] **Install** (runbook 3a): no UAC prompt, per-user, the app starts and the browser opens.
- [ ] **First-run flow** (runbook 3b and 3c): the "Before you start" overlay blocks the page until
      "I understand, continue"; Simple mode; "Clean My Computer" scans and finishes without an
      error banner.
- [ ] Switch to Advanced; tabs are Overview, Storage Treemap, Review Queue, AI Suggestions,
      Quarantine & Restore, **Move to Another Drive**, Settings (runbook 3d lists six; 1.4.0
      has the extra tab).
- [ ] **Agent Leftovers:** Settings shows the category; it is off, and cannot be enabled in safe
      mode. `reclaim.exe agent-artifacts` runs and prints a dry run.
- [ ] **Move to Another Drive** (needs a second fixed drive or VHD): the page lists candidates; "Check
      this move" on a small test folder shows a plan and changes nothing; "Move it" moves it behind
      a junction; "Move it back" restores it. Use a throwaway folder, never real data.
- [ ] **Assistant setup:** Settings > "Use Reclaim from your AI assistant" shows copy-ready text;
      Reclaim does not edit any client config.
- [ ] **Weekly clean and 80% alert** (Settings): turning each on registers its scheduled task;
      turning it off removes it (runbook items on Task Scheduler apply).
- [ ] Apply and restore (runbook 3f and 3g), safe mode: the file reaches the Recycle Bin and
      restores.
- [ ] **Upgrade:** install the previous release (1.3.0) first, create a config and some data, then
      run the 1.4.0 installer over it. `config.toml` and data survive, the weekly task is
      re-registered only if the toggle was on, the version shown is 1.4.0.
- [ ] **Uninstall** (runbook 3k): the data prompt defaults to No; a silent uninstall keeps config
      and data and removes both scheduled tasks. Reinstalling puts the tasks back.
- [ ] Each step's pass/fail recorded, with the VM or machine description and the installer SHA-256.

## 5. SHA-256 sidecar

Follow runbook "Publishing: SHA-256 checksum sidecar".

- [ ] `packaging\dist\reclaim-setup.exe.sha256` exists, is 84 bytes, lowercase hex, two spaces, the
      bare filename, LF line ending, no BOM.
- [ ] `Get-FileHash packaging\dist\reclaim-setup.exe -Algorithm SHA256` equals the sidecar's hash.
- [ ] The installer that was smoke-tested and VM-tested (steps 2 to 4) has this exact hash. If the
      installer was rebuilt after any test, the tests were run on a different file: repeat them.

## 6. Final pre-publish review (still nothing public)

- [ ] `CHANGELOG.md`: change "unreleased" to the release date; reread "Known limits".
- [ ] README: the Download link and numbers still match what was measured in steps 1 to 5
      (`docs/release/README_NUMBERS_AUDIT.md` lists every number and its source).
- [ ] The release notes text is drafted from the changelog and includes the unsigned-binary
      SmartScreen and antivirus explanation and the "residual risk" paragraph that earlier
      releases carried (runbook "Publishing: what a user actually sees on an unsigned installer").
- [ ] `SECURITY.md` supported-versions table reads correctly for the new release.

## 7. Publish **[OWNER ONLY]** (every step below)

Not done by a session or an agent; none of these has been done.

- [ ] **[OWNER ONLY]** Turn on private vulnerability reporting (repo Settings > Code security).
      It is off today (`gh api repos/gaurav-gandhi-2411/reclaim/private-vulnerability-reporting`
      returned `{"enabled":false}` on 31 Oct), and `SECURITY.md`'s main reporting path needs it.
- [ ] **[OWNER ONLY]** Tag the verified main SHA: `git tag -a v1.4.0 <main SHA> -m "Reclaim 1.4.0"`,
      then push the tag. The tag must point at the SHA that was built and tested.
- [ ] **[OWNER ONLY]** Create the GitHub release for `v1.4.0` with `reclaim-setup.exe` and
      `reclaim-setup.exe.sha256` as the only two assets, notes from the changelog.
- [ ] **[OWNER ONLY]** Verify the published assets: download both, compare hashes with `Get-FileHash`
      and the sidecar, run the downloaded installer once on the clean machine.
- [ ] **[OWNER ONLY]** Check that the in-app update check (off by default) and the README "latest
      release" link resolve to the new release.
- [ ] **[OWNER ONLY]** Announcement, if any (where and what is the owner's call;
      `docs/release/DEMO_GIF_SCRIPT.md` has the shot list for the demo GIF).

## Result record (fill in as steps complete)

| Step | Command or action | Result | Date | SHA or file |
|---|---|---|---|---|
| 0 merge #199 | | | | |
| 0 main CI green | | | | |
| 1 build | | | | |
| 2 safe-mode smoke | | | | |
| 2 serve smoke | | | | |
| 2 frozen suite | | | | |
| 3 soak (120 min) | | | | |
| 4 VM install, first run | | | | |
| 4 upgrade from 1.3.0 | | | | |
| 4 uninstall | | | | |
| 5 SHA-256 sidecar | | | | |
| 7 tag, release, announcement | | | | |
