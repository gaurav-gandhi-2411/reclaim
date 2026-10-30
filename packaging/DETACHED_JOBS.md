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
| start gate | free RAM >= 8 GB (`--start-ram-gb`; refused below 8, the build floor) and every disk floor met, 3 samples in a row, `--gate-seconds` 20 apart | waits; gives up after `--wait-max-min` (720) with state `gave_up` |
| disk floors | `--disk C:=20 --disk D:=50` (repeatable) | crossing one while running aborts; an unreadable drive counts as crossed |
| RAM while running | free RAM < `--abort-ram-gb` (2) for 2 samples in a row, `--sample-seconds` 15 apart | aborts |
| time limit | `--limit-hours` (12) | Task Scheduler ends the task |

**Why the running floor is 2 GB, not 8.** The 8 GB floor is the bar for *starting* heavy work,
and it is unchanged. A build or verify then uses several GB itself, so a running floor of 8
would abort nearly every job on its own memory use. The running floor exists to stop the
machine from running out (the reaper fired at 3.6-4.3 GB free last run, with other sessions'
jobs holding the rest). The value is a flag; raise it for a job that must yield sooner.

**Abort.** The command runs in a Windows Job Object with kill-on-close, so the whole tree ends
together (`taskkill /T` is the fallback). Then, for each `--cleanup` dir, only the top-level
entries that did not exist when the job started are deleted:
- a cleanup dir must be strictly inside `D:\reclaim-build` (checked at launch and again at abort,
  after resolving links);
- an entry that is, or holds, a junction or symlink, or cannot be read, is **not** deleted; it is
  logged as `REFUSED` and left for a person;
- every listing and deletion is written to `runner.log`.

If the runner itself dies, the Job Object ends the command with it, so a job never runs on
unwatched.

## Use

From the repo root, with the venv's Python (the runner itself uses only the standard library):

```powershell
& .venv\Scripts\python.exe packaging\detached_job.py launch --name verify-bff0752 `
    --cwd D:\reclaim-build\tmp\verifyexp_bff0752 -- `
    C:\Users\gaura\ml-projects\reclaim\.venv\Scripts\python.exe scripts\verify.py
& .venv\Scripts\python.exe packaging\detached_job.py status verify-bff0752
& .venv\Scripts\python.exe packaging\detached_job.py forget verify-bff0752   # after it ends
```

`status` exits 0 for `done`, 1 for `failed`/`aborted`/`gave_up`/`error`, 2 while waiting or
running; its `heartbeat_age_s` shows whether the runner is still sampling. Everything is under
`D:\reclaim-build\jobs\<name>\`:

| file | content |
|---|---|
| `spec.json` | the command and every threshold |
| `status.json` | state, child PID, last free RAM, heartbeat, end reason |
| `runner.log` | gate samples, running samples, abort and cleanup lines, and a final `END state=... rc=...` line |
| `output.log` | the command's stdout and stderr |

A session polls `status` or the log files; it never waits on the job from a background shell
(the waiter could be reaped, though the job would not). A `--name` can be used once.

## Proof (2026-10-30, on GG's machine)

The run log (`docs/run-log-2026-10.md`, 30 Oct) has the commands and output. On `4f9bed6`
unless noted, with a dummy job (`D:\reclaim-build\tmp\jobtest\dummy_job.py`) that prints a numbered
line every 2 s and, for the abort runs, writes partial output and starts a grandchild:

| proof | what was done | result |
|---|---|---|
| ancestry | `Win32_Process` parent chain of the runner | `pythonw.exe` -> `svchost.exe` (Task Scheduler) -> `services.exe`; no Claude Code process |
| 1b (pre-`4f9bed6`, same launch path) | launching shell stopped while the job waited on the RAM gate (free RAM 5.4-6.3 GB) | waited 10.5 min, then ran; 60/60 lines, `DUMMY DONE`, `END state=done rc=0` |
| 2 | launching shell stopped 5 s after the child started | ran on; 90/90 lines, `DUMMY DONE`, `END state=done rc=0`; `status` exit 0 |
| 3 | disk floor 1 GB under D:'s free space, the job writes 2 GB into a cleanup dir holding an older file | abort at 1,088 MB written; child, job and grandchild gone; only the job's `partial_*` dir removed, the older file kept; D: back to its starting free space |
| 4b | as 3, with a junction to a canary folder inside the partial dir | the partial dir `REFUSED` and kept; the canary behind the junction intact; the tree ended |

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
