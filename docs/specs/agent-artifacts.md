# Spec: stale AI-agent worktrees and finished agent scratch

**Target user:** a developer who runs several AI coding agents (Claude Code today) against their own repos on one Windows machine and has never cleaned up after them.
**Pain point:** agents leave a git worktree (with its own `node_modules`/`.venv`) per task and a scratch directory per session; on the owner's machine that is the single largest consumer of C:, and the only safe way to clean it today is a human reading `git status` in dozens of directories.
**Success metric:** on the fixture suite (section 8), ZERO unsafe selections (hard gate: nothing with unpushed commits, dirty files, a live process, an unmerged branch or an excluded name is ever offered or removed), and on the owner's machine the dry run proposes at least 90% of the bytes a human reviewer agrees are finished.
**Who pays:** the user; no inference calls, no per-use cost. It is a new Reclaim category, not a new product.

Status: spec only, no build. Written 2026-10-15. Related: ADR-0039 (user exclusion list), ADR-0034 (regenerable tier), ADR-0005 (vault retention), `docs/specs/assistant-mcp.md` (same "provable rules, hard-zero unsafe gate" approach).

## 1. What this machine looks like (VERIFIED, read-only walks on 2026-10-12)
- `%TEMP%` held 129.6 GiB at 21:47 (`%TEMP%\claude` is nearly all of it, one directory per project; the largest eight were): the gg-portfolio project 81.5 GiB (newest write 21:45, i.e. live), gold-rate-tracker 18.8, review-iq 9.1, triage-iq 5.9, agentgauge 5.5, reclaim 2.7. The owner reported one gg-portfolio session holding about 127 GB in total, together with its 18 `gg-portfolio-wt-*` worktrees (about 1.6 GB each, 1.09 GB of that `node_modules`), two `gg-portfolio-verify*` copies and a `_verify305` clone under `C:\Users\gaura\ml-projects\`.
- Layout of agent scratch (VERIFIED by listing): `%TEMP%\claude\<project-slug>\<session-uuid>\{scratchpad, tasks}`; `<project-slug>` is the project path with separators turned into `-` (`C--Users-gaura-ml-projects-reclaim`). The same `<session-uuid>` names the transcript `C:\Users\gaura\.claude\projects\<project-slug>\<session-uuid>.jsonl`.
- Process working directories of same-user processes are readable (VERIFIED with `psutil.Process.cwd()` on the live `claude.exe`, `node.exe`, `cmd.exe` processes: it returns each one's project directory).
- Reclaim already refuses by name: ADR-0039 makes `[exclusions] project_names` block any path whose name embeds the project, and it names `%TEMP%\claude\C--Users-<u>-ml-projects-<project>` explicitly.

## 2. Scope
In: (A) linked git worktrees created by agents, (B) finished Claude Code session scratch under `%TEMP%\claude`. Out: anything an agent wrote into a repo's tracked tree, other agents' scratch layouts (Codex, Cursor and others are UNVERIFIED here: no layout was inspected, so no rule is written for them), and the main checkout of any repo. Both categories are OFF until the user enables them in Settings: they are other tools' data, not Reclaim's caches.

## 3. Principles
1. Prove finished, never guess. Every rule below is a conjunction; any check that cannot complete (git fails, a process cannot be inspected, a walk errors, a path is unreadable) makes the item NOT a candidate (fail closed, rule 98a).
2. Nothing is deleted from a live session. "Live" is a positive finding of use, not an absence of recent writes.
3. Excluded projects and `[safety] deny` win over everything (ADR-0039), including through worktree names (`<excluded>-wt-*`) and scratch slugs.
4. Everything is re-checked at apply time, after the user's click, against the state of the disk then (the same TOCTOU rule as the MCP delete, #164).
5. Reversible first: scratch goes to the vault; a worktree is recreatable from its remote (section 4), and its non-regenerable ignored files are never auto-removed.

## 4. Category A: `agent_worktree`
Unit: one linked worktree registered by `git worktree list --porcelain` in a repository Reclaim can see (never the main worktree, never a bare repo, never a `locked` worktree). All of the following must hold:

| # | Rule | How it is proven (read-only) |
|---|---|---|
| W1 | It is a linked worktree of a repo on this machine | `git -C <repo> worktree list --porcelain` lists the path; `.git` inside it is a file pointing at `<repo>\.git\worktrees\<name>` |
| W2 | No uncommitted work | `git -C <wt> status --porcelain=v1 --untracked-files=all` exits 0 and prints nothing (an untracked file counts as dirty); no `MERGE_HEAD`, `rebase-merge`, `rebase-apply`, `CHERRY_PICK_HEAD`, `BISECT_LOG` in its gitdir |
| W3 | No unpushed commits | `HEAD` is an ancestor of a remote-tracking ref: `git -C <wt> merge-base --is-ancestor HEAD <remote>/<branch>` for the branch's configured upstream, or for a detached `HEAD` of `<remote>/<default>`. No remote or no upstream means unpushed. No `git fetch` is run (read-only, offline): the answer is as fresh as the last fetch, and says so |
| W4 | Its branch is merged | `HEAD` is an ancestor of `<remote>/<default-branch>` (default branch from `refs/remotes/<remote>/HEAD`). A squash-merged PR is NOT an ancestor, so those worktrees are not candidates in v1 (they can be offered as "merged on GitHub? check" in Tier B later; that needs the network and a decision on tokens, so it is open item 3) |
| W5 | No live process | no process of the current user whose `cwd`, or whose command line, contains the worktree path (`psutil`); an inspection error on a same-user process fails the check |
| W6 | Not part of a live session | no `<session-uuid>.jsonl` transcript for the repo's slug modified within 7 days AND no session scratch dir newer than 7 days that mentions the worktree path (that is how a session that is paused but not finished is recognised) |
| W7 | Not excluded | passes `SafetyValidator` with the user's exclusions: the worktree path and its repo path must not match `[exclusions] project_names` or `[safety] deny` |
| W8 | Only regenerable ignored content | `git status --ignored=matching --porcelain` lists only directories on a closed regenerable list (`node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.next`, `dist`, `build`, `.cache`, `target`). Anything else ignored (`.env*`, `*.db`, `*.sqlite*`, `*.pem`, `*.key`, `*.pfx`, data dirs, models) makes the worktree a review-only item that lists those files and is never selected by "clean all" |

Reparse points: the walk and the removal never follow a junction or symlink inside the worktree (a junction to another drive must lose only the link). Any path that needs `\\?\` is handled the way the scanner already handles it.

Removal: Reclaim runs `git worktree remove <path>` (no `--force`, so git itself refuses a dirty tree) after re-running W1 to W8, then `git worktree prune`. It never deletes the repo's branch; the branch ref stays, so the work is one `git worktree add` away. The freed bytes are real (no Recycle Bin, no vault: a vault copy of a `node_modules` tree would defeat the purpose and everything in it is in the remote or regenerable by W3/W8).

Tier: B (review queue) in v1. Tier A (automatic, under the weekly auto-clean) is out of scope until the measurement gate in section 8 has run clean for four consecutive weekly dry runs on the owner's machine, and then only for worktrees idle 14+ days.

## 5. Category B: `agent_session_scratch`
Unit: one `%TEMP%\claude\<project-slug>\<session-uuid>` directory. All of the following must hold:

| # | Rule | How it is proven |
|---|---|---|
| S1 | Identity | parent is `%TEMP%\claude\<slug>`, the child name is a UUID, and the directory contains only `scratchpad` and `tasks` (any other entry: not a candidate) |
| S2 | Idle 7+ days | newest `mtime` of ANY file in the tree (full walk, no reparse points) is older than 7 days; the directory's own mtime is not used; a walk error fails the check |
| S3 | Its transcript is idle too | `C:\Users\gaura\.claude\projects\<slug>\<session-uuid>.jsonl` is absent or older than 7 days (a session that is open but quiet keeps writing its transcript when used) |
| S4 | No live process | no process of the current user whose command line or `cwd` contains `<session-uuid>` or the scratch path |
| S5 | Not excluded | the slug embeds an excluded project name (ADR-0039 already blocks `%TEMP%\claude\...-<project>`), or matches `[safety] deny` |
| S6 | Not the running session | the scratch path of the session that is calling Reclaim (MCP `client` or the dashboard's own user session) is skipped |

Removal: vault with a 7-day retention (scratch is not rebuildable, so ADR-0005's zero-retention shortcut does NOT apply), then purge frees the space. Tier B in v1; the "finished sessions only" rule is the owner's own standing permission (2026-10-10), here made a product rule.

## 6. Exclusions and live sessions, concretely
- Exclusions: the same `SafetyValidator` path as every other category; the report of an apply states that no excluded project appeared among applied candidates (CLAUDE.md rule 3).
- Another live session's worktrees and scratch are never touched, only offered: a session that is working in `gg-portfolio` appears to Reclaim only as items that fail W5/W6/S2-S4, so they are shown as "in use by a live session" with the reason, and never selectable. The user (not Reclaim) decides to close that session.
- Nothing here talks to the agents: no signal, no kill, no lock file.

## 7. UI and reporting
Review queue rows show: repo, branch, last commit date, size, why it qualifies (the W/S rules that held), and for a refusal the first rule that failed. The summary line "Agent leftovers: N worktrees, M scratch sessions, X GB" appears under the category group; the 80% disk alert may mention it as the largest reclaimable group.

## 8. Eval and acceptance (written alongside the feature, hard gate)
Fixture suite, each case a synthetic repo built in a tmp dir (hermetic, CLAUDE.md rule 7), one JSON expectation per case: clean+merged (offered), dirty tracked, dirty untracked, unpushed commit, detached ahead of remote, no remote, no upstream, branch not merged, squash-merged (not offered), stash only (offered; stashes are repo-wide and listed), merge/rebase in progress, locked worktree, live process with cwd inside, live process with the path on its command line, junction to another volume inside the tree (target must survive), `.env` ignored file present (review-only), excluded repo name, excluded `-wt-` suffix, path over MAX_PATH, shallow clone, worktree whose repo is gone, `git` missing. Scratch: idle 6.9 days vs 7.1 days, fresh transcript, extra entry besides `scratchpad`/`tasks`, excluded slug, walk error. Hard gate: 0 unsafe selections; measured: precision 100% and recall reported per rule.

## 9. Open items and UNVERIFIED claims
1. Whether `psutil` can read `cwd` of every same-user process on a machine with an EDR or an elevated agent; a process that cannot be inspected is treated as live (fails closed), which may hide worktrees. UNVERIFIED beyond the 13 processes sampled.
2. Other agents' layouts (Codex, Cursor, Aider): not inspected, no rules written.
3. GitHub-side "PR merged" for squash merges (needs network and a token; would extend W4).
4. Whether to ever promote Category A to Tier A (section 4 sets the measurement gate; the decision is the owner's).
5. Worktrees inside a repo whose main checkout is on another drive or a network share: out of scope (the scanner's network-root refusal applies).
