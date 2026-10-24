# Plan: an automatic tier for agent artifacts (plan and eval design only -- NOT enabled)

**Target user:** a developer who runs many AI-agent sessions and never reviews their leftovers, so the "Agent Leftovers" review list (ADR-0043) grows unread.
**Pain point:** agent scratch accumulates at tens of GB per month (about 180 GB measured on 2026-10-16, ADR-0043), and a Tier-B click per item is a chore nobody does.
**Success metric:** GB returned per month by unattended runs, with **0 wrong automatic moves** in the gate (below) and in shadow mode.
**Who pays:** the user, in disk space. No spend is involved.

Status: plan written 2026-10-24 (autonomous run, item D7). Nothing here is built or switched on. Enabling any part needs the owner's decision and its own ADR.

## 1. Where the automatic boundary is today

Two mechanisms clean without a per-item click, and neither fits agent artifacts as built:
- **The regenerable path (ADR-0034)** runs in safe mode and on the weekly schedule. It is safe because it is closed: a fixed allow-list in code, no scan index, no caller-supplied path. Agent artifacts are scan-driven (sizes and ages come from the index) and need live git and process proofs. Adding them there would open the closed path.
- **Tier A in power mode** goes through `apply_batch` with the user's blanket approval. ADR-0043 fixed all three agent categories at Tier B, forced off in safe mode.

So "automatic" means a third, separately gated mechanism, and it would stay reversible: the vault rename stays the only removal (ADR-0043 decision 4).

## 2. What could become automatic, in order of risk

| Category | Proposed | Why |
|---|---|---|
| `agent_session_scratch` (`%TEMP%\claude\<project>\<session>`) | **Candidate for phase 1** | Holds no git history by construction. Its owner session is identifiable. Losing it costs a re-run at most. |
| `agent_clone` (`*-verify*`, throwaway clones) | Not before phase 1 has a clean record | "Pushed" is only as fresh as the last fetch. A clone's untracked or ignored files are invisible to git. |
| `agent_worktree` | **Never automatic** | Removing a worktree with unpushed or hidden work destroys what cannot be rebuilt (ADR-0043 context). A click per worktree stays the price. |

Phase-1 rule (all must hold, every one re-proven from live state at move time, as `reverify` already does):
1. ADR-0043's whole S-rule proof passes.
2. **Stricter age:** no file or directory inside is newer than 14 days, measured on the newest content, never on the directory's own mtime (#110).
3. **Owner session ended:** the session id in the path matches no running process's command line or working directory (`procscan`). Any unreadable same-user process refuses, as today.
4. **Never the current session's scratch**, matched by session id, not by age.
5. No `.git`, `.venv`, `pyvenv.cfg`, `node_modules` or reparse point anywhere inside. Those go to review instead.
6. A size cap per run, for example 20 GB, and a count cap. Anything over a cap waits for the next run or for review.
7. The vault's retention keeps every move restorable for 14 days. The run's notification names what moved, with one-click restore.

## 3. Eval design (the gate an enabling PR must pass)

Precision is the only hard gate. A wrong automatic move is a severity-1 failure; a missed one is not.
- **Fixture generator** (seeded, like the planner eval's builder): N negatives that must never be selected, each one condition away from the boundary. Examples:
  - newest file 13 days old;
  - a live process with the session id in its command line;
  - an unreadable same-user process;
  - a nested repository holding an unpushed commit;
  - a junction to outside data, or a symlink to a file;
  - a locked file;
  - an 8.3 short-name path;
  - an excluded project name (stand-in `zzexclproj`) anywhere in the path;
  - the current session's own scratch;
  - a `.env` file;
  - a directory whose own mtime is old but whose content is new;
  - an over-cap tree.

  Plus positives that must be selected.
- **Sample size from the target, not by feel.** With 0 failures in n independent negatives, the 95% upper bound on the wrong-move rate is about 3/n (rule of three). To claim below 1%, the gate needs at least **300 negatives with 0 selections**; below 0.5% needs 600. Recall is reported with a Wilson interval, but it is not gated.
- **Mutation:** each rule in section 2 is removed in turn, and the gate must fail for each (as with ADR-0043's 26 killed rule mutants).
- **Shadow mode before enabling:** run the selection as a dry run on the weekly schedule for at least 4 weeks on the owner's machine. Log what it would have moved (path hash, size, ages, the rule trace). Then compare with what the owner later deleted, kept or restored by hand. Promotion requires 0 items the owner kept or needed back.
- **Baselines reported beside it:** "everything older than 14 days" (naive) and "never" (status quo), with GB and wrong-move counts for each.

## 4. Not in scope

Worktrees and clones in phase 1. Any non-vault removal. Turning anything on.

## 5. Open questions for the owner

- Is 14 days the right age floor, or should it follow the vault retention?
- Should phase 1 run only in power mode? Safe mode today never moves anything into the vault (ADR-0043).
