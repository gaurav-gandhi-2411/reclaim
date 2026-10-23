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
   copied into the vaulted directory (`.reclaim-gitadmin/`) and EXACTLY that one admin entry is
   removed so the repository stops listing a missing directory (the effect of `git worktree prune`
   limited to this worktree; see Addendum 1 for why plain `prune` is not used). Restore puts the
   admin directory back, deletes the helper folder and runs `git worktree repair`, then verifies git
   lists the worktree. A failure of this bookkeeping is logged and never fails or undoes the vault
   move.
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

## Addendum 1 (2026-10-20): changes forced by an independent hostile review

A first review of the build (a verifier whose only job was to get something unsafe offered or moved)
succeeded in 9 scenarios, 4 of them through to an actual move. Every one is fixed and has a regression
test that reproduces the reported scenario. What changed:

- **A commit counts as backed up only if BOTH the fetch and the push URL of the remote are real**
  (`git remote get-url --all` and `--push --all`), not loopback (`ssh://localhost/...`) and not a
  drive path (`ssh://host/D:/backup`). A `pushurl`/`pushInsteadOf` to a local path still updates
  `refs/remotes/<name>/*` and is no backup. (My own first test fixtures relied on exactly that; they now
  record a push the way git does, by setting the remote-tracking ref behind an `https` URL.)
- **Clones and nested repositories are compared with `git rev-list --all`**, i.e. every ref namespace
  (custom refs, `refs/original`, `refs/jj`, notes, stash), not just branches and tags. Commits that
  exist only in a reflog are still not counted (accepted residual risk).
- **Repositories INSIDE an artifact are proven too**, whatever the artifact is: a repository under
  `node_modules`/`.venv` (pip editable VCS installs create `.venv/src/<pkg>`) must be clean and fully
  pushed (rules W9 for worktrees, C8 for clones, S7 for scratch), and a bare repository inside (it can
  hold the only copy of a commit and has no working tree to inspect) is refused outright.
- **"Regenerable ignored content" is decided by ignored-ness AND tracking**: an ignored file inside a
  directory that merely bears a regenerable name (`venv/`, `node_modules/`) is regenerable only if
  nothing under that directory is tracked. A tracked `venv/tool.py` next to an ignored secret is a
  project, not a cache. A lock file in the git dir (`index.lock`) means a git process is active.
- **`git worktree prune` is NOT used.** It removes the admin entry of EVERY prunable worktree, not just
  the one just vaulted, so a worktree whose directory is temporarily elsewhere (an offline drive, a
  rename) lost its staged index and an unpushed detached commit. The build removes exactly the one
  entry it just backed up, after checking the entry's `gitdir` file names this worktree. This departs
  from the owner's instruction to run `git worktree prune`; the reason is data loss in a case the
  owner never asked Reclaim to touch. Restore refuses to write into a repository that has moved or
  has been replaced by a different one (matched by root commits), and relative `.git` pointers are
  resolved against the worktree, not the process's cwd.
- **Process matching resolves paths**: relative arguments against the process's own directory, `..`,
  junction/symlink aliases, and a local administrative share (`\\localhost\D$\...`). The vendor-service
  allow-list uses name globs (`python*`, `pycharm*`, `devenv*`, ...) and ignores a drive root.
- **Exclusions are decided on real locations too** (a junction alias of an excluded repository), on the
  Claude project slug (`my_proj` is `my-proj` there), and any exclusion hit, including one found only in
  the content, makes the verdict hidden. The apply-time re-proof and the rename-only move key on the
  group OR the category, so a candidate that lies about one still gets the strict treatment.
- **Accepted, not fixed:** hidden work in an NTFS alternate data stream survives the rename (it is lost
  only when the vault entry is purged); a stale or hand-made remote-tracking ref counts as pushed (no
  network is used, so the answer is as fresh as the last fetch); one unreadable same-user tool process
  outside the vendor classes blocks every item.

## Addendum 2 (2026-10-23): changes forced by the second hostile review (A3)

A second, independent review, run against the rebased build `71803a1`, re-ran all nine pass-A scenarios; every one stayed fixed. It then found six new ways through, two of which lost data. Each is fixed with a regression test that reproduces the reported scenario and fails on `71803a1`.

