# Spec: stale AI-agent worktrees and finished agent scratch

**Target user:** a developer who runs several AI coding agents (Claude Code today) against their own repos on one Windows machine and has never cleaned up after them.
**Pain point:** agents leave a git worktree (with its own `node_modules`/`.venv`) per task and a scratch directory per session; on the owner's machine that is the single largest consumer of C:, and the only safe way to clean it today is a human reading `git status` in dozens of directories.
**Success metric:** on the fixture suite (section 8), ZERO unsafe selections (hard gate: nothing with unpushed commits, uncommitted or hidden-modified files, a live process, an unmerged branch, a junction to outside data or an excluded name is ever offered or removed), and on the owner's machine the dry run's recall is MEASURED and reported (target: most of the bytes a human reviewer agrees are finished; expect a real loss from junction-bearing worktrees and globally ignored files, see section 4, so no percentage is promised before the first measurement).
**Who pays:** the user; no inference calls, no per-use cost. It is a new Reclaim category, not a new product.

Status: BUILT 2026-10-20 (ADR-0043); section 10 lists every place where the build differs from sections 2-9 below, and it wins where they disagree. Written 2026-10-15, revised the same day after two hostile fact-checks: the first reproduced two data-loss cases against the first draft, the second found submodule and nested-repo holes in the revision (sections 3, 4, 5 and 9 record what changed). Related: ADR-0039 (user exclusion list), ADR-0034 (regenerable tier), ADR-0005 (vault retention), `docs/specs/assistant-mcp.md` (same "provable rules, hard-zero unsafe gate" approach).

