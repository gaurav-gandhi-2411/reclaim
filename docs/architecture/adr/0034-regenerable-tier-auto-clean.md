# 0034. Regenerable tier: a closed allow-list that frees real space in safe mode

## Context

On 2026-10-01 the owner's default-mode "Clean My Computer" yielded **0 auto-cleanable
candidates** — the original "a fresh install cleans nothing" P0 in a new form. The cause is the
interaction of two correct decisions in ADR-0023 (safe mode):

1. every scan-driven candidate is forced to Tier B (review-only, guarantee 3), and
2. every apply in safe mode is Recycle-Bin-only (guarantee 1).

A Recycle Bin move frees **no disk space** until the bin is emptied (the #48 house rule), so even
a fully-reviewed safe-mode apply of the categories that matter most — package caches, aged TEMP,
crash dumps, browser caches — reclaims nothing. Measured on the owner's drive on 2026-10-01:
`C:` was 96% used, with `uv`'s cache alone at 26.7 GB and npm's at 2.2 GB; the product could
neither show those as "safe" nor free them.

ADR-0023's guarantees exist because the generic pipeline can reach **any** file under a scan
root, driven by a path list that crosses an API boundary. That risk does not exist for a
closed-world operation whose targets are fixed in code. Weakening the generic pipeline (letting
safe mode permanent-delete some candidates) would have been the wrong repair: it would have
turned a structural boundary into a conditional.

## Decision

Add a **separate** code path, `reclaim.regenerable`, that does not call `apply_batch`, does not
read the scan index, and does not accept a path from any caller:

- **What it may touch (the allow-list, in code, not config):**
  - *Package-manager caches via the owning tool's own command*: `uv cache prune` (unreferenced
    entries only — never `uv cache clean`), `pip cache purge`, `npm cache clean --force`,
    `conda clean --tarballs --index-cache -y` (never `--packages`/`--all`, which can remove
    package directories hardlinked into live environments), `yarn cache clean`.
  - *Direct children of `%TEMP%` / `C:\Windows\Temp`* whose **newest** content is older than the
    7-day floor (the rule from #110: a directory's own mtime is meaningless).
  - *Crash dumps / WER report queues* under the fixed CrashDumps and WER roots, same age rule.
  - *Disk caches (`Cache`, `Code Cache`, `GPUCache`, Firefox `cache2`) of Chrome, Edge, Brave and
    Firefox*, only while that browser's process is not running. Never cookies, history, local
    storage, or extensions.
- **What it never does:** empty the Recycle Bin; purge or touch the vault; follow a junction or
  symlink (the link entry is removed, the target never); delete a tree containing `.git`,
  `.venv`/`venv`/`pyvenv.cfg`, `site-packages` or `node_modules` (reported for review instead);
  run a tool with `--force` past its lock; run anything outside the table in the module.
- **Per-delete re-checks at the moment of deletion:** a share-mode-0 `CreateFileW` probe
  (any other open handle ⇒ skip), reparse-point check, top-level realpath containment. Failure of
  any probe — including failure to enumerate running processes — is a **skip, never a pass**
  (rule 98a, fail closed).
- **Audit:** every item, plus every skipped/failed path (capped), is appended to
  `data/regenerable_audit.jsonl` with a run id; the summary line records logical bytes removed and
  the measured disk-free delta.
- **Reporting honesty:** `bytes_removed` is *logical* bytes unlinked or the cache directory's
  shrinkage; for hardlinked trees (uv's cache) it can exceed what the volume gained, so the
  measured `disk_free_delta_bytes` is reported beside it, never instead of it.
- **Surfaces:** `POST /api/clean/regenerable` (`apply=false` previews; body has no path field and
  `extra="forbid"`), the one-click button in Simple mode, `reclaim auto-clean`, and the weekly
  scheduled task (B5).

### What this does **not** change

ADR-0023 is untouched and still true of the generic pipeline: `apply_batch` in safe mode is
Recycle-Bin-only, candidates are Tier B, blanket applies are refused, and
`evals/test_safe_mode_gate.py` still proves it. This ADR *adds* a second, narrower, separately
tested mechanism; it does not amend any of those guarantees. The generic review flow remains the
only way anything outside the allow-list is ever offered for deletion.

## Consequences

- One click on a fresh install frees real space for the categories with no human-judgement
  component, in safe mode, with no scan.
- A second delete path exists, so it must stay closed: `tests/test_regenerable.py` has teeth tests
  for "active cache skipped" (running browser, open handle, running tool), "aged cache cleaned",
  and "non-allow-listed path untouched" (documents, downloads, HF cache, programs, a similarly
  named `CrashDumpsNot`), plus junction-not-followed and fail-closed process enumeration. Adding
  an entry to the allow-list is a code change with a test, never a config edit.
- It runs unelevated (`reclaim.elevation.assert_not_elevated` is respected); roots that need
  administrator rights (`C:\Windows\Temp`, `ProgramData` WER) are reported `skipped_no_access`,
  not failed.
- **What did not work / known limits:** (1) *Superseded behaviour, kept for the record:* the first version skipped `uv cache prune` whenever any `uv` process was running, and on a workstation running several agent sessions that is most of the time, so the biggest cache (26.7 GB) was almost never cleaned. uv's cache lock exists precisely so a prune WAITS for in-flight installs instead of racing them, so the pre-skip was removed: `uv cache prune` now runs with `UV_LOCK_TIMEOUT` = `UV_LOCK_WAIT_SECONDS` (30 min, overridable via `RegenerableEnv.uv_lock_wait_seconds`) and a subprocess timeout 60 s longer, never `--force`. Only an expired wait is a skip: `skipped_in_use`, detail "waited 1800 s for uv's cache lock". uv runs last so every other item is finished and reported before the wait, and the one-click `POST /api/clean/regenerable` (`apply=true`) is a single-flight background job (202 + run id, 409 if one is running) polled through `GET /api/clean/regenerable/status`, so no HTTP request is held for up to 30 minutes; `apply=false` previews stay synchronous and never wait. pip/yarn/conda keep the busy-process skip. Measured 2026-10-01 whether they have a cache lock to wait on instead: **pip 25.1 has none** (source: `pip cache` code has no lock; only per-entry `filelock` inside the HTTP cache reader; scratch-cache test: purge takes 0.5 s, and with one cache file held open it removes the rest and exits 2 with a `PermissionError`, which is now classified `skipped_in_use`, not `failed`); **conda 25.5.1's** lock is a per-repodata-file byte lock (10 attempts, 1 s apart) that `clean --tarballs --index-cache` does not take, so there is nothing to wait on (conda.exe running is still a skip); **yarn is not installed here, NOT MEASURED** (kept as a busy-process skip). A scheduled caller must allow for the wait (its task time limit must exceed 30 min plus the rest of the run). (2) The share-mode-0 probe protects
  against deleting a file another process has open, but not against a process that opens a file
  immediately after the probe; the age floor and the closed-browser gate carry that residual risk.
  (3) Logical-vs-real byte accounting for hardlinked caches is approximate by construction.

## Alternatives considered

- **Let safe mode permanent-delete `package_caches`/`temp`/`crash_dumps` candidates.** Rejected:
  turns ADR-0023's structural boundary into a conditional and keeps a path-list-driven delete in
  the default mode.
- **Add a Tier A back for these categories in safe mode.** Same objection, and still scan-driven
  (a full-profile walk before the first byte is freed).
- **Empty the Recycle Bin after moving.** Rejected outright: it would destroy items the user put
  there for unrelated reasons.
- **Do nothing and keep the review flow.** Rejected: the review flow cannot free space in safe
  mode at all, which is the defect.

## Addendum: uv lock holder and the logon retry

*Added 2026-10-05. Does not change the allow-list, the delete paths, the safety gate or the
exclusions; it changes only WHEN the scheduled task runs and WHICH allow-listed tools it runs.*

**Problem.** Limit 1 above made `uv cache prune` wait up to 30 min for uv's cache lock instead of
skipping. A real scheduled attempt with `UV_LOCK_TIMEOUT` = 3300 s still never got the lock, and
the weekly trigger (Sundays 10:00) fires exactly when sessions are most likely to be running, so on
a busy workstation uv would be `skipped_in_use` every week.

**Verified lock semantics (2026-10-05, Restart Manager + `LockFileEx` probe, on the owner's
machine).** While a `uv run --no-project python -c "time.sleep(40)"` was alive, a non-blocking
EXCLUSIVE `LockFileEx` on `%LOCALAPPDATA%\uv\cache\.lock` was refused (error 33,
`ERROR_LOCK_VIOLATION`), a SHARED lock was granted, and Restart Manager listed that `uv.exe` pid as
the holder. So uv holds a shared lock for the entire lifetime of any `uv run` / `uv pip` /
`uv sync` process, not just while it touches the cache, and `uv cache prune` (exclusive) waits for
every such process to exit. At an idle moment the same lock was free.

**Not verified.** The identity of the process(es) that held the lock during the 3,300 s failure
cannot be recovered after the fact. BELIEVED (not measured): long-lived `uv run` processes of other
agent sessions. That is an inference from the semantics above, not an observation.

**Decision.** The task gets a second trigger, `LogonTrigger` for the current user with
`<Delay>PT3M</Delay>`, next to the weekly `CalendarTrigger` (`StartWhenAvailable` kept; the 45 min
`ExecutionTimeLimit` is NOT raised). Shortly after sign-in no session has started `uv` yet. Both
triggers run the identical command, so a sign-in run must be cheap and harmless:
`data/autoclean_state.json` (`reclaim.autoclean_state`, schema-versioned per ADR-0027, written
atomically via temp file + replace) holds `last_full_run_utc` and `pending_tools` (native tools
whose item ended `skipped_in_use`). For `auto-clean --apply --scheduled`:

| State | Run |
|---|---|
| missing / unreadable / corrupt / no `last_full_run_utc` / older than 6 days / in the future | the full regenerable tier, as before |
| recent and `pending_tools` non-empty | ONLY those tools (retry) |
| recent and nothing pending | nothing, exit 0, no toast |

After a run, a tool that ended `skipped_in_use` is added to `pending_tools`; a tool that ran and
ended any other way is removed (a retry run leaves `last_full_run_utc` alone; a full run sets it).
Corrupt state fails toward the full run, which only touches provably-regenerable caches. The retry
filter (`only_keys`) can only narrow the plan; unknown keys in the state file are dropped on read.
Dry runs and manual (non-`--scheduled`) runs never read or write the state.

**Consequences / limits.**
- A retry still waits for the same lock; it merely runs at a moment when it is likely free. If the
  user signs in and immediately starts `uv run`, it can still expire and stay pending until the next
  sign-in or the next full run.
- **Cadence drift.** A full run at sign-in (state older than 6 days) moves the weekly cadence to
  whenever the user signs in: a full run on, say, a Tuesday makes the following Sunday run a
  no-op (or a retry), so full runs are roughly 6-7 days apart, not strictly weekly.
- **`failed` tools are not retried.** A full run stamps `last_full_run_utc` even when some native
  tools ended `failed`; only `skipped_in_use` is retried, so a failed tool waits for the next full
  run (the state is still not stamped by a run that violates the `excluded_applied` invariant).
- Already-registered tasks keep their single weekly trigger until re-registered (`register_task`
  uses `/f`, so toggling the Settings switch off and on re-registers with both). Nothing
  re-registers on app start.
- Rejected: `--force` or killing `uv` processes (ADR-0034's rule is to wait, never race). Not
  taken: raising `ExecutionTimeLimit` or the wait further; the 3,300 s attempt shows a longer wait
  does not help while the lock holders are long-lived.

## Addendum: pytest temp (opt-in, off by default)

**What.** A new category `pytest_temp` deletes whole `%TEMP%\pytest-of-<user>\pytest-<N>` basetemp
directories whose NEWEST content (live recursive scan, the same rule #110 uses for aged TEMP; a
directory with one recent file inside is not old whatever its own mtime says) is at least
`PYTEST_TEMP_MIN_AGE_SECONDS` (7 days; `>=`, so exactly 7 d qualifies, same as aged TEMP) old.
Unit of deletion is one `pytest-<N>` directory (`fullmatch` on `pytest-[0-9]+`). `pytest-current`,
anything else in the parent, a link or file named like a basetemp, and the `pytest-of-<user>` parent
itself are never candidates; reparse points are never followed (a link *inside* a basetemp is
unlinked, its target untouched, as for every other category).

**Safety checks, per directory, all-or-nothing.** (1) user exclusions (`subtree_exclusion_match`,
ADR-0039) -> `excluded`, status `skipped_excluded` when nothing else happened; (2) the existing
guard names (`.git`, venv, `node_modules`) -> left for review; (3) on apply, every file is probed
with the same `has_open_handle` the other categories use and ONE open or un-probeable file skips the
whole directory when the handle is present at probe time (`skipped_in_use`, with a reason; a detector that raises is treated as in use,
fail-closed); (4) ADR-0036: the directory is re-scanned after the (slow) handle probe and immediately
before the delete, and kept if anything is newer; each file is again re-stat'ed against the age floor
as it is unlinked. A dry run does NOT probe handles (that would open every file); it reports what the
apply path would attempt.

**The ownership caveat, and the decision.** A pytest basetemp is shared by every project the user
runs pytest for, and the directory records nothing about which project created it. Reclaim therefore
cannot prove that a given `pytest-<N>` is not a run of a project on the user's `[exclusions]` list.
Decision: the category is **off by default**. `[regenerable] pytest_temp = true` (config file only;
no Settings toggle) is the only thing that lets `--apply`, the weekly task or the dashboard's one-click
clean touch it; with the flag off the item is not even planned (not listed, not deleted). To preview
without opting in, `reclaim auto-clean --include-pytest-temp` adds the item to a DRY RUN only
(`pytest_temp_mode = "report"`: even if `apply` were forced the runner downgrades to a report, and
the CLI refuses `--apply --include-pytest-temp` without the config flag, exit 2). Each report line
states that ownership is unknown. **Exclusions only help partially:** `project_names` and path
patterns are matched against every path inside the directory, so a run whose file or directory names
embed the project name is skipped, but a run that merely used `tmp_path` fixtures with generic names
cannot be matched; the user can protect those only by writing a `[safety] deny` path pattern for the
folder (or by leaving the flag off).

**Races after the probe.** A handle, touch or working-directory process that appears AFTER the
probe is not caught by (3): the rescan keeps a touched dir (`skipped_in_use`, "touched since it
was planned"), and if the delete phase still leaves the top-level dir behind (an in-use file, or
a process whose cwd is inside it, which leaves an empty skeleton) the item is `skipped_in_use`
with a detail naming the dir and why. `cleaned` is never reported while any qualifying dir
remains (even if other dirs were removed; bytes/files removed are still counted).

**Name matching limits.** Exclusion name tokens match literally: a hyphen does not match an
underscore (`my-proj` does not match `test_my_proj0`), so pytest's own sanitised test-dir names
usually escape `project_names`. As for aged TEMP, a `%TEMP%` that has been re-pointed
(junction) is followed.

**A change to aged TEMP that this required.** Before this addendum the generic aged-TEMP category
could delete `%TEMP%\pytest-of-<user>` wholesale whenever every file in it was over 7 days old, which
contradicts "off by default". Aged TEMP now always skips direct children named `pytest-of-*` (noted
in its `detail`), whether or not the new category is on.

**Consequences / limits.** A basetemp holding a virtualenv is left for review (the common
"venv built inside tmp_path" test pattern), so it never frees that space. How much it frees depends entirely on
the machine (see the PR that added this for a metadata-only dry run on one).

## Addendum: Upgrade path

*Added 2026-10-05. Closes the last Consequences bullet of the addendum above (an existing install kept a single-trigger task until the user toggled Settings off/on).*

**Decision.** `reclaim auto-clean --reconcile-task` (mutually exclusive with `--apply`/`--dry-run`; cleans nothing) reads `[autoclean] enabled` from the user's `config.toml`. Enabled: `register_task()` (schtasks `/f`, so an old single-trigger task is replaced by the current weekly + logon definition). Disabled or no config: no schtasks call at all and no task is created. Source/dev run: message, exit 0. Real failure (schtasks error, invalid config, elevated): actionable message on stderr, exit 1, details in `task_registration_diagnostic.log`. Safe to repeat. `packaging/reclaim.iss` runs it from `[Run]` on every install and upgrade, in `{app}`, with `runhidden nowait skipifdoesntexist runasoriginaluser`. Inno does not fail an install on a `[Run]` exit code (BELIEVED from Inno's documented behaviour, not exercised here), so a failure never blocks the install; the Settings toggle remains the manual path.

**Account.** Setup is `PrivilegesRequired=lowest`, so the disk-space task step (`RegisterDiskSpaceTask`, `[Code]`) already runs as the installing user. `runasoriginaluser` keeps the new step on that same user even if Setup were launched elevated, because the task is a per-user `InteractiveToken` task and `auto-clean` refuses to run elevated.

**Not verified.** A real installer upgrade over a real old task has not been run; the tests use an injected fake schtasks and a static parse of the `.iss`.