- **Per-worktree refs must be pushed too (F1, data loss).** W4 proved only HEAD of a worktree. Refs under `refs/worktree/`, `refs/bisect/` and `refs/rewritten/` live in the admin dir, which is removed after the move. Once that dir was gone, their commits were unreachable and `git gc` dropped them, while restore still reported success. W4 now also requires every such ref to be on a real remote.
- **Restore never takes over an admin entry that belongs to another worktree (F2, data loss).** After a move, `git worktree add` may reuse the freed admin name. Restore used to skip the copy, delete its backup and run `worktree repair`. That repointed the newer worktree's index and HEAD, and a later `worktree remove` then lost that worktree's staged work. Now, when the admin dir exists and its `gitdir` names another path, restore writes nothing, keeps the backup and returns a note.
- **Git may not stay silent about paths it cannot open (F3).**
  - Every git call runs with `core.longpaths=true`.
  - Any stderr from the status and ls-files proofs is a refusal: when git cannot open a directory, it warns, exits 0 and lists nothing.
  - A name ending in a dot or a space, which Win32 tools including git cannot open, refuses a worktree or clone, or any nested repository that contains it.
- **`.git` is matched case-insensitively (F4).** Git on Windows uses `.GIT` as the repository, and it skips content under `notes/.GIT/`.
- **Nested `.git` pointers are checked against the exclusions before any git command runs in them (F5).** This covers the admin dir and its common dir, that is, the main repository of a linked worktree. A neutral-looking folder in scratch or in a clone can be a linked worktree of an excluded project, and only its pointer names that project. A pointer that cannot be read or parsed counts as excluded.
- **A remote's tracking refs count only if nothing else can write them (F6).** `--remotes=<name>` reads `refs/remotes/<name>/*`. Another remote can write there in two ways:
  - through its fetch refspec, for example a local `bak` mapped to `refs/remotes/origin/*`, where a push updates that ref;
  - through its name, for example `origin/bak`.

  A remote is dropped from the proof when any other remote's fetch destination overlaps its namespace, or when its name is a case-insensitive prefix match of another remote's name. Loose refs are files, so `ORIGIN` and `origin` share a namespace. A remote whose own name contains `/` is never used.
- **Loopback and drive-path URL spellings are refused.** This covers loopback and unspecified addresses in every IPv4/IPv6 notation (`127.1`, decimal, hex, IPv4-mapped), a trailing-dot host, this computer's own name, and drive paths that are percent-encoded, use the `d|` form or follow doubled slashes. No DNS lookup is made.
- **Accepted, not fixed, all in the safe direction or restorable:**
  - Process matching misses a bare relative argument run from the parent directory, and some UNC loopback spellings. The rename still fails on any open handle, and git state, not process state, protects the work.
  - A clone that another repository borrows objects from (`clone --shared`) can be moved. That breaks the borrower until restore; the objects are on the remote.
  - A junction to a huge tree is walked for exclusions before the reparse check refuses it. The order stays, so an excluded name inside is never reported.

## Addendum 3 (2026-10-23): changes forced by the third hostile review (A3b)

A third independent review, run against `7cc15b1`, re-ran every earlier attack. All were still refused. It then found five new ways through, and its mutation run left seven checks without a test of their own. Every finding is fixed, each with a test that reproduces the reported scenario; those tests fail on `7cc15b1`. Each surviving check now has its own test.

- **Legacy remote files are refused (N1).** Git still honours `$GIT_COMMON_DIR/remotes/<name>` and `branches/<name>`. Such a remote is missing from `git remote` and from `remote.*.fetch`, so the addendum-2 namespace checks never saw a `Pull:` line writing `refs/remotes/origin/*`. Any legacy remote definition is now refused (W3).
- **A bare repository inside a git dir counts (N2).** Only a directory named `.git` is exempt from the bare-repository check, not everything below it. `nested/.git/backup.git` can hold the only copy of a commit.
- **Every git dir is checked against the exclusions before git runs in it (N4).** This covers a nested `.git` directory, a clone's own git dir, and the admin dir a worktree's pointer names. For each one, the check covers:
  - its common dir;
  - every object store it borrows through `objects/info/alternates`, followed as a chain (`clone --shared` of an excluded project);
  - any `core.worktree` it names.

  A file that exists but cannot be read counts as excluded.
