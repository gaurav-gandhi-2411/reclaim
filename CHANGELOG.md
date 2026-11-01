# Changelog

All notable changes to Reclaim are recorded here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Numbers in parentheses are pull
requests on [gaurav-gandhi-2411/reclaim](https://github.com/gaurav-gandhi-2411/reclaim/pulls).
Entries for 1.0.0 to 1.3.0 are condensed from each release's GitHub release notes.

Reclaim is unsigned: Windows SmartScreen and some antivirus engines may warn on the installer
(see the README). Safe mode (Recycle Bin only, review-only, riskiest categories off) is the
default for every fresh install in every release below. One exception from 1.4.0: the one-click
clean and the weekly cache clean (ADR-0034) run each tool's own cleaning command on the
regenerable-cache list, in safe mode, without a review step and not through the Recycle Bin.

## [1.4.0] - unreleased

1.4.0 is everything merged to `main` since v1.3.0, including PR #199 (merged 1 Nov 2026:
agent leftovers, move to another drive, the assistant planner and read-only tools, and the
fail-closed exclusion walk of #190). The version number is bumped on the release-prep branch;
nothing is tagged or published yet.

### Added

- **Agent Leftovers** (#199, ADR-0043, `docs/specs/agent-artifacts.md`): finished git worktrees,
  throwaway clones and agent scratch folders, offered only when proven finished (all work pushed,
  nothing hidden, no live process, not a protected project). Off by default and always off in
  safe mode. Every action is a move into the restorable vault, never a delete.
  `reclaim agent-artifacts` is a read-only dry run.
- **Move to Another Drive** (#199, ADR-0044, `docs/specs/relocate.md`): moves a large folder (for
  example a model cache) to another fixed drive behind an NTFS junction. It copies, freezes,
  delta-copies, compares every file by SHA-256, then swaps in the junction; the old copy is kept
  until a separate, confirmed `delete-old`. `reclaim relocate list|plan|move|status|rollback|
  recover|delete-old` and a dashboard page. Docker and WSL are listed as guided steps only:
  sized, never moved by Reclaim.
- **Use Reclaim from your AI assistant** (#199, `docs/specs/assistant-mcp.md`): `plan_free_space`
  plus read-only tools (`get_disk_status`, `list_exclusions`, `explain_disk_usage`,
  `find_unused_models`, `list_relocation_options`) over the local MCP server, a Settings panel
  with copy-ready setup text, and approval cards in Reclaim's own window (#163, #164): an
  assistant cannot name a path, and every assistant-initiated clean is reversible and needs the
  user's click.
- **One-click clean of provably regenerable data** in safe mode (#113, ADR-0034), and an opt-in
  weekly auto-clean task with CLI, Settings toggle and toast (#116, #133, #137, #138), plus an
  opt-in pytest temp category (#139).
- **User exclusion list** honoured by every delete path, with `excluded_applied` reported (#129).
- **Background duplicate warm-up** after a full scan, cancellable and low priority (#134,
  ADR-0040); a typed 409 when a cache reader is cold or stale (#127).
- **Scan index on another volume** via `[storage] index_dir` (#174, ADR-0041); `reclaim
  index-prune` (#130); opaque dependency trees (`node_modules`, `.venv`, `.git`, ...) are
  scanned as one entry with a total (#177, ADR-0042).
- **Disk-space (80%) notification** and its Settings toggle (#38, #97), an **audit log** for
  scan-initiation events (#92), and a **notifications AUMID of its own** so toasts are not
  dropped (#158).
- **Bundled AI layer in the installer** as pinned, hash-checked ONNX models, no separate pip step
  (#30, ADR-0030); an optional Anthropic-key category explainer (#37); the first MCP control
  surface (#39).
- **Assistant-requested relocations** (#230): an assistant may plan and request a Move to
  Another Drive over MCP (`plan_relocation`, `request_relocation`, `relocation_status`;
  ids only, never a path), but the move runs only after the user's click on a relocate
  approval card, after a live re-plan with a matching plan hash. `delete-old` and
  `rollback` are never reachable over MCP. Every terminal state is audit-logged.
- **What changed on disk** (#233, ADR-0046): a read-only growth history (a bounded walk
  every 4 h into `data/growth.sqlite3`), a "What changed" panel in Advanced mode, a
  one-line weekly summary in Simple mode ("C: grew 6.2 GB this week, mostly Docker"),
  and an anomaly toast. Excluded projects appear only as one "Excluded projects" total.
  `[growth] enabled = false` stops all growth IO.
- A generated ADR index checked by a test (#210), ADR-0045 (#208).
- Release prep: this changelog, a security policy, issue and PR templates, a release checklist.

### Changed

- **Performance** (each measured in its PR; see the PR for the number and the machine):
  prefix-scoped index queries (#117), scanner reads IDs, sizes and times from the NTFS directory
  listing (#118, its Lever 2; Lever 1 was not shipped), bounded parallel dedup hashing (#125),
  memoised environment-root probes (#124), `/api/summary` without materialising every row
  (#152, #160), path-scoped apply from the warm cache (#121), exclusion patterns compiled once
  (#187), bounded index WAL (#103, #151), and the scanner asks git whether a repo is clean only
  under `node_modules`, the one place a verdict reads it (#231).
- Bytes are reported as **freed** only when really released; Recycle Bin and vault moves are
  reported as **moved** (#115, #48, #54).
- Default scan scope is the invoking user's own profile, not a volume-root traversal (#47); a
  whole-drive or outside-home scan needs a session-minted, 60-second confirmation (#65, #66,
  #70, #71).
- Installer: re-registers the weekly task on every install and upgrade (#138), keeps
  `config.toml` across uninstall (#162), per-account task names (#91), terminates a running
  Reclaim before removing files (#34). Build: Nuitka pinned to 4.2.2 (#112), one consistent
  VC++ runtime set with a DLL-closure gate (#108), numpy/scipy test suites excluded by an
  allow-list (#106), the build's source commit recorded in a sidecar (#73), heavy jobs run as
  detached scheduled tasks with a watchdog (#220, #223).
- Dependencies: pyjwt 2.15.1 (#119), anyio 4.15.1 (#101), mcp 1.30.0 for CVE-2026-59951 (#175),
  certifi 2026.10.19 for PYSEC-2026-4320 (#185).

### Fixed

- Apply skips a file whose size or mtime changed since the scan and re-stats before every
  regenerable unlink (#122, ADR-0036); identity re-verification at mutation time (#45); a
  pre-flight check for live processes and hardlink-shared active installs (#36); an ownership
  and scope check at the apply choke point (#57).
- Temp roots are aged by their newest contents, not their own mtime (#110, #46); detectors that
  found nothing when the scan root was the temp directory (#76, #81); hardlink-aware reclaimable
  bytes (#44).
- Scanner: unreadable directories are surfaced, not dropped (#41); a completed rescan no longer
  prunes unlistable subtrees (#130); settings that hide work no longer make a repo read clean
  (#189); the per-repo git status is strictly read-only (#186); only the attribute bits Reclaim
  reads are recorded (#170).
- Task Scheduler: registration failures are captured (#88), the disk-space task now really
  registers (#83), uninstall no longer deletes another install's task (#100), `auto-clean
  --json` always writes exactly one JSON document (#137).
- Dashboard: the `hidden` attribute always wins over an empty recovery banner (#165); completed
  scan roots persist so volume reconciliation survives a restart (#205); the console log handler
  follows the current `sys.stderr` (#206); a present-but-unloadable optional package is reported
  as a load failure (#109); onnxruntime was dead in the frozen build (#108).
- Agent Leftovers: applying a worktree no longer refuses itself because the apply process's
  own command line names the path (rule W5 counted it as a live use; fail-closed, so
  nothing unsafe, but every apply root reported 0/0) (#236).
- Test hermeticity: tests redirect every profile root and refuse destructive operations on the
  real profile (#140, #143, #145). The frozen-build smoke suite follows the cold-cache contract,
  passes its own `--config`, and checks the per-account task name (#235).

### Security

- **The exclusion subtree walk fails closed** (#190): a directory it cannot list, an entry whose
  type it cannot read, an unresolvable junction, or too many junction-reached listings now
  answers "protected" (`<unreadable directory: ...>`) instead of "nothing excluded". Paths
  deeper than MAX_PATH are now seen, and junction cycles terminate.
- Closed gaps in scan scoping and confirmation tokens (#65, #66, #70, #71), and in assistant
  deletes: always reversible (#163), approved by a click in Reclaim's own window (#164).
- Dependency advisories cleared (see Changed).
- **No git inside an excluded project** (#239): the scanner, apply re-verification, AI
  filtering and Agent Leftovers start no git process in a repo that is, is inside, or
  contains an excluded project, including through a junction, a `.git` pointer,
  `commondir`, `alternates`, `config.worktree` or a submodule git dir. Such a repo reads
  not-clean. `reclaim scan` now reads `config.toml` (new `--config`) for exclusions and
  refuses when it cannot be read.

### Known limits

These are stated in the PRs and ADRs; none is a silent regression.

- #190: the junction budget counts listings, not resolutions, so a crafted dense junction mesh
  takes minutes before failing closed (the old walk was unbounded on it); very large pnpm trees
  (about 90k junction-reached listings) fail closed, meaning they are never offered, when a
  path-keyed exclusion pattern is configured (the default config has none); case-sensitive NTFS
  directories could collide in the walk's real-path set (shown by simulation only).
- Move to Another Drive: Docker and WSL are guided only. The relocate screenshots in the repo
  were taken before ADR-0044 addendum 8 and show guided rows at 0 bytes.
- The assistant planner is optimal on 31 of 31 benchmark cases with 0 unsafe selections, but the
  spec's "90% within 5%" target cannot be met while deletes are whole selections (stated in
  #199). Client setup snippets for Claude Desktop, Claude Code and Codex CLI follow each
  client's documentation and were not checked against a live install of every client.
- Agent Leftovers: the "Agent Leftovers" filter had not yet been screenshotted from an installed
  build when #199 was opened.
- The dedup warm-up on a very large profile is still long (the measurement is in
  `docs/HOWTO.md` section 6c); it runs in the background at low priority.
- `uv.lock` records the project version and is regenerated by `uv lock`, not by hand.
- #239: with exclusions configured, each repo whose verdict needs git is first walked in
  full (about 0.6 s per 50,000 entries, measured on a synthetic tree; estimated 6-8 s per
  scan for 11 such repos, not measured on a real profile). A repo with an `[include]` in
  its own git config, or a remote named like an excluded project, reads not-clean.
- #233: the growth store grows about 11.5 MB a year (simulated), against a 5 MB target;
  steady growth of 1.67 GiB/day or more trips the 5 GiB / 72 h volume rule; the
  uninstaller leaves `growth.sqlite3`, as it already leaves `notification_state.json`.
- #230: a short window between the post-click re-plan and the move, which the engine
  re-checks; every MCP tool now refuses undeclared arguments (no affected client found).

## [1.3.0] - 2026-07-25

### Added

- **Simple mode**, the default: one "Clean My Computer" button that scans every fixed drive with a
  live progress bar and a time estimate, then shows plain-language results scoped to
  categorically-safe clutter. Advanced mode is one click away and unchanged (#23, #24).
- **A new visual identity**: indigo and mint palette, elevation, skeleton loaders, a global
  `prefers-reduced-motion` gate, checked for WCAG AA contrast in both themes (#20).
- Third-party license notices bundled and shown in the About panel (#14 to #18).

### Changed

- The AI Suggestions tab states plainly that the installer does not bundle the optional AI layer
  and no longer shows a pip command that does not apply to installer users (#22).

### Fixed

- Nine audit fixes (#14 to #18): clearer port-in-use message, OS-level lock on `manifest.jsonl`
  appends, explicit handling of a full disk mid-scan, a path-alias bypass in the safety validator
  (11 alias forms: UNC, 8.3 names, subst drives, junctions and more), a Unicode NFC/NFD filename
  mismatch, a decompression-bomb cap for Pillow, a friendly message on a malformed `config.toml`,
  and four dead-code findings.

## [1.2.0] - 2026-07-24

Supersedes 1.1.0, whose `apply`, `purge` and `restore` wrote the manifest once after a whole
batch, so a kill mid-batch could orphan a moved file with no record.

### Added

- `reclaim recover` and a dashboard banner (shown only when needed) that reconcile what a crash
  interrupted by checking what is actually on disk (#5).
- Live progress for long operations in the CLI and dashboard, with an up-front time estimate for
  large ones (#10).
- A rotating persistent log file and a "Copy diagnostics" button (#9); schema versioning of
  durable state so a newer release's fields never crash an older install (#6).

### Fixed

- **Crash-safe by design**: every quarantine, restore and purge writes a durable record before and
  after touching a file (#5).
- Long-path (over 260 characters) scanning, including a "N path(s) couldn't be read" note, and the
  same gap in the "is this file inside a git repository" check (#8).

### Security

- CLIP and sentence-embedding model weights pinned to an exact revision and checked against a
  known-good hash before use (#7).

## [1.1.0] - 2026-07-23 (superseded by 1.2.0)

### Added

- **Quick Clean**: one confirmation for every categorically-safe group (dev-tool caches, package
  caches, temp and browser caches, crash dumps), each with its measured size and a plain-language
  reason; suggested-folder buttons.
- **AI Suggestions**, recommend-only and wired into the dashboard for the first time (#2, ADR-0025):
  near-duplicate photos, document version chains, screenshot bursts and semantic photo grouping.
  Semantic groups and ambiguous screenshot bursts have no select or delete affordance at all.

### Fixed

- The logo failed to render everywhere in the dashboard (an invalid `--` in an SVG comment) (#4).

### Known issue

- `apply`, `purge` and `restore` could orphan a moved file if killed mid-batch (fixed in 1.2.0).

## [1.0.0] - 2026-07-23

First public installer: per-user, no admin prompt, unsigned.

### Added

- Rules-first scanner and detectors for provably-safe categories behind a hard safety gate that
  runs before any candidate is generated.
- **Safe mode** as a structural default: Recycle Bin only, review-only, `duplicates`,
  `model_caches` and `dev_artifacts` off. Power mode is a typed, logged opt-in
  (`reclaim mode power --confirm ...`); going back to safe mode needs no confirmation.
- A localhost-only dashboard with CSRF and DNS-rebinding defences, vault and manifest with
  restore, and a core-only installer (no AI dependencies bundled).