## 1. What this machine looks like (VERIFIED, read-only walks on 2026-10-12 unless noted)
- `%TEMP%` held 129.6 GiB at 21:47 (`%TEMP%\claude` is nearly all of it, one directory per project; the largest eight were): the gg-portfolio project 81.5 GiB (newest write 21:45, i.e. live), gold-rate-tracker 18.8, review-iq 9.1, triage-iq 5.9, agentgauge 5.5, reclaim 2.7. The owner reported one gg-portfolio session holding about 127 GB in total, together with its 18 `gg-portfolio-wt-*` worktrees (about 1.6 GB each, 1.09 GB of that `node_modules`), two `gg-portfolio-verify*` copies and a `_verify305` clone under `C:\Users\gaura\ml-projects\`.
- Layout of agent scratch (VERIFIED 2026-10-15 by listing): `%TEMP%\claude\<project-slug>\<session-uuid>\...`; `<project-slug>` is the project path with separators turned into `-` (`C--Users-gaura-ml-projects-reclaim`; a lowercase variant `c--users-gaura-ml-projects-gg-portfolio` also exists, so slug matching is case-insensitive). Of 15 session directories, 9 contain exactly `scratchpad` and `tasks`; the other 6 hold a subset of them plus extras (for example `srv-b.pid`). There are also non-UUID siblings (`shima`, `shimb`) that are not sessions. The same `<session-uuid>` names the transcript `C:\Users\gaura\.claude\projects\<project-slug>\<session-uuid>.jsonl`.
- Process working directories of same-user processes are readable with `psutil.Process.cwd()` (VERIFIED), but only 134 of 287 processes were readable (the rest are presumably other users' or system processes, not broken down by owner), and `psutil` is NOT a Reclaim dependency today (nothing in `pyproject.toml`, `uv.lock` or `src/`).
- Reclaim already refuses by name: ADR-0039 turns each `[exclusions] project_names` entry into a case-insensitive `*<name>*` glob on the whole path, which covers `<name>-wt-x` worktrees and scratch directories embedding the name (the ADR names `%TEMP%\claude\C--Users-<u>-ml-projects-<project>`).
- Overlap to resolve (VERIFIED by reading `regenerable.py` and `executor.py`): the weekly regenerable `aged_temp` sweep already treats `%TEMP%\claude` as a direct TEMP child and deletes it permanently once all its content is older than 7 days, except when a git repo, venv or `node_modules` is inside (those are "guarded" and left for review). Category B (section 5) must not be weaker than that existing guard, and the two must not both claim the same directory (open item 6).

## 2. Scope
In: (A) linked git worktrees created by agents, (B) finished Claude Code session scratch under `%TEMP%\claude`. Out: anything an agent wrote into a repo's tracked tree, other agents' scratch layouts (Codex, Cursor and others are UNVERIFIED here: no layout was inspected, so no rule is written for them), and the main checkout of any repo. Both categories are OFF until the user enables them in Settings: they are other tools' data, not Reclaim's caches.

## 3. Principles
1. Prove finished, never guess. Every rule below is a conjunction; any check that cannot complete (git fails or returns a code other than the documented ones, a process cannot be inspected, a walk errors, a path is unreadable) makes the item NOT a candidate (fail closed, global rule 98a). `git merge-base --is-ancestor` returns 0 (yes), 1 (no) and 128 (bad ref): only 0 counts.
2. Nothing is deleted from a live session. "Live" is a positive finding of use, not an absence of recent writes.
3. Excluded projects and `[safety] deny` win over everything (ADR-0039), including through worktree names (`<excluded>-wt-*`) and scratch slugs.
4. Everything is re-checked at apply time, after the user's click, against the state of the disk then (the same TOCTOU rule as the MCP delete, #164).
5. **Read-only means read-only.** Every git command here runs as `git --no-optional-locks -c core.fsmonitor=false ...`: a plain `git status` refreshes and REWRITES the worktree's index (reproduced), which can collide with a live agent's git process (`index.lock`), and `core.fsmonitor` would run a program from the repo's own config.
6. **Do not delegate safety to `git worktree remove`.** Reproduced on git 2.51: without `--force` it refuses tracked/untracked changes and locked worktrees, but it SUCCEEDS and deletes (a) the contents of a junction that sits in an ignored path (`node_modules` junctioned to another folder: the other folder's files were deleted), (b) local edits to tracked files marked `skip-worktree` or `assume-unchanged` (status prints nothing), and (c) any ignored files (`.env`, `*.db`). Reclaim therefore proves the conditions itself and removes the tree with its own reparse-point-aware code.
7. Reversible first: scratch goes to the vault; a worktree is recreatable from its remote (section 4), and its non-regenerable ignored files are never auto-removed.

## 4. Category A: `agent_worktree`
Unit: one linked worktree registered by `git worktree list --porcelain` in a repository Reclaim can see (never the main worktree, never a bare repo, never an entry printed as `locked` or `locked <reason>`, never a `prunable` entry: the directory is already gone). All of the following must hold:

| # | Rule | How it is proven (read-only) |
|---|---|---|
| W1 | It is a linked worktree of a repo on this machine | `git -C <repo> worktree list --porcelain` lists the path; `.git` inside it is a file pointing into `<repo>\.git\worktrees\<name>`; the per-worktree git dir below is read with `git -C <wt> rev-parse --git-dir`, never assumed to be `<wt>\.git` |
| W2 | No uncommitted or hidden work | `git -C <wt> status --porcelain=v1 --untracked-files=all` exits 0 and prints nothing (an untracked file counts); AND `git -C <wt> ls-files -v` shows only `H` (any `S` skip-worktree or lowercase assume-unchanged flag fails the check); AND none of `MERGE_HEAD`, `REVERT_HEAD`, `CHERRY_PICK_HEAD`, `REBASE_HEAD`, `BISECT_LOG`, `rebase-merge/`, `rebase-apply/`, `sequencer/` exists in the per-worktree git dir. Status is run with `--ignore-submodules=none` (a repo or user config of `submodule.<name>.ignore=all` / `diff.ignoreSubmodules` otherwise hides a dirty submodule: reproduced). The worktree is refused outright when its index has any gitlink (`git ls-files -s` shows mode `160000`), when `.gitmodules` exists, or when `<git-dir>\modules` exists: a submodule's own git directory lives INSIDE the per-worktree git dir, so removal and `git worktree prune` would destroy its commits and stash (reproduced) |
| W3 | The remote is a real remote | the remote's resolved URL (`git remote get-url <remote>`, which applies `url.<base>.insteadOf`) must match a SCHEME ALLOW-LIST: `https://`, `ssh://`, `git://`, or scp-style `user@host:path`. Anything else is refused: a drive-letter path, a UNC path, `file://`, a relative or absolute POSIX path (a clone of another folder on this disk is not a backup; reproduced that `get-url` returns plain `D:/...` paths). No remote at all means unpushed |
| W4 | Its HEAD is on the remote's default branch | `git -C <wt> merge-base --is-ancestor HEAD <remote>/<default>` returns 0, with `<default>` read from `refs/remotes/<remote>/HEAD` (a missing remote HEAD fails closed). No `git fetch` is run (offline): the answer is as fresh as the last fetch, may be stale or force-moved on the remote, and the review row says when the refs were last fetched. A squash-merged PR is NOT an ancestor (VERIFIED: rc 1), so those worktrees are not candidates in v1 (open item 3). Note that removing a worktree keeps its branch and commits in the main repository, so this rule protects mainly a detached HEAD |
| W5 | No live process | no process of the CURRENT USER whose `cwd`, or any path-like TOKEN of whose command line, lies inside the worktree: each token is normalised (case-fold, forward to back slashes, strip a `\\?\` prefix, 8.3 names expanded with `GetLongPathNameW` on the token, which works on a path and not on a raw substring) and compared on a path boundary, so `wt-1` never matches `wt-10` (a substring match would only be a safe false positive). A same-user process that cannot be inspected fails the check; other users' and system processes are out of scope (this needs the owner SID of each process, open item 1) |
| W6 | Not part of a live session | no `<session-uuid>.jsonl` transcript modified within 7 days under EITHER the repo's slug OR the worktree's own slug (a session started inside the worktree has the worktree's path as its project; how `_` and `.` in a name become `-` in a slug is UNVERIFIED, all real project directories seen use `-`), and no session scratch directory newer than 7 days that mentions the worktree path (reading file contents, to be specified). This is a convenience; the safety rests on W2-W4 |
| W7 | Not excluded | the worktree path and its repo path do not match `[exclusions] project_names` or `[safety] deny` (the `exclusion_match` check only; the validator's built-in "inside a git repository" deny is a different rule and would reject every worktree if read literally) |
| W8 | No ignored content except regenerable directories, and no reparse point anywhere | `git status --ignored=matching --porcelain` lists only directories named `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.next`. Everything else ignored (`.env*`, `*.db`, `*.sqlite*`, `*.pem`, `*.key`, `*.pfx`, data and model dirs, and also `build`, `dist`, `.cache`, `target`, which can hold non-regenerable output) makes the worktree a review-only item that lists those paths and is never selected by "clean all". Reclaim walks the whole tree (not following links) and refuses any worktree containing ANY junction or symlink, including inside `node_modules` (the first draft's "lose only the link" cannot be honoured by git). Recall cost, measured by reading npm 10's installer: plain `npm install` on Windows writes `.bin` as cmd-shim FILES, not links, so ordinary trees pass; npm workspaces, `file:` dependencies and `npm link` create junctions and are refused; pnpm is UNVERIFIED. `--ignored=matching` also lists globally ignored files (`.vscode/`, `.idea/`, `.vercel/`, `*.log`, `*.tsbuildinfo`, `.coverage`, `dist`), which makes those worktrees review-only: both losses are expected, unmeasured, and the reason section 1's metric is a measurement rather than a promise |

Removal (SUPERSEDED by section 10: items are moved into the vault, not deleted): Reclaim re-runs W1 to W8 immediately before acting, then deletes the tree with its own non-following removal (the reparse-aware code the executor already uses), then runs `git worktree prune`. It never deletes the repo's branch; the branch ref stays, so the work is one `git worktree add` away. The freed bytes are real: no Recycle Bin and no vault, because a vault copy of a `node_modules` tree would defeat the purpose and everything in the tree is in the remote or regenerable by W2-W4 and W8. Residual risk, stated: files inside `node_modules`/`.venv` that were hand-edited are lost (they are regenerable by definition; an edit inside them is not supported).

Tier: B (review queue) in v1. Tier A (automatic, under the weekly auto-clean) is out of scope until the measurement gate in section 8 has run clean for four consecutive weekly dry runs on the owner's machine, and then only for worktrees idle 14+ days.

Pre-existing gap noted, not fixed here: `scanner.py` finds a repo root by looking for a `.git` directory, but a linked worktree's `.git` is a file, so `git_repo_root` is `None` there and existing detectors see worktree contents (for example `node_modules`) without in-repo protection. Candidates inside an agent worktree are therefore already offered today; this category should take them over (open item 7).

## 5. Category B: `agent_session_scratch`
Unit: one `%TEMP%\claude\<project-slug>\<session-uuid>` directory (case-insensitive slug). All of the following must hold:

| # | Rule | How it is proven |
|---|---|---|
| S1 | Identity | the parent is `%TEMP%\claude\<slug>` and the child name is a UUID. Contents are NOT required to be exactly `scratchpad` and `tasks` (6 of 15 real session directories hold extras such as `srv-b.pid`); non-UUID siblings such as `shima`/`shimb` are not sessions and are never candidates |
| S2 | Idle 7+ days | newest `mtime` of ANY file in the tree (full walk, no reparse points followed) is older than 7 days; the directory's own mtime is not used; a walk error fails the check |
| S3 | Its transcript is idle too | `C:\Users\gaura\.claude\projects\<slug>\<session-uuid>.jsonl` is absent or older than 7 days |
| S4 | No live process | no process of the current user whose command line or `cwd` contains `<session-uuid>` or the scratch path (normalised as in W5) |
| S5 | Not excluded | the slug embeds an excluded project name (ADR-0039 already blocks `%TEMP%\claude\...-<project>`), or matches `[safety] deny` |
| S6 | Not the running session | the scratch path of the session that is calling Reclaim is skipped |
| S7 | No unproven repository inside | scratch trees are "full of cloned repos" (a comment in `executor.py` says so). Any `.git` (directory or file) found under the scratch directory makes the directory a candidate only if, for that repository: W2 holds (with the submodule refusal), W3 holds, EVERY local branch and tag (`git for-each-ref refs/heads refs/tags`) is an ancestor of the remote default branch, and there is no stash (`refs/stash`): a deleted clone takes its other branches and its stash with it, which a HEAD-only check misses. A `node_modules`/venv inside follows W8. This is different from, NOT "at least as strict as", the existing `aged_temp` guard: that guard leaves the whole top-level entry for review on ANY `.git`, `.venv`, `venv`, `pyvenv.cfg`, `site-packages` or `node_modules` at any depth, whereas S7 admits such a directory when it proves each repository clean and pushed (stricter about git, looser about regenerable environments) |
| S8 | No reparse point | the walk finds no junction or symlink anywhere in the tree |

Removal: vault with a 7-day retention (scratch is not rebuildable, so ADR-0005's zero-retention shortcut does NOT apply), then purge frees the space. Tier B in v1; the "finished sessions only" rule is the owner's own standing permission for the three excluded projects' scratch (2026-10-10, an owner instruction recorded in `docs/RESUME.md`), here made a product rule for all projects.

## 6. Exclusions and live sessions, concretely
- Exclusions: the same `exclusion_match` path as every other category; the report of an apply states that no excluded project appeared among applied candidates (CLAUDE.md rule 3). The regenerable tier has its own excluded-pattern path (ADR-0034); this category must be checked against it too.
- Another live session's worktrees and scratch are never touched, only shown: a session working in `gg-portfolio` appears to Reclaim only as items that fail W5/W6 or S2-S4, shown as "in use by a live session" with the reason and never selectable. The user (not Reclaim) decides to close that session.
- Nothing here talks to the agents: no signal, no kill, no lock file.

## 7. UI and reporting
Review queue rows show: repo, branch, last commit date, size, when the remote refs were last fetched, why it qualifies (the W/S rules that held), and for a refusal the first rule that failed. The summary line "Agent leftovers: N worktrees, M scratch sessions, X GB" appears under the category group; the 80% disk alert may mention it as the largest reclaimable group.

## 8. Eval and acceptance (written alongside the feature, hard gate)
Fixture suite, each case a synthetic repo built in a tmp dir (hermetic, CLAUDE.md rule 7), one JSON expectation per case. Worktrees: clean+merged (offered); dirty tracked; dirty untracked; `skip-worktree` edit; `assume-unchanged` edit; unpushed commit; detached ahead of remote; no remote; remote that is a local path; no upstream; ref that does not exist (rc 128); branch not merged; squash-merged (not offered); stash only (offered; stashes are repo-wide and survive removal); merge, revert, cherry-pick, rebase and bisect in progress; locked with and without a reason; prunable; submodule (git refuses it), a submodule hidden by `submodule.<name>.ignore=all` with a dirty file inside, a worktree containing any gitlink or `.gitmodules`; live process with `cwd` inside; live process with the path in different spellings (slashes, case, `\\?\`, 8.3); an ignored junction in `node_modules` pointing at a folder with files (the files must survive, and the worktree must be refused); ignored `.env`, `*.db`, `build/` (review-only); excluded repo name; excluded `-wt-` suffix; path over MAX_PATH; shallow clone; worktree whose repo is gone; `git` missing. Scratch: idle 6.9 days vs 7.1 days; fresh transcript; extras such as `srv-b.pid` (still a candidate); a non-UUID sibling (not a candidate); a nested clone with an unpushed commit (not a candidate), a nested clone whose HEAD is pushed but another local branch or a stash is not (not a candidate), a `.git` file pointing at a missing git dir (git exits 128: not a candidate), globally ignored `.vscode/` (review-only); a junction inside; excluded slug; mixed-case slug; walk error. Hard gate: 0 unsafe selections; measured: precision 100% and recall reported per rule.

## 9. Open items and UNVERIFIED claims
1. Process inspection: `psutil` is not a dependency (adding one, or using `NtQueryInformationProcess` through `ctypes`, is a decision), and 153 of 287 processes were not readable on this machine; the rule needs the owner of each process so that only the current user's processes must be inspectable. UNVERIFIED how many same-user processes are unreadable with an EDR or an elevated agent running.
2. Other agents' layouts (Codex, Cursor, Aider): not inspected, no rules written.
3. GitHub-side "PR merged" for squash merges (needs network and a token; would extend W4).
4. Whether to ever promote Category A to Tier A (section 4 sets the measurement gate; the decision is the owner's).
5. Worktrees inside a repo whose main checkout is on another drive or a network share: out of scope. Note the existing behaviour precisely: the scanner does not refuse network roots (it scans them under a timeout guard); `SafetyValidator` hard-denies UNC and UNC-resolving paths (D13).
6. Reconcile with the weekly `aged_temp` sweep, which can already delete finished `%TEMP%\claude` children permanently: decide whether Category B replaces that behaviour for `%TEMP%\claude` (reversible, with S7) or stays a read-only report next to it. Until decided, nothing in this spec may weaken the sweep's guard.
7. Take over detector candidates that today appear inside agent worktrees (see the pre-existing gap in section 4), so a `node_modules` in a worktree is never offered on its own.
8. `git status` can execute programs named in the repo's own config (`core.fsmonitor`, hooks are not run by status); the spec disables fsmonitor, but a hostile repo is out of scope (these are the owner's own repos).
9. NOT CHECKED: `rebase-apply/` and `MERGE_HEAD` appear in the per-worktree git dir (the other states were reproduced), a `file://`, UNC or relative remote form (the allow-list refuses all non-listed forms by construction), sparse checkouts (inconclusive: files stayed present, `ls-files -v` showed only `H`), `core.worktree` oddities, a worktree on another drive, and a `.git` pointer file whose path contains spaces.

## 10. As built (2026-10-20): where the implementation differs from sections 2-9
Owner decisions of 2026-10-20 and what building it taught. Where this section and the text above
disagree, this section is right. Code: `src/reclaim/agent_artifacts.py`, `src/reclaim/procscan.py`;
hooks in `executor.py` (apply-time re-proof, rename-only move, prune/repair) and `detectors.py`.

1. **Three categories, not two.** Added `agent_clone` (C1-C7): standalone throwaway clones whose
   directory name matches `[categories.agent_artifacts] clone_name_patterns` (default `*-verify*`,
   `_verify*`, case-insensitive). Rules: a standalone `.git` directory; clean (W2) and no stash; EVERY
   local branch, tag and HEAD contained in a remote ref of a real remote (`git rev-list --branches
   --tags HEAD --not --remotes=<name>` is empty); no linked worktrees depend on it; no live process;
   not excluded; regenerable ignored content only; no reparse point; same volume as the vault.
2. **W3/W4 relaxed to "every commit of HEAD is on a real remote ref".** The spec required HEAD to be an
   ancestor of the remote DEFAULT branch, which refuses every worktree on a pushed feature branch.
   The build requires `git rev-list HEAD --not --remotes=<allowed remote>` to be empty, where an
   allowed remote is one whose fetch URL passes the scheme allow-list (W3, unchanged). A squash-merged
   branch with its remote branch deleted is still not offered.
3. **Removal is a vault move for all three categories (supersedes the worktree "Removal" paragraph).**
   A same-volume `os.rename` into the vault with a manifest entry and retention
   (`retention_days`, default 14, minimum 1; never a permanent delete), restorable with
   `reclaim undo`. The copy-verify-delete fallback is DISABLED for these categories: a refused rename
   is reported as a failed item and the directory is left untouched. An item on a different volume
   than the vault is not offered (rule V). Worktrees: the git admin directory is copied into the
   vaulted directory (`.reclaim-gitadmin/`) and exactly that one entry is removed (item 12: not `git worktree prune`); restore puts it back,
   removes the helper folder, runs `git worktree repair` and checks git lists the worktree.
4. **Always Tier B, off by default, forced off in SAFE mode** (added to
   `SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS`: SAFE mode never uses the vault). Assistant (MCP) requests go
   through the same approval card as any delete (`reversible_only` batches vault these items).
5. **W6 narrowed.** Only a transcript under the WORKTREE'S OWN project slug counts; the repository's
   slug is ignored (otherwise every worktree of an active repository would be refused for as long as
   anyone works in it, which is the common case). Slugs are compared after mapping every
   non-alphanumeric to `-` on both sides.
6. **S2 counts directories too.** The idle test uses the newest mtime of any FILE OR DIRECTORY in the
   tree (the spec said files only): a rename or a deletion bumps a directory's mtime and is activity.
   More conservative than the spec; the scan index (aggregate rows included) gives an early refusal
   so an active tree is never walked.
7. **Process inspection decided: ctypes, no `psutil`** (spec open item 1). Same-user processes only
   (SID compared); a same-user process that cannot be read fails the check, EXCEPT unreadable
   Windows/vendor service binaries under %SystemRoot%, Program Files or ProgramData whose name is
   not a shell/interpreter/work host (`procscan.is_vendor_service`). Measured on the owner's machine:
   all 6 same-user unreadable processes were OEM/Windows helpers (Tobii, NVIDIA, ctfmon, an Fn-key
   helper, a WMI host, Defender); without that rule nothing would ever be offered.
8. **Apply-time re-proof is total.** `executor._preflight_skip_reason` re-runs every rule from live
   state (fresh process scan included) before each move; failure skips the item
   (`agent_artifact_not_provably_finished`); a non-vault method is refused
   (`agent_artifact_requires_vault`); a missing validator or vault refuses.
9. **Nested candidates.** An offered artifact takes over ordinary candidates inside it (open item 7),
   and a scratch directory holding a worktree or clone is offered once, as the outer item.
10. **UI.** Rows carry the project, branch, last commit date, remote-ref fetch time, size and the
    exact rules that held in the rationale text; grouping is by category group in the existing review
    queue, and `reclaim agent-artifacts` (read-only dry run) prints the list grouped by project with
    the first failed rule of every kept item (`--all`). No dedicated project-grouped view was built.
11. **Open items status.** 1 decided (item 7 above). 2, 3, 4 unchanged. 5 unchanged. 6: the weekly
    `aged_temp` sweep is untouched; both may see the same scratch directory, and whichever acts first
    wins (the other finds it gone). 7 done (item 9 above). 8, 9 unchanged.
12. **Changes forced by the first independent review (2026-10-20; ADR-0043 Addendum 1).** Backed up
    means BOTH fetch and push URL are real (no loopback, no drive path); clones and nested repositories
    are compared against `--all` refs; repositories nested in `node_modules`/`.venv` and any bare
    repository are proven or refused (W9, C8, S7); ignored content inside a regenerable-named directory
    counts only if nothing under it is tracked; `index.lock` blocks; a worktree's admin entry is removed
    EXACTLY (no `git worktree prune`, which also drops other prunable worktrees' entries), and restore
    never writes into a moved or different repository; process matching resolves relative arguments,
    `..`, junction aliases and local administrative shares; exclusions are checked on real locations and
    on the Claude slug. Items 3 and 8 above are amended accordingly (prune -> exact entry removal).
