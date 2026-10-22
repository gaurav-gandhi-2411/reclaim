# Reclaim — Autonomous Run Spec (start → 27 Oct 2026)

**Owner:** GG — unreachable until 27 Oct 2026.
**Executor:** Claude Code (orchestrator) delegating to executor + verifier subagents at `C:\Users\gaura\.claude\agents\`.
**Authority:** this spec is the single source of authority for the run. Where it conflicts with older docs, this spec wins — **except** CLAUDE.md safety rules, the merge-gate hook, and the hard exclusions, which nothing in this spec overrides.

---

## 0. Goal

Keep improving Reclaim continuously without GG: finish in-flight work, ship it to GG's installed app, then build **Relocate**, then **AI-assistant integration**, then **hardening**. Never sit idle, never wait on GG, never compromise data safety.

---

## 1. Grants from GG (decided before the run)

| # | Grant |
|---|---|
| G1 | **Merge authority:** CC may merge any PR the merge-gate hook allows, but only after it passes the **stricter bar** in §3 — for every PR during this run, including delete-path, exclusion and uninstaller changes. |
| G2 | **Agent-artifacts apply on the real machine:** allowed for candidates passing every rule, moved to the vault (restorable), under the procedure in Phase A6. |
| G3 | **Install / upgrade / roll back** Reclaim on GG's account (`gaura`). |
| G4 | **Priority order:** A finish in-flight → B Relocate → C AI-assistant integration → D hardening → E maintenance. |
| G5 | **Git identity:** repo-local `user.email = gaurav.gandhi2411@gmail.com` (CLAUDE.md rule 8). Never rewrite history. |

**Not granted (log to WHEN GG HAS TIME instead):** admin/UAC actions; editing `merge_gate.py` or any hook; Windows settings (pagefile, power, drivers, updates); real-machine **relocation** (dry-run only); editing Claude Desktop / Codex config files on GG's machine; paid APIs or paid services; publishing anything externally (posts, issue replies, public GitHub releases or announcements); touching other projects' files except through Reclaim's own product rules (G2).

---

## 2. Hard rules (never break)

1. **Hard exclusions:** never touch `fr-en-transformer`, `shipdoc-extract`, `intent-router` — data, caches, worktrees, models, processes, pytest dirs, git. Exclusion is decided **from the path before any process or git call**. Only exception (granted earlier): their finished sessions' scratch under `%TEMP%\claude` with no live process and 7+ days idle by newest content.
2. **Live work is untouchable:** never touch any path a live process uses (cwd, open handle, command line). If a process can't be inspected, treat the path as live.
3. **Check-then-delete:** every deletion is a separate command after the check's output has been read and reported. An empty check result counts as a failed check.
4. **Read-only git is truly read-only:** every read-only git call uses `--no-optional-locks` (or `GIT_OPTIONAL_LOCKS=0`) so it can never rewrite `.git/index`.
5. **Hermetic tests:** tests never touch the real profile (root `conftest.py` + `safety_env` guard). Verifier probes redirect every profile root.
6. **No bypasses:** if a hook, the permission classifier, or the merge gate refuses something, do not work around it, rename to evade it, or edit it. Log it and move on.
7. **Sequential work:** one task and one subagent at a time. Heavy steps (build, full scan, warm-up, soak, mutation run) never overlap.
8. **Resources:** keep the 8 GB free-RAM build floor (never lower). If C: < 20 GB free, no installs or scans until it recovers.
9. **Disk hygiene:** remove each worktree once its PR merges; delete your own scratch and index copies when done (index copies only on D:); report disk footprint at every checkpoint.
10. **No paid APIs/services.** Local models, Claude via the Max plan (Claude Code itself), or free tiers only.
11. **Evidence:** tag every claim VERIFIED (ran it / read the code) or BELIEVED (inferred). Numbers over adjectives.
12. **Force-push** only `--force-with-lease`, only on your own PR branches.

---

## 3. Merge bar for this run (stricter than normal)

A PR may be merged only when **all** hold, recorded in the MERGE LOG with evidence:

1. Rebased on current `origin/main` in the same task that merges it.
2. All required CI checks green on the exact head. One re-run allowed for a **documented** known flake; a second failure is treated as real.
3. Full `scripts/verify.py` green on the exact head (not a filtered subset).
4. **Two independent verifier passes** (fresh subagents, falsify-don't-confirm mandate). Every finding fixed, or explicitly accepted with reasoning — never accept a safety finding.
5. **Mutation tests** on changed safety-relevant code: every mutant killed, or the survivor justified in writing.
6. **Installed-build smoke** for any change to shipped code: build, DLL closure, both smoke tests, in-place upgrade on GG's account, settings survive, one scan + warm-up + `/api/summary`, no new errors in logs. (May be batched: one build covering several PRs, as long as each PR's head is in that build.)
7. **Extra for delete-path, exclusion, uninstaller, relocation and agent-artifacts changes:** a third verifier pass focused only on data safety, plus a real-machine **dry run** showing no candidate in an excluded, live, unpushed or dirty class.
8. After merge, `main` CI must go green. If it goes red and it isn't a documented flake, **revert in the same task**.

**When the hook refuses a merge** (size, branch prefix, sensitive path): don't edit or bypass it. If the work splits honestly into coherent gate-passing PRs (one real change per PR — not mechanical slicing to dodge review), split it. Otherwise leave the PR open, include it in `integration/run-2026-10` (= main + every bar-passing but hook-blocked PR), install from that branch so GG's machine still gets the work, and list the PR in the review pack for GG to merge.

---

## 4. Shipping to GG's machine

- Build on D: (build dir, Nuitka cache, TEMP, uv cache), watchdog guarding D:, RAM-wait launcher with the 8 GB floor.
- Before every upgrade, keep the previous installer at `D:\reclaim-build\installers\<sha>\`.
- Upgrade in place. Verify config.toml (byte-identical or expected diff), exclusions, `index_dir`, both scheduled tasks, notification identity, protocol handler.
- Then: scan + warm-up + first `/api/summary` timing; Overview loads; no new errors in logs.
- **Regression on the installed build → roll back** to the previous installer, write an incident note, fix forward on a branch.
- At most **one install per day**, at phase milestones, never mid-task.

---

## 5. Work queue

### Phase A — finish in-flight work (PR #182)

- **A1** Fix the 3 CI failures: tests must set the vault on the same volume as the test tree explicitly; capture git's stderr for the local-push failure and fix the root cause (no skip, no xfail without a real reason).
- **A2** Enforce `--no-optional-locks` on every read-only git call (agent-artifacts and anywhere else). Test: a read-only classification leaves `.git/index` mtime and bytes unchanged.
- **A3** Second independent review of the agent-artifacts fixes; full verify on the final head; mutation run.
- **A4** Merge per §3 (split if honest; otherwise integration branch + review pack).
- **A5** Build 11, upgrade in place, measure warm-up and first `/api/summary`.
- **A6** Agent-artifacts on the real machine (G2):
  1. Re-run the **dry run on the final installed code**. Save the full list (proposed and kept, with reasons) to the review pack.
  2. Set vault retention for these batches to **≥ 21 days** (no purge before 17 Nov 2026) and confirm no scheduled purge can remove them earlier.
  3. **Wave 1:** apply the 2 smallest candidates. Restore one, verify it's intact (worktree: `git worktree repair` + `git status` clean; scratch: file count and bytes match), then re-apply it.
  4. **Wave 2:** apply the rest — re-checking every rule at apply time.
  5. Report per item: path, size, reason, vault path, exact restore command, C: free before/after.
  6. Any unexpected result → stop applying, restore, incident note (see §7).

**Exit:** #182's work merged (or on the integration branch and installed), agent-artifacts applied with restore proven, review pack updated.

### Phase B — Relocate to another drive (spec: `docs/specs/relocate.md`)

Build it as an in-app feature, **review tier** (user clicks).

- Detect target volumes with space; propose candidates; classify each as **movable** or **must-stay** with reasons. Must-stay: uv cache, conda pkgs (hardlinked into envs), venvs, anything with cross-directory hardlinks, excluded projects, anything with a live handle.
- Move procedure: free-space check on target with margin → copy with per-file hash → freeze + re-sync → preserve symlinks/reparse points → junction swap → keep the source as `<name>.reclaim-moved` until the user confirms or 7 days pass → delete only after a final re-hash.
- Long paths: this machine has `LongPathsEnabled=0` — test paths > 260 chars explicitly.
- Interruption safety: kill mid-copy and mid-swap → state is either fully rolled back or cleanly resumable. One rollback command restores the original.
- MCP requests go through the approval card; reversible only. Audit log for every step. UI page with per-candidate size, reasons, and status.
- **Eval:** fixtures for symlinks, hardlinks, long paths, open handles, read-only files, ACL-denied files, interruption, insufficient target space, excluded-project paths. **Zero data loss is a hard gate.** Mutation tests on the move/verify/swap code.
- **Real machine: dry-run only.** Put the candidate list (sizes, movable/must-stay, reasons) in the review pack.

### Phase C — AI-assistant integration (spec: `docs/specs/assistant-mcp.md`)

- Package the MCP server for **Claude Desktop** and **Codex CLI** (local stdio). Settings page shows the exact config snippets with a copy button; optional "register" buttons edit the client config only on explicit user click, with a backup of the original. **Do not edit either client's config on GG's machine during this run.**
- Planner tools, all deterministic: `explain_disk_usage`, `plan_free_space(goal_gb, constraints)` (keep-projects, keep-categories, reversible-only), `find_unused_models` (HF repos not referenced by any project file; read-only).
- Invariants: no free-form delete paths; approval card for every delete/move; reversible only; exclusions win; rate limits.
- **Eval:** tool-contract tests with known-correct plans — measure over-/under-inclusion; any constraint violation or unsafe selection is a hard zero. End-to-end smoke using **Claude Code itself (Max plan) as the MCP client** against a fixture profile. No paid APIs.
- Docs: HOWTO section "Use Reclaim from Claude / Codex".

### Phase D — hardening backlog (in order)

- **D1** `reconciliation_volume` is null in the summary — root-cause and fix.
- **D2** Duplicate case-insensitive path index (`idx_files_path_lower` vs the unique autoindex): migration with ADR, measured savings, works on old indexes.
- **D3** Scanner-listing tests on this Windows build (attribute bit) — confirm fixed by #170, or fix.
- **D4** Flaky tests (cold-start budget, CI throughput floor, `-n 4` load failures): make deterministic or correctly scoped. No blind retries.
- **D5** Remaining warm-up costs after the speedup: profile, fix the small ones.
- **D6** Docs: HOWTO current, README screenshots current, ADR index.
- **D7** Agent-artifacts automatic tier: plan + eval design only, not enabled.
- **D8** Docker / WSL relocation via the product: plan only.

### Phase E — queue empty

2-hour soak on the installed build; full verify; docs; prune your own worktrees/scratch; keep the review pack current. **Do not invent features beyond this spec.**

---

## 6. Decision framework

When something is unclear:

1. **Covered by a grant or rule here?** Apply it.
2. **Otherwise decide:** tie-breakers in order — data safety/correctness → product quality → AI/ML rigor → operability → maintainability → scalability → dev productivity → cost. Pick the most reversible option. Record it in the DECISIONS log (what, options, choice, reasoning).
3. **Needs GG** (admin/UAC, physical presence, irreversible *and* ambiguous, policy change, money, external publishing): add to WHEN GG HAS TIME with exact numbered steps, then **move to the next task that doesn't depend on it.** Never wait idle.

**Default-safe answers:** unsure whether deletable → keep. Unsure whether live → treat as live. Unsure about compatibility → stay backward compatible. Unsure whether a failure is a flake → treat it as real until proven otherwise.

---

## 7. Scenario playbook

| Scenario | Action |
|---|---|
| Context compaction or a new session | Re-read this spec, `docs/RESUME.md`, the run log and the review pack before acting. |
| Session dies / laptop reboots | If auto-resume (§9) is set up, it restarts; otherwise work stops until GG returns. Checkpoints guarantee nothing is lost. |
| Low RAM (e.g. Ollama loaded) | RAM-wait for builds; meanwhile do non-heavy tasks (docs, tests, reviews, planning). |
| C: < 20 GB free | Stop installs and scans. Run only Reclaim's own safe cleanup (tool caches via native commands). Never touch other sessions' files. Log it. |
| Heat / GPU crash / thermal throttling | One heavy step at a time; add cool-down pauses between heavy steps; if a crash recurs, halve heavy-work frequency and log it. |
| CI flake | One re-run if documented; second failure = real. |
| GitHub / DNS outage | Retry with backoff; keep working locally; push when it recovers. |
| Permission classifier or hook denial | Don't work around it. Log, switch tasks. |
| Verifier finds a defect | Fix and re-verify. If not fixable reasonably, hold the PR (don't merge), log, move on. |
| Installed build regresses | Roll back to the previous installer; incident note; fix forward on a branch. |
| **Unexpected deletion / data-safety incident** | **STOP all deleting, moving and applying work.** Restore from the vault, write an incident report at the top of RESUME.md and the review pack, continue only non-destructive tasks for the rest of the run. |
| Excluded project touched | Stop that task; incident note; fix the ordering so exclusion is decided first. |
| Other sessions busy (CPU/disk/RAM) | Never touch them; schedule heavy work around them. |
| Queue finished early | Phase D, then E. |

---

## 8. Checkpointing & reporting

- **After every completed task:** update `docs/RESUME.md` (top section) and `docs/run-log-2026-10.md`. Push at least every 2 hours via a docs PR (self-merge if the gate allows; otherwise keep it on the branch and push).
- **Daily summary** appended to the run log once per day: shipped, merged, installed, decisions, incidents, disk footprint (C:, D:, Reclaim install/index/vault/logs).
- **Review pack** `docs/REVIEW-2026-10-27.md`, kept current throughout:
  1. What shipped (per PR: what, why, evidence, merge SHA).
  2. Open PRs and `integration/run-2026-10` contents needing GG's merge, with exact steps.
  3. Installs done (installer SHAs, rollback path).
  4. Agent-artifacts applied — full list with restore commands.
  5. Relocate dry-run candidates with sizes.
  6. DECISIONS log.
  7. Incidents.
  8. WHEN GG HAS TIME list (exact numbered steps).
  9. Disk footprint and next steps.
- **By 27 Oct 2026 09:00 IST** the review pack is complete. After that, only low-risk Phase D/E tasks, keeping the pack current.

---

## 9. Resilience — auto-resume (task R0, optional)

At the start of the run, try to set up auto-resume so a reboot doesn't end the run:

- A **per-user** scheduled task (no admin), triggered at logon with a 5-minute delay, that relaunches Claude Code in `C:\Users\gaura\ml-projects\reclaim` **only if** no reclaim Claude Code session is running, continuing the latest conversation with an instruction to re-read this spec and RESUME.md.
- Verify the exact CLI invocation against `claude --help` / docs and test it once. It must expire automatically after 27 Oct 2026 (trigger end date) and log each launch.
- If it can't be verified to work reliably and safely, **skip it** and note why. Never run it elevated.
- Note: if Windows requires a password after a reboot, nothing resumes until GG logs in.

---

## 10. Start-of-run checklist

1. Copy this spec into the repo at `docs/specs/autonomous-run-2026-10.md` (docs PR).
2. State check: `main == origin/main`, CI green on HEAD, open PRs (#182), worktrees, nothing of a previous session running, C:/D: free, free RAM, pagefile location, installed build version.
3. Create `docs/run-log-2026-10.md` and `docs/REVIEW-2026-10-27.md` skeletons.
4. R0 (auto-resume), then Phase A.
