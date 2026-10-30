# Detached heavy jobs (`detached_job.py`)

Use this for every heavy job a Claude Code session starts on this machine: an installer build, a
full `scripts/verify.py`, a soak, a real-machine dry run, a profile on a scratch index copy.

## Why

Claude Code stops the process tree of a session's background shells when the machine runs short
of memory (the "low-memory reaper"). In the 2026-10 autonomous run that stopped every build and
every full verify, including jobs that were only waiting for RAM: the #190 dry run, two full
verifies and build 12. A reaped job may not be restarted by the session on its own.

`detached_job.py launch` registers the job as a one-time **per-user scheduled task** and runs it at
once. The Task Scheduler service starts it, so it has no ancestor in any Claude Code shell, and a
reaper event, a stopped background shell or the session ending cannot reach it. No admin rights
are needed (LeastPrivilege, the user's interactive token). It is a scheduled task you can see in
Task Scheduler as `ReclaimJob-<name>`; it deletes itself a day after its end boundary, or at once
with `forget`.

## The watchdog (inside the job, not in the session)

| check | default | what happens |
|---|---|---|
| start gate | free RAM >= 8 GB (`--start-ram-gb`; refused below 8, the build floor) and every disk floor met, 3 samples in a row, `--gate-seconds` 20 apart | waits; gives up after `--wait-max-min` (600, must be under the time limit) with state `gave_up` |
| disk floors | `--disk C:=20 --disk D:=50` (repeatable) | crossing one while running aborts; an unreadable drive counts as crossed |
| RAM while running | free RAM < `--abort-ram-gb` (2; at least 0.5) for 2 samples in a row, `--sample-seconds` 15 apart | aborts |
| time limit | `--limit-hours` (12) | Task Scheduler ends the task: the runner dies and the Job Object ends the command, but there is no cleanup and no `END` line, so `status` shows it as stale |

Both periods are capped at 60 s: a longer one would leave the job unwatched between samples. The
launcher and the runner run the same validation, so a hand-edited `spec.json` cannot lower a
floor; a malformed one ends the job with `state=error` and an `END` line.

**Why the running floor is 2 GB, not 8.** The 8 GB floor is the bar for *starting* heavy work,
and it is unchanged. A build or verify then uses several GB itself, so a running floor of 8
would abort nearly every job on its own memory use. The running floor exists to stop the
machine from running out (the reaper fired at 3.6-4.3 GB free last run, with other sessions'
jobs holding the rest). The value is a flag; raise it for a job that must yield sooner.

**Abort.** The command runs in a Windows Job Object with kill-on-close, so the whole tree ends
together (`taskkill /T` is the fallback). Only once the child is confirmed gone, the entries
in each `--cleanup` dir are deleted:
- a cleanup dir must **not exist** when the job starts (checked at launch and again by the
  runner): the job creates it, so nothing else writes there and everything in it is the job's.
  Point a build's output at a fresh dir if you want its partial output removed on abort;
- it must be absolute, strictly inside `D:\reclaim-build`, not the jobs dir or a parent of it,
  and not a junction, checked at launch and again at abort (after resolving links);
- an entry that is, or holds, a junction or symlink, or cannot be read, is **not** deleted; it is
  logged as `REFUSED` and left for a person;
- its resolved path must be its written path, so a junction the job made on a parent folder
  at runtime cannot point the delete at another folder's files;
- if the kill is not confirmed, the cleanup is skipped and logged;
- every listing and deletion is written to `runner.log`.

The job never starts without its Job Object. If the runner dies (the time limit, logoff, a
reboot), the Job Object ends the command with it; there is then no cleanup and no `END` line,
and `status` reports the job as stale. A task re-run on the same job dir is refused.

**Limits** (data-safety pass 3; the job runs as you, so these guard against mistakes, not a
hostile command):
- give each job its **own** cleanup dir, never one shared with or nested in another running
  job's: launch refuses a dir that exists, but two jobs launched before either creates its dir
  are not checked against each other, and an abort would delete the other job's output there;
- the kill is confirmed on the command itself; the Job Object ends its descendants, but a
  process the command starts *outside* the Job Object (through Task Scheduler, a service or
  WMI) is not tracked;
- the runner's own files (`status.json`, the logs) live in the job dir; the status temp file
  is unlinked before each write, but the job could still redirect the logs with links.

## Success check: rc 0 is not success

A job declares what success looks like, and `launch` refuses a job that declares nothing:

- `--expect-file PATH` (absolute, repeatable): the file must exist when the command ends, with
  an mtime no more than 2 s before the command started (a stale installer from an earlier
  build does not count). Its size, mtime and SHA-256 are recorded in `status.json`
  (`artifacts`) and `runner.log`.
- `--expect-line TEXT` (repeatable): some line of `output.log` must contain the text, e.g. the
  `==> DONE.` the installer build prints last, or `OK: all checks passed` from
  `scripts/verify.py`.

rc 0 with every check met ends `state=done`; rc 0 with any check unmet ends **`state=suspect`**
with the unmet checks as the reason (and `status` exits 1); a nonzero rc is `failed` whatever
the checks say. Why: build 12's first try ran `cmd.exe C:/ ...` (see below), and `cmd` with no
`/c` opened a prompt, read end-of-input and exited 0, so the job reported `done` having built
nothing. Only reading `output.log` caught it.

## Launching from Git Bash

Git Bash rewrites POSIX-looking arguments before Python sees them: `/c` becomes `C:/`,
`/tmp/x` becomes `C:/Users/<you>/AppData/Local/Temp/x`, `--out=/d/x` becomes `--out=D:/x`.
Either launch from PowerShell, or prefix the launch with `MSYS_NO_PATHCONV=1` (or
`MSYS2_ARG_CONV_EXCL='*'`); both were checked to keep `/c` intact. As a backstop, `launch` (and
the runner, for a hand-edited spec) refuses any command argument, `--cwd`, `--cleanup` or
`--expect-file` with a drive letter followed by `/`, the shape that conversion produces; write
Windows paths with backslashes. `tests/test_detached_job.py` runs a real Git Bash both ways:
with `MSYS_NO_PATHCONV=1` the stored command equals what was typed, and without it the launch
is refused and nothing is written. A second test checks that the argv a child receives from the
runner equals the launched argv exactly (spaces, quotes, a trailing backslash, `&|<>^`, `%VAR%`,
an empty argument, non-ASCII).

## Use

From the repo root, with the venv's Python (the runner itself uses only the standard library):

```powershell
& .venv\Scripts\python.exe packaging\detached_job.py launch --name verify-bff0752 `
    --cwd D:\reclaim-build\tmp\verifyexp_bff0752 --expect-line "OK: all checks passed" -- `
    C:\Users\gaura\ml-projects\reclaim\.venv\Scripts\python.exe scripts\verify.py
& .venv\Scripts\python.exe packaging\detached_job.py status verify-bff0752
& .venv\Scripts\python.exe packaging\detached_job.py forget verify-bff0752   # after it ends
```

`status` exits 0 for `done`, 1 for `failed`/`aborted`/`gave_up`/`error`/`suspect`, 2 while
waiting or running, and 3 for `stale` (the runner stopped sampling: no heartbeat for 3 periods
plus a minute, or no status at all that long after launch). Everything is under
`D:\reclaim-build\jobs\<name>\`:

| file | content |
|---|---|
| `spec.json` | the command, every threshold and the success checks |
| `status.json` | state, child PID, last free RAM, heartbeat, end reason, expected artifacts with their SHA-256 |
| `runner.log` | gate samples, running samples, abort and cleanup lines, and a final `END state=... rc=...` line |
| `output.log` | the command's stdout and stderr |

A session polls `status` or the log files; it never waits on the job from a background shell
(the waiter could be reaped, though the job would not). A `--name` can be used once.

## Proof (2026-10-30, on GG's machine)

The run log (`docs/run-log-2026-10.md`, 30 Oct) has the commands and output. On `4f9bed6`
unless noted (the abort proofs were rerun on `b3e13a0`, where a cleanup dir must be new), with a dummy job (`D:\reclaim-build\tmp\jobtest\dummy_job.py`) that prints a numbered
line every 2 s and, for the abort runs, writes partial output and starts a grandchild:

| proof | what was done | result |
|---|---|---|
| ancestry | `Win32_Process` parent chain of the runner | `pythonw.exe` -> `svchost.exe` (Task Scheduler) -> `services.exe`; no Claude Code process |
| 1b (pre-`4f9bed6`, same launch path) | launching shell stopped while the job waited on the RAM gate (free RAM 5.4-6.3 GB) | waited 10.5 min, then ran; 60/60 lines, `DUMMY DONE`, `END state=done rc=0` |
| 2 | launching shell stopped 5 s after the child started | ran on; 90/90 lines, `DUMMY DONE`, `END state=done rc=0`; `status` exit 0 |
| 3 (`b3e13a0`) | disk floor 1 GB under D:'s free space; the job creates its cleanup dir and writes 2 GB into it, and starts a grandchild | abort at 1,088 MB written; child, runner and grandchild gone; the job's `partial_*` dir removed, the cleanup dir left empty; D: back to its starting free space (1,345.57 GB) |
| 4b (`b3e13a0`) | as 3, with a junction to a canary folder inside the partial dir | the partial dir `REFUSED` and kept; the canary behind the junction intact; the tree ended |
| 5 (`b3e13a0`) | `launch` with a cleanup dir that already exists (holding an older file) | refused before any task was registered: `cleanup dir ... already exists`, exit 2; the older file untouched |

What is not proven on the machine: the RAM abort. Free RAM cannot be driven down on purpose
without harming other sessions, so that path is covered by `tests/test_detached_job.py` (a real
child process, faked RAM readings, the real kill and cleanup). Survival of a *real* reaper event
is BELIEVED, not observed: the reaper stops the process trees of a session's background shells,
the same as stopping the launching shell in proofs 1b and 2, and the ancestry shows no such
shell above the job. (A real reaper event in this session stopped a shell that was only
waiting on a job's log file; it could not have touched the job.)

The first proof run failed and is part of the record: the task then started the venv's
`pythonw.exe`, which launches a *console* `python.exe`, and the runner ended with `0xC000013A`
(a console control event) 10 s after its launching shell was stopped. The task now starts the
base interpreter's `pythonw.exe` (no console), and the command gets a hidden console of its own
in a new process group.
