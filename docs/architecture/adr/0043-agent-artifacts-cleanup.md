# ADR-0043: reversible cleanup of finished AI-agent worktrees, throwaway clones and idle scratch

Status: accepted (owner decisions 2026-10-20). Implements `docs/specs/agent-artifacts.md` (section 10 of
the spec lists every place where the build differs from the 2026-10-15 draft).

## Context

On the owner's machine the single largest consumer of C: is what coding agents leave behind: one git
worktree per task (each with its own `node_modules`/`.venv`), standalone `*-verify*` clones, and one
`%TEMP%\claude\<project>\<session>` directory per Claude Code session (about 180 GB measured on
2026-10-16). Deleting any of it by hand means reading `git status` in dozens of directories, and the
existing detectors cannot help: the scanner does not recognise a linked worktree as a repository
(its `.git` is a file), and `SafetyValidator` rightly denies anything inside a git repository.

The risk is asymmetric. Removing a worktree with unpushed commits, a hidden `skip-worktree` edit, a
junction to someone else's data or a directory a live process is using destroys work that cannot be
rebuilt. `git worktree remove` is not a safe primitive (the spec reproduced three data-loss cases).

## Decision

Three categories under one config group, `[categories.agent_artifacts]`: `agent_worktree`,
`agent_clone`, `agent_session_scratch`. Always Tier B (a human clicks; never automatic), OFF by
default, forced off in SAFE mode (removal is a vault move, which SAFE mode never performs).

1. **Prove finished, never guess.** An item is offered only when a conjunction of read-only checks all
   hold (`src/reclaim/agent_artifacts.py`, rule ids W/C/S in the reasons shown to the user). Any check
   that cannot complete (git fails or returns an unexpected code, a process cannot be inspected, a
   walk errors) refuses. Every git command is `git --no-optional-locks -c core.fsmonitor=false ...`.
2. **"Pushed" means on a real remote.** Every commit of HEAD (worktrees) or of all branches, tags and
   HEAD (clones, nested repositories) must be contained in a remote-tracking ref of a remote whose
   fetch URL is `https://`, `ssh://`, `git://` or scp-style; a drive-letter, UNC, `file://` or relative
   remote is not a backup. No network call is made, so the answer is as fresh as the last fetch (the
   review row says when that was).
3. **Live use is a positive finding.** `procscan.py` reads every same-user process's working
   directory and command line with `NtQueryInformationProcess`/`ReadProcessMemory` (no `psutil`).
   A same-user process that cannot be read blocks everything, except unreadable Windows/vendor
   service binaries under `%SystemRoot%`, Program Files or ProgramData whose name is not a shell,
   interpreter or other work host (a documented, tested allow-list; on the owner's machine six OEM
   helpers fall in that class). The vault move itself is a second, independent guard (below).
4. **Reversible removal, rename only.** An applied item is MOVED into the vault by a same-volume
   `os.rename` with retention (default 14 days) and a manifest entry, like every other vaulted
   category. For these categories the copy-verify-delete fallback of `_atomic_move` is disabled: a
   refused rename means something still holds the directory, and copying then deleting a live tree
   would be the failure mode this ADR exists to prevent. A candidate on another volume than the vault
   is not offered.
5. **Worktree bookkeeping survives the move.** After the move the worktree's git admin directory is
   copied into the vaulted directory (`.reclaim-gitadmin/`) and `git worktree prune` is run; restore
   puts the admin directory back if prune removed it, deletes the helper folder and runs
   `git worktree repair`, then verifies git lists the worktree. A failure of this bookkeeping is
   logged and never fails or undoes the vault move.
6. **Nothing is trusted from scan time.** `executor._preflight_skip_reason` re-runs the whole proof
   from live state immediately before each move (`agent_artifacts.reverify`); an item that cannot be
   re-proven is skipped with `agent_artifact_not_provably_finished`, and one asked to go anywhere but
   the vault with `agent_artifact_requires_vault`. The batch-level exclusion check (ADR-0039) still
   refuses the whole batch on any exclusion match.
7. **Exclusions win** (`exclusion_match` on the item and, for a worktree, on its repository), and the
   validator's own protected-root/UNC/deny rules are applied to a record with its git fields cleared.
8. **Sizes and ages come from the scan index** (opaque aggregate rows included); only the proof reads
   live state. `reclaim agent-artifacts` is a read-only dry run that prints what would be proposed.

## Consequences

- **Hard gate:** `evals/test_agent_artifacts_gate.py` (registered in `scripts/verify.py`): 35 fixture
  cases with real git repositories, 0 wrong offers, 100% recall on the 6 finished ones, and the first
  failed rule asserted per refusal. 26 mutations of the rules were applied and killed (see the PR).
- Recall is deliberately incomplete: a squash-merged branch whose remote branch was deleted is not
  "pushed", a worktree with any non-regenerable ignored file (`.env`, `*.db`, `build/`, `dist/`) or
  any junction is kept, and one unreadable same-user tool process blocks every item. All are the safe
  direction and are reported by `--all`.
- Space is not freed until the vault entry is purged (a rename moves the bytes, it does not release
  them). That is the price of reversibility.
- `procscan` is a new ctypes surface (64-bit Windows only; elsewhere it reports "unavailable" and
  everything is refused).
- Residual risk, stated: a process that opens files without being visible in cwd/command line and
  without blocking a rename (rare on Windows, where an open file blocks the parent rename unless it
  was opened with delete sharing) would not be noticed; git state, not process state, is what protects
  the work itself.

## Alternatives

- **Delete in place (the spec's first draft for worktrees):** frees space at once but is irreversible;
  rejected by the owner in favour of the vault.
- **`git worktree remove`:** rejected, it deletes junction targets and hidden edits (spec section 3.6).
- **HEAD must be an ancestor of the remote default branch (spec W4):** safer-sounding but refuses every
  worktree on a pushed feature branch; "contained in any real remote ref" protects the same commits.
- **Add `psutil`:** a new dependency for one function; ctypes is enough and keeps the frozen build
  small. A handle-enumeration pass (every open handle under the directory) was rejected as fragile;
  the rename is the handle test.
