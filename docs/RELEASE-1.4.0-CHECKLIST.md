# Reclaim 1.4.0 release-candidate checklist (build 14)

Status: **prepared, nothing published.** No tag, no GitHub release, no announcement. This file
records each RC-gate check as PASS / FAIL / NOT RUN with its evidence. Build 14 is the first build
from `main` with 1.4.0 complete (#240, the 1 Nov release PR, merged by GG; #245 on top). Build 13's
record is kept below the build 14 table for comparison.

Recorded 3 Nov 2026 (IST). Every number comes from a command run on this build, named next to it.

## Summary (build 14, main `e14a42e`)

| # | Check | Result |
|---|---|---|
| 1 | Built from the release SHA on `main` | PASS (`e14a42e`) |
| 2 | Build completes; source-commit sidecar | PASS |
| 3 | `--version` prints 1.4.0 | PASS |
| 4 | Bundled models are real files, sizes match the README | PASS |
| 5 | SHA-256 sidecar format and hash | PASS |
| 6 | Packaged safe-mode smoke | PASS (14/14) |
| 7 | Packaged serve smoke | PASS (13/13) |
| 8 | Frozen smoke suite on the installed build | PASS with #249 (check 3 BLOCKED on main's suite) |
| 9 | In-place upgrade from build 13 preserves state | PASS |
| 9a | Growth alert off by default on the owner's machine, collection on | PASS |
| 9b | The 4-hourly disk-space task completes | **FAIL** (fix in #248) |
| 10 | Full-profile scan on the installed build | NOT RUN (deferred to build 15) |
| 11 | Candidates warm-up on the installed build | NOT RUN (deferred to build 15) |
| 12 | Two-hour soak of the installed exe | NOT RUN (deferred to build 15) |
| 13 | Clean machine or VM install, first run, uninstall | NOT RUN (owner; steps in RESUME.md, WHEN GG HAS TIME) |
| 14 | README and CHANGELOG numbers re-measured | PASS for the model sizes; the rest unchanged from build 13 |
| 15 | Agent Leftovers apply through the CLI by path | PASS (dry run) |
| 16 | Apply and restore on the installed build | PASS via frozen suite check 7; the by-path apply was not repeated |

**Verdict: not releasable as built.** Row 9b is a real defect in 1.4.0 (below): whenever a growth snapshot
is due, Task Scheduler kills the run before the low-disk check. #248 fixes it; build 15
from main after #248 (and #249) must repeat rows 1-9b and run 10-12. Row 13 is the owner's.

## 1-2. Source and build — PASS

- Job `build14-e14a42e`, `END state=done rc=0` at 06:46:34 (started 02:15:34: **4 h 31 min**).
  Defender exclusions skipped (no admin).
- `Built from commit: e14a42e3410063bdda3bff0e82e4bf7e4b75b4f5` (sidecar `reclaim-setup.exe.buildsha`).
- Dist folder 835,562,470 bytes (796.9 MiB); installer **307,382,826 bytes** (293.1 MiB).

## 3. `--version` — PASS

`reclaim.exe --version` on the installed build: `reclaim 1.4.0` (`D:\reclaim-build\tmp\ver14.txt`);
file ProductVersion 1.4.0.0 (`install_snapshot.ps1`).

## 4. Bundled models — PASS

| File | Bytes |
|---|---:|
| `reclaim/ai/models/clip_vision_fp16.onnx` | 175,784,030 |
| `reclaim/ai/models/minilm_int8.onnx` | 23,643,136 |

Equal to build 13's and to the README; real files, not LFS pointers.

## 5. SHA-256 sidecar — PASS

`b8bd52dab4ea2b880c59cddaf5399c833d5a19e5a6fd605806799941967f9097`, equal to `Get-FileHash` of the
installer and of the kept copy `D:\reclaim-build\installer-e14a42e.exe`. Sidecar 84 bytes, lowercase
hex, two spaces, bare filename, LF (`od -c`).

## 6-7. Packaged smokes — PASS

- `test_packaged_safe_mode.ps1`: exit 0, **14 PASS / 0 FAIL** (`D:\reclaim-build\tmp\smoke14_safe.log`).
- `test_packaged_serve.ps1`: exit 0, **13 PASS / 0 FAIL** (`D:\reclaim-build\tmp\smoke14_serve.log`);
  semantic_image, near_identical_image and near_dup_document_and_version_chain ran; no ImportError.

## 8. Frozen smoke suite — PASS with #249

| Suite | Result | Log |
|---|---|---|
| main `e14a42e` | 9 PASS / **4 BLOCKED** / 0 FAIL | `D:\reclaim-build\tmp\frozen14.log` |
| #249 head `cda381a` | **10 PASS / 3 BLOCKED / 0 FAIL** (140 s) | `D:\reclaim-build\tmp\frozen14b.log` |

- The extra BLOCKED was check 3: "reclaim check-disk-space did not exit within 20s". Since 1.4.0 that
  command takes a growth snapshot first (see 9b). #249 waits 300 s and passes `--growth-db` into the
  check's scratch; without it the snapshot went to the installed app's real growth store.
- Still BLOCKED as documented: 3b (human-seen toast), 1d (hardlink seed data), 1e (8.3 username).

## 9. In-place upgrade from build 13 — PASS

- No Reclaim process running (`tasklist`). `installer-e14a42e.exe /VERYSILENT /SUPPRESSMSGBOXES
  /NORESTART` exit 0; `D:\reclaim-build\install14.log` ends at 06:50:26.
- `install_snapshot.ps1` before/after (`D:\reclaim-build\tmp\snap_before14`, `snap_after14`): the only
  difference is `reclaim.exe` (SHA-256 `CB787890...` -> `853B6313...`, version 1.3.0.0 -> 1.4.0.0).
  Unchanged: `config.toml` SHA-256 `1A3081AE...E063` (11,008 bytes), `index_dir`, the three excluded
  projects, both Reclaim tasks (Ready), the AUMID and the `reclaim-notify` handler.
- Install folder after the upgrade: 862,331,738 bytes (of which `data\` 21,810,105).

## 9a. Growth alert default on the owner's machine — PASS

Installed `reclaim.exe serve --port 8437`, then stopped by PID: `GET /api/settings/growth-alert` =
`{"enabled":false,"collection_enabled":true}`; `GET /api/settings/notifications` =
`{"enabled":true,"disk_threshold_percent":80.0}`. `config.toml` has no `[growth]` section, so the
defaults apply (alert off, collection on), as the owner decided on 2 Nov.

## 9b. The 4-hourly disk-space task completes — FAIL (fix in #248)

- The installer registers `Reclaim Disk Space Check (gaura)` with `ExecutionTimeLimit` **PT2M**
  (`packaging/reclaim.iss:375`). 1.4.0's `check-disk-space` takes the growth snapshot (budget 120 s,
  plus index queries outside it) **before** the 80 % check.
- VERIFIED: the installed command with the real config, a scratch `--state` and a scratch
  `--growth-db` took **157.1 s**; the snapshot hit its budget after 4 cache roots (`complete=0`).
- VERIFIED: the task's observed run (06:51:46) ended `0x41306` (terminated by the time limit).
- Scope (corrected by the fact-check): the defect is new in build 14. Build 13 (`519a9c9`) never took
  a scheduled snapshot: `75a4cd9` (snapshot in the 4-hourly task) is not its ancestor. Its checks
  ran on every slot: `data\logs\reclaim.log` has `notifications.check_no_notify` at each
  03:30/07:30/11:30/15:30/19:30/23:30Z slot from 1 to 3 Nov. That is why the growth store was
  empty before 06:52 on 3 Nov, not killed runs.
- On build 14: the first run (06:51:46 IST) was due, killed, and logged no check. The 09:00 run was
  `not_due` (my repro had stored a snapshot at 06:52) and completed (`0x0`, check logged 03:30Z).
  From the 13:00 run on, every run is due again and is killed before the 80 % check, until a
  build with #248 is installed. Installed build 14 is in that state now.
- #248: the check and its toast run first, the limit is PT10M (re-registered on every install), and a
  test pins the limit at least 5 min above the snapshot budget. Verifier: no defect.

## 10-12. Scan, warm-up, soak — NOT RUN (deferred)

Build 14 cannot ship (9b), so the hours these take go to build 15, the build that can. Build 13's
figures (628 s scan, 699 s warm-up, -0.02 MB/h soak) are the baseline.

## 14. README and CHANGELOG numbers

Model sizes re-measured (row 4): PASS. Nothing else measurable changed since build 13's row 14.

## 15. Agent Leftovers apply through the CLI by path — PASS (dry run)

- Listing (`aa14-dry`, `reclaim.exe agent-artifacts --all`): 43 proposals.
- `reclaim.exe apply C:\Users\gaura\stylemaitri-wt-migration-drift-gate --tier B --include-categories
  agent_worktree` (no `--apply`):
  - safe mode (`aa14-w1-dry-wt`): `0/2 tier/root-eligible kept`, method recycle_bin. Agent Leftovers is
    off in safe mode, as designed.
  - power mode through a **scratch** mode log (`--mode-log D:\reclaim-build\tmp\aa14\mode_log.jsonl`,
    job `aa14-w1-dry-wt-power`): **`1/1` kept**, `[DRY-RUN] ... processed=1 succeeded=1`,
    `bytes_moved=6879825`. Build 13 printed `0/0` here (#236's bug).
- The owner's real mode stayed safe throughout (`data\mode_log.jsonl` unchanged); the worktree is in
  place (`Test-Path` True).

## 16. Apply and restore — PASS (frozen suite check 7)

Check 7 (`Scan -> apply (vault) -> undo end-to-end`) PASS in both suite runs. The by-path apply of a
real agent artifact was not repeated this round (row 15 is its dry run).

## 13. Clean machine — NOT RUN

Owner only; exact steps, including the reset of `ReclaimSmokeTest`'s old state, are in RESUME.md
under WHEN GG HAS TIME. Installers in `C:\Users\Public\Downloads\`.

---

## Build 13 record (1 Nov 2026, integration `519a9c9`), kept for comparison

Status: **prepared, nothing published.** No tag, no GitHub release, no announcement. This file
records each RC-gate check, run on **build 13**, as PASS / FAIL / NOT RUN with its evidence. The
procedure each step follows is in `docs/release/RELEASE_CHECKLIST_1.4.0.md` (on #225's branch
`chore/release-prep-1.4.0`, not yet on main) and [`packaging/RELEASE_RUNBOOK.md`](../packaging/RELEASE_RUNBOOK.md).

Recorded 1 Nov 2026 (IST). Every number below comes from a command run on this build. The
command or file is named next to it.

### Summary

| # | Check | Result |
|---|---|---|
| 1 | Built from the release SHA on `main` | **FAIL** (deviation, see 1) |
| 2 | Build completes; source-commit sidecar | PASS |
| 3 | `--version` prints 1.4.0 | **FAIL** (expected; the bump is only in #225) |
| 4 | Bundled models are real files, sizes match the README | PASS |
| 5 | SHA-256 sidecar format and hash | PASS |
| 6 | Packaged safe-mode smoke | PASS |
| 7 | Packaged serve smoke | PASS |
| 8 | Frozen smoke suite on the installed build | PASS with #235 (FAIL on main's suite) |
| 9 | In-place upgrade from build 12 preserves state | PASS |
| 10 | Full-profile scan on the installed build | PASS (628.03 s) |
| 11 | Candidates warm-up on the installed build | PASS (699 s, with a caveat) |
| 12 | Two-hour soak of the installed exe | PASS (−0.02 MB/h) |
| 13 | Clean machine or VM install, first run, uninstall | NOT RUN (no VM; owner only) |
| 14 | README and CHANGELOG numbers re-measured | PASS for what is measurable; 2 N/A, 1 not re-measured |
| 15 | Agent Leftovers apply through the CLI by path | **FAIL** (fix in draft #236) |
| 16 | Apply and restore on the installed build | PASS (apply via build 13 source) |

**Verdict: not releasable as built.** Rows 1 and 3 fail by construction until #199 and #225 are
on main and the release is rebuilt from main; row 15 needs #236 or a known-limit line; row 13 is
the owner's.

### 1. Source: built from the release SHA on main — FAIL (deviation)

- The checklist wants the build from the merged `main` SHA. Build 13 is from
  `integration/run-2026-10` at `519a9c9`, because #199 (integration into main) is not merged.
  `main` alone does not have the 1.4.0 features.
- `519a9c9` = merge of main `9178ede` (#231) into integration. Since `9178ede`, main has added
  docs and #235, and #235 touches only the frozen smoke harness and its test:
  `git diff --name-only 9178ede origin/main -- src packaging tests evals pyproject.toml uv.lock`
  lists `packaging/smoke/http_probe.py`, `packaging/smoke/run_frozen_smoke_suite.ps1` and
  `tests/test_frozen_http_probe.py`, and nothing under `src`. So build 13's product code is main's plus
  #199's, which is the 1.4.0 code without #225's version bump.
- Release build still to do: after #199 and #225 merge, build from that main SHA and repeat
  rows 2-12 on it.

### 2. Build — PASS

- Job `build13-519a9c9` (`D:\reclaim-build\jobs\build13-519a9c9\`), `END state=done rc=0`. Child
  started 00:32:05, ended 04:52:04: **4 h 20 min**. Defender exclusions were skipped (no admin).
- Dist folder 796.0 MiB (834,659,834 bytes); installer **292.9 MiB** (307,170,479 bytes). The
  build tool prints MiB as "MB"; the runbook's figures below use the same unit.
- Source sidecar `reclaim-setup.exe.buildsha` = `519a9c9ea4f1262910d01fc299bb2c49ac34a6ad`.
- C compile: 2,493 `.o` files, 0 ccache hits; scipy 59.1 min (24.6 %) and onnxruntime 28.8 min are
  the largest (the build's `compile_breakdown\summary.md`, kept as
  `D:\reclaim-build\tmp\b13_compile_summary.md`; the build clone is deleted).

### 3. `--version` — FAIL (expected)

- `reclaim.exe --version` on the installed build 13 printed `reclaim 1.3.0`
  (`D:\reclaim-build\tmp\ver13.txt`). `519a9c9:pyproject.toml` has `version = "1.3.0"`; 1.4.0 is only
  on #225. Passes only on the release build from main after #225.

### 4. Bundled models — PASS

`find packaging/build/entry_point.dist -iname "*.onnx" -printf "%s %p"`:

| File | Bytes | README says |
|---|---:|---|
| `reclaim/ai/models/clip_vision_fp16.onnx` | 175,784,030 | 175.8 MB (the exact byte count is in #225's README) |
| `reclaim/ai/models/minilm_int8.onnx` | 23,643,136 | 23.6 MB |
| total | 199,427,166 | 199.4 MB |

Both are real files (sizes are the models', not 134-byte LFS pointers).

### 5. SHA-256 sidecar — PASS

- `sha256sum reclaim-setup.exe` = `4c0ba9d8d7af2c3872e9abcd2bc72f82a3d66c62dbf51c67dcc60c15d29f26d8`,
  equal to the sidecar. Sidecar is 84 bytes, lowercase hex, two spaces, bare filename, LF, no BOM
  (`od -c`).
- The kept copy `D:\reclaim-build\installer-519a9c9.exe` has the same hash; it is the file installed
  in row 9.

### 6. Packaged safe-mode smoke — PASS

`pwsh -File packaging\test_packaged_safe_mode.ps1` (default dist path) in the build clone: exit 0,
**14 PASS, 0 FAIL** (`D:\reclaim-build\tmp\smoke13_safe.log`).

### 7. Packaged serve smoke — PASS

`pwsh -File packaging\test_packaged_serve.ps1`: exit 0, **13 PASS, 0 FAIL**
(`D:\reclaim-build\tmp\smoke13_serve.log`). The AI tracks semantic_image (CLIP), near_identical_image
and near_dup_document_and_version_chain (MiniLM) ran; no ImportError.

### 8. Frozen smoke suite on the installed build — PASS (after #235)

| Suite version | Result | Log |
|---|---|---|
| main before #235 | 8 PASS / 4 BLOCKED / **1 FAIL** | `D:\reclaim-build\tmp\frozen13b.log` |
| #235 head `c158393` (merged as `ac4bbbb`) | **10 PASS / 3 BLOCKED / 0 FAIL** | `D:\reclaim-build\tmp\frozen13e.log` |

- The FAIL was the suite, not the product. Check 7 treated the cold-cache 409 as a failure, and
  the suite's server read the real installed config. Check 5 queried a task name the installer no
  longer uses. All three were fixed in #235.
- Still BLOCKED, as documented in the suite: 3b (a human must see the toast), 1d (needs hardlink
  seed data) and 1e (8.3 short-name username).
- Harness note: a first run with `-OutDir` on D: got a 403 on check 7 (the seed was outside the
  home folder). That was my setup, not a product fault; later runs used `%TEMP%`.

### 9. In-place upgrade from build 12 — PASS

- No Reclaim process was running. `installer-519a9c9.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`
  exited 0. `D:\reclaim-build\install13.log` ran 04:54:42-04:55:25 and ends "Installation process
  succeeded".
- `install_snapshot.ps1` before and after (`D:\reclaim-build\tmp\snap_before13`, `snap_after13`):
  the only difference is the `reclaim.exe` SHA-256 (`A1177594...` to `CB787890...`).
  - Unchanged: the `config.toml` SHA-256 `1A3081AE...E063` (11,008 bytes), `index_dir`, the
    exclusions (all three excluded projects), both Reclaim tasks (Ready), the AUMID and the
    `reclaim-notify` handler.
- Install folder after the upgrade: 844,759,067 bytes (805.6 MiB).
- This is upgrade evidence on the dev machine, not the clean-machine gate (row 13).

### 10. Full-profile scan on the installed build — PASS

- Detached job `scan13`, `reclaim.exe scan C:\Users\gaura --workers 4`, `END state=done rc=0`.
- "2303858 entries under C:\Users\gaura (345432 dirs visited, 194524 written, 2109334 unchanged,
  20720 pruned, 146 skipped/unreadable, 856 guarded-path stat, 2303002 fast-path stat) in
  **628.03s**".

| Build | Scan time | Source |
|---|---:|---|
| 10 (20 Oct) | 2,541.58 s | `D:\reclaim-build\scan10.out` |
| 12 (31 Oct) | 4,827.34 s | job `scan12` |
| **13** | **628.03 s** | job `scan13` |

- The build-12 slowdown did not recur on the installed build. #231 (git status only where it is
  read) is in build 13. The two runs are not like for like: build 12 wrote 364,504 rows and pruned
  213,430; build 13 wrote 194,524 and pruned 20,720. So this run alone does not isolate #231's
  share; #231's own A/B (`AppData`, fresh db each, 709.7 s -> 436.6 s) does.
- Caveats:
  - The index was warm from earlier scans (2,109,334 entries unchanged), as it was for build 12.
  - This is one run, not a distribution.

### 11. Candidates warm-up on the installed build — PASS (with a caveat)

- Detached job `warm13` (`D:\reclaim-build\tools\warm_installed13.py`, installed `reclaim.exe
  serve`, index from config), `END state=done rc=0`, log `D:\reclaim-build\warm_installed13.log`.
- **Ready in 699 s, peak server RSS 958 MB** (47 samples, 15 s apart). Then `/api/summary`
  answered in 0.10 s, 0.07 s and 0.08 s. The script stopped its own server by PID afterwards.
- Build 12's installed warm-up never finished: its log ends at `elapsed=272s`, still computing.
  BELIEVED cause: Claude Code's low-memory reaper (the log does not say why it stopped). So there
  is no build-12 figure. Running it as a detached job is what made this run possible.

| Build | Warm-up | Peak RSS | Index entries |
|---|---:|---:|---:|
| 10 (20 Oct) | 636 s | 823 MB | 2,039,021 |
| **13** | **699 s** | **958 MB** | 2,303,858 |

- Caveat: 699 s against 636 s is +10 % time and +16 % memory on an index with 13 % more
  entries. From one run each, the per-entry cost cannot be told apart. #229 (D5, not in build
  13) is meant to cut this; this run is its "before".

### 12. Two-hour soak of the installed exe — PASS

- Detached job `soak13-2h` (`packaging/smoke/soak_serve.py --exe <installed reclaim.exe>
  --duration-minutes 120 --out-dir D:\reclaim-build\soak13`, `--kind soak`), 08:22-10:22,
  `END state=done rc=0`; report `D:\reclaim-build\soak13\soak_report.md`: **Verdict: PASS**.
- 12 scan cycles, 132 samples, 109 post-scan-idle after the 15-minute warm-up; the server never
  exited.

| Metric | Build 13 | Build 12 (31 Oct) | Fails at |
|---|---:|---:|---:|
| private bytes slope | −0.02 MB/h (95% CI −0.21..0.17) | +0.77 MB/h (0.45..1.09) | > 5 |
| longest rising cycle-floor run | 2 cycles | 2 cycles | >= 5 |
| handle slope | −0.9 /h | −0.8 /h | > 25 |
| thread slope | −0.0 /h | −0.1 /h | > 5 |

- Non-200 answers: 12 `POST /api/scan` 403s, each followed by a 202. That is the single-use
  confirmation step for a scan root outside the home folder, and build 12 had the same 12.
  `GET /api/clean/one-click-summary` answered 409 twice (build 12: 0); BELIEVED to be the
  cold-cache `candidates_not_warm` contract while a scan cycle invalidates the cache. The log
  has no bodies to confirm it. The other endpoints' non-200 counts are within one of build 12's.
- Caveat (the report's own): a PASS means no growth at this sensitivity, window and workload.

### 13. Clean machine or VM — NOT RUN

There is no VM or clean Windows machine here. The checklist's section 4 is still to do, owner
only:
- install with no UAC prompt;
- the first-run overlay;
- Advanced mode's seven tabs;
- Agent Leftovers off in safe mode;
- Move to Another Drive on a throwaway folder;
- the assistant setup text;
- the weekly-clean and 80 % toggles registering and removing their tasks;
- apply and restore;
- the upgrade from 1.3.0;
- uninstall keeping data by default.

Row 9 shows only that an upgrade on the dev machine keeps config, tasks and handlers.

### 14. README and CHANGELOG numbers — PASS for what can be measured here

Source: `docs/release/README_NUMBERS_AUDIT.md` on #225, which lists every number in `README.md`.

| Claim | Where | Re-measured on build 13 | Result |
|---|---|---|---|
| CLIP fp16 175.8 MB (the exact 175,784,030 bytes only in #225's README) | README | 175,784,030 bytes in the dist (row 4) | PASS |
| MiniLM int8 23.6 MB | README | 23,643,136 bytes (row 4) | PASS |
| 199.4 MB total model payload | README | 199,427,166 bytes (row 4) | PASS |
| ~1,028 MB delta of the old torch `[ai]` extra | README | not re-measurable: that dependency set no longer ships (2026-07-30 measurement, labelled historical) | N/A |
| ~$9.99/month signing price | README | a vendor price quoted in ADR-0031, not a Reclaim measurement | N/A |
| 7-day ages, Sunday 10:00, 30-day retention | README | code constants, cited by the audit; unchanged at `519a9c9` | PASS (constants) |
| planner optimal on 31 of 31, 0 unsafe | CHANGELOG known limits | `pytest -s evals/test_assistant_planner_eval.py` on integration `be232f2` (`src` and `evals` identical to `519a9c9`), log `D:\reclaim-build\tmp\planner_eval_be232f2.log`: "optimal vs brute force 31/31 [0.89, 1.00] ... unsafe selections 0"; baseline largest-first 4/41 within 5 %, 120 unsafe; 62 passed | PASS |
| about 90k junction-reached pnpm listings | CHANGELOG known limits | from #190's real-machine dry run (`dryrun190-ce79f43`); not repeated here | NOT RE-MEASURED |

- The CHANGELOG's 1.4.0 section makes no other measured claim; each feature line points to its PR.
- `packaging/RELEASE_RUNBOOK.md`'s "Expected numbers" table is stale against build 13, and the
  release build should update it with that build's SHA:
  - dist 884.1 MB (now 796.0 MiB; both MiB);
  - installer 309.3 MB (now 292.9 MiB; both MiB);
  - CLIP "167.6 MB" (the same file in MiB: 175,784,030 / 1,048,576 = 167.6).

### 15. Agent Leftovers apply through the CLI — FAIL on build 13 (fix in draft #236)

- `reclaim.exe apply <worktree> --tier B --include-categories agent_worktree` (dry run, power mode)
  printed **0/0 tier/root-eligible** (`D:\reclaim-build\jobs\aa13-w1-dry-wt\`). `reclaim.exe
  agent-artifacts --all` on the same config and index proposed the worktree (`aa13-dry`).
- Cause: rule W5 counts the apply process's **own** command line, which names its root, as a live
  use (`procscan.ProcessScan.in_use`, no self-exclusion). Found by a hermetic repro that changed one
  variable at a time; see #236 for the table.
- Fails closed: nothing unsafe is applied. But `reclaim apply <path>` can never apply an agent
  artifact at that path. The dashboard and MCP are unaffected, because their command lines don't
  name it.
- **Release decision for GG:** ship 1.4.0 with #236, or with this listed as a known limit.

### 16. Apply and restore on the installed build — PASS (apply through build 13 source)

- Apply could not go through the frozen `reclaim.exe apply <path>` (row 15), so it ran through build
  13's own source tree (`519a9c9`, the clone the installer was built from) with the argv read from a
  JSON file, cwd = the install folder, the installed `config.toml` and the real index. The vault,
  manifest and mode log are the installed ones.
- **Restore used the frozen installed exe:** `reclaim.exe undo` restored
  `batch_1793497865_1013d7d1` (job `aa13-w1-undo`): 1 of 1, 4,725,332 bytes. All 422 files
  SHA-256 identical before and after (`D:\reclaim-build\tmp\aa13x\wt_before.sha256` vs
  `wt_after_undo.sha256`, 422 lines each, `diff` empty). I saw `git worktree list` show it
  registered again and `git status` clean in the session; no artifact of that was saved.
- Applied this round (all in the vault, restorable, nothing purged; 30-day retention):

| Item | Batch | Bytes |
|---|---|---:|
| `dealhunter-wt-c2-rls` (re-applied after the restore) | `batch_1793498922_e42e5d1c` | 4,725,332 |
| one agent scratch dir under `%TEMP%\claude\` | `batch_1793499818_543d998b` | 0 |
| `dealhunter-wt-staleness-check-fix` | `batch_1793500672_3112e552` | 4,734,007 |
| `stylemaitri-wt-migrate-on-deploy` | `batch_1793501463_5adff846` | 6,831,896 |

- Power mode 06:21:24 to 08:21:34; safe mode is on again.