- **A late commit keeps the admin entry (N5).** Before the one admin entry is removed after the move, its detached HEAD and per-worktree refs are checked again against the remote-tracking refs. A commit made between the apply-time re-proof and the rename is otherwise reachable only through that entry, and `git gc` would drop it. When one is not on a remote, the entry is kept and no backup is written; the worktree then shows as prunable, and its commit stays in the repository.
- **Restore adopts an existing admin dir only if it is ours (N6).** That means its `gitdir` names our path and its HEAD is the HEAD we saved. Another worktree added at the same path and later deleted leaves an admin dir that names our path but holds its own HEAD and index. When restore refuses, the restored folder's `.git` pointer is renamed to `.git.reclaim-detached`, so git run in that folder cannot read or write another worktree's index and HEAD.
- **Stated precisely: what the vault does not keep for a worktree (N3).**
  - A worktree's objects live in its repository, not in the worktree folder. So commits reachable only from the worktree's own reflog or `ORIG_HEAD` become collectable as soon as its admin entry is removed, and `git gc --prune=now` drops them at once. Before the move, the worktree's reflog would have protected them for the reflog expiry period.
  - This is the accepted reflog class of addendum 1. It is now stated for worktrees explicitly, because the vault does not make those commits restorable.
  - For a clone the whole object store moves with the vault, so its reflog-only commits do survive until purge.

## Addendum 4 (2026-10-23): changes forced by the fourth review (A4, data safety)

A fourth independent review, focused on data safety and run against `0ef5811`, found one high, one medium and two low issues. The three it fixed each have a test that reproduces the reported scenario, and those tests fail on `0ef5811`.

- **A repository inside an artifact can back a worktree outside it (F1, high).** A clone in a scratch directory, or a nested repository in a clone's `.venv/src`, can have a linked worktree anywhere on the disk. That worktree's index, HEAD, per-worktree refs and objects live in the nested repository's git dir, so moving the artifact breaks the outside worktree at once (`fatal: not a git repository`), and purging loses its staged work. The checks proved the nested repository clean and pushed, but never asked whether it had worktrees; only the top-level clone (C6) did.
  - When the worktree lay in an excluded project, nothing read `.git/worktrees/*/gitdir`, so the exclusion never fired.
  - Every linked worktree's path, as its admin `gitdir` file names it, is now one of the paths checked against the exclusions. This happens before any git runs.
  - A nested git dir with any entry under `worktrees/` is refused. The check reads the filesystem, so a worktree that git lists as prunable (for example on an unplugged drive) counts too.
- **Git LFS content is not proven pushed (F2, medium).** C4 and W4 prove that commits, and so the pointer files, are on a remote, not the LFS objects those pointers name. A skipped or failed `git lfs push`, or a push made while git-lfs was not installed, leaves the only copy in `.git/lfs/objects`. A clone or nested repository with any file there is now refused, since nothing offline proves the push. An empty store (for example after `git lfs prune`) is not refused.
- **Restore no longer rewrites other worktrees (F3, low).** `git worktree repair` rewrites every linked worktree of the repository into the configured path style. With `worktree.useRelativePaths=true`, restoring one worktree rewrote another worktree's `.git` file and admin `gitdir`. The paths stayed equivalent, but a worktree the user never chose was written to. Repair now runs with the setting forced off, which only fixes broken links.
- **Accepted (F4, low): a clone's own hooks and `rr-cache`.** A clone takes its `.git/hooks` and its recorded conflict resolutions (`rr-cache`) into the vault with it. They are restorable until purge and lost after. This is configuration rather than work, and most hooks are installed by a tool (`pre-commit install`) that can recreate them. Refusing every clone with a non-sample hook would refuse most real clones. For a worktree both live in the common dir, which is not moved.

## Addendum 5 (2026-10-23): changes forced by the fifth review (A5, data safety)

A fifth independent review, run against `5f4d7a9`, found five issues: one medium and four low. Each fix has a test that reproduces the reported scenario, and those tests fail on `5f4d7a9`.

- **The proofs read the real commit graph (F1).** A `.git/info/grafts` line can attach a local-only commit to a pushed one. With git's deprecation advice silenced, nothing on stderr betrayed it, so C4 saw the commit as reachable from `origin` and offered the only copy.
  - Git now runs with `GIT_GRAFT_FILE` naming a file that does not exist, and with `GIT_NO_REPLACE_OBJECTS=1`.
  - The file must be missing: git treats an existing one, even `nul`, as a graft file and prints the hint on every command.
  - If the chosen directory ever exists, git is not run at all.
- **git-annex is the same class as Git LFS (F2).** Its `annex/objects` store is checked like `lfs/objects`.
- **`log` no longer runs `gpg.program` (F3, partly).** With `log.showSignature=true`, the proof's `log -1` ran the signing program the config names. That setting is now forced off.
- **Accepted: clean filters run during `status`.** A clean filter configured for a stat-dirty tracked file, Git LFS among them, runs during the proof's `git status`, as it does in any `git status` the user runs. It is the user's own configured program, it runs only on content git is about to compare, and anything it writes stays in the repository. Refusing every repository with a filter attribute would refuse every LFS repository, including ones whose objects were pruned.
- **A listing never names an excluded project (F4).** When W2 refused first, its detail printed an untracked path under an excluded project into the report. A status listing that names an excluded path now hides the verdict like any other exclusion hit.
- **A clone of an excluded project is excluded wherever it sits (F5).** Remote URLs in a git dir's config (`url`, `pushurl`) are exclusion targets, checked before any git runs. A clone named `_verify-pr12` of `.../zzexclproj.git` is therefore hidden.
- **Hardening: inherited git variables are dropped.** Variables that point git at another repository, index, object store or ref view, or that inject config (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG_PARAMETERS` and the like), are removed before git runs. Inherited from a hook or a shell, they made every proof describe another repository. `GIT_CONFIG_GLOBAL` is kept, since a user can choose it deliberately.

## Addendum 6 (2026-10-23): changes forced by the sixth review (A6, data safety)

A sixth independent review, run against `905bfa1`, found one medium and two low issues, plus one over-refusal. Each fix has a test that reproduces the reported scenario, and those tests fail on `905bfa1`.

- **A clean filter can hide the only copy (F1, medium).** This narrows what addendum 5 accepted: a filter *running* is still accepted, but a filter *hiding content* is not.
  - A clean filter that drops content on its way into the index, such as `nbstripout` dropping notebook outputs, leaves the working file different from the commit. Once that commit is pushed and the index stat matches, `git status` is empty, and the dropped bytes exist only on disk.
  - W2, which every clone and nested repository also goes through, now refuses when any clean or process filter applies to a tracked file. Git LFS is the exception: it keeps what it replaces in its store, which is checked separately.
  - The exemption follows the configured program (`git-lfs ...`), not the driver's name.
  - Comparing `hash-object --no-filters` against the blob would be more precise, but it mismatches every file that `core.autocrlf` converts on Windows, so it was not used.
  - A filter installed globally but applying to no tracked file does not refuse.
- **No refusal detail names an excluded project (F2, F3).** Some rules run before the subtree exclusion walk (W2, W8, S2, S8), and their details can name a path inside the artifact.
  - Every evaluator's refusal is now hidden when the artifact's subtree holds an excluded name. A walk that fails counts as a hit.
  - A skip-worktree path, which is not on disk under sparse checkout, is matched on its own.
- **`status.showUntrackedFiles=no` no longer refuses everything (O1).** With that setting configured, W8's `status --ignored=matching` was a fatal git error. W8 now passes the default `--untracked-files=normal` explicitly.
- **Known, safe direction (O2):** in a reftable repository the admin HEAD is `ref: refs/heads/.invalid`. `after_vault_move` therefore always keeps the admin entry, and the worktree shows as prunable. No commit is lost; only the bookkeeping is skipped.

## Addendum 7 (2026-10-24): the seventh review (A7, data safety) passed; two read-only gaps closed

A seventh independent review, run against `eeeb493`, **passed for data safety**. It found no realistic disk state in which applying the offer, and optionally restoring, loses work, changes another worktree, or writes outside the vault. It left two low-realism gaps in which git *read* an excluded project's files. Nothing could be offered through either. Both are closed, each with a test that fails on `eeeb493`.

- **Config file references are exclusion targets (F1).** A git dir's config can name files that git reads on every command: `[include]` and `[includeIf]` paths, `core.excludesFile` and `core.attributesFile`. One inside an excluded project was read by every proof. These targets are now matched before any git runs.
- **A local remote is matched as a path (F2).** `git clone <root>\zzexclproj\.git` produces a remote URL ending in `.git` itself, so no repository name came out of it. The name now skips a trailing `.git` part, and a local remote's own path (a drive, root or `file://` path) is matched against the exclusions as a path.
- **Recorded, safe direction, not changed:**
  - Worktrees of a bare repository are refused at W1, because the admin dir's parent is not named `.git`.
  - A worktree whose admin `gitdir` is relative keeps its admin entry, so the bookkeeping is skipped and the worktree shows as prunable.
  - If the user rebuilds a vaulted worktree by hand on the same branch and then deletes that folder, restore can adopt the leftover admin entry, whose HEAD is identical. What is lost is the saved index, proven clean, and the reflog: the accepted class.

## Addendum 8 (2026-10-24): the eighth review (A8) found a crash in the addendum-7 change

A narrow eighth review of the addendum-7 change (`98218ce`) found a regression and some remaining spellings. Nothing that the earlier head refused was offered, so no data was at risk. Each fix has a test that fails on `98218ce`.

- **A home path that cannot be expanded no longer crashes detection (F1).** `Path("~otheruser/x").expanduser()` raises `RuntimeError`, which `except OSError` did not catch. So a single `core.excludesFile = ~nosuchuser/...`, or a `[submodule] path = ~lib` that git accepts, aborted the whole detection run. Such a path now counts as excluded. The `path` key is no longer read at all: it also matched `[submodule] path`.
- **Includes are not followed; any include counts as excluded (F2).** An include can be nested, quoted in pieces, continued across lines or conditional. Following one reliably would take git, and git would read the included file itself.
  - Any `[include]` or `[includeIf]` section, or any continued line, in a git dir's own config therefore hides the artifact before git runs.
  - Repository-local includes are rare, and hiding is the safe direction.
  - The other values read (`core.worktree`, remote URLs, `core.excludesFile`, `core.attributesFile`) have their quotes removed, as git joins quoted pieces, and are also matched when written on their section's header line. `file://host/` prefixes are stripped.
- **Not covered, on purpose:** other keys that name files (`core.hooksPath`, `commit.template`, `mailmap.file`). None of the read-only proofs reads them: the proofs run `status`, `ls-files`, `rev-list`, `for-each-ref`, `config`, `check-attr` and `log -1 --format`, never a commit or a hook. (This is reasoned from that command list, not tested key by key.)

## Addendum 9 (2026-10-24): the ninth review (A9) passed; one realistic over-exclusion fixed

A ninth independent review, of the addendum 7 and 8 changes (`89f6010` against `eeeb493`), **passed for data safety**. Nothing newly offered, and none of 15 crafted configs raised: binary junk, UTF-16, a config that is a directory, NULs, 120k-character values, device and UNC paths. It found one realistic over-exclusion and two smaller issues, all fixed with tests that fail on `89f6010`.

- **An escaped trailing backslash is no continued line (F1, realistic).** Git writes a value's trailing backslash as `\\`. The addendum-8 check read that as a continued line, so any clone whose remote URL, excludes file or alias ended in a backslash was hidden. A Windows path from tab completion is enough (`git clone D:\repos\x\ dest` stores `url = D:\\repos\\x\\`). A continued line is now an *odd* run of trailing backslashes.
- **An include anywhere counts (F2).** Git honours `[include]` after another header on the same line, and after a UTF-8 BOM. Both were missed. `[include` is now matched anywhere in the file, and the key prefix allows a BOM.
- **Config scans are linear (F3).** `^\s*` with MULTILINE went quadratic on blank lines: 50,000 blank lines took 213 s, where git takes 0.03 s. Every pattern now uses `[ \t]*`, and values stop at the line end.
- **Checked and still offered:** configs written by mainstream tools: `git lfs install --local`, Git Credential Manager settings, `gh` remote markers, VS Code branch settings, `lfs.url`, and `core.excludesFile = ~/.gitignore_global`.
