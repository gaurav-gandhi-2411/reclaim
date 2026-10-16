# ADR-0041: `[storage] index_dir` -- the scan index may live on another volume

Status: accepted (2026-10-16)

## Context

The scan index (`reclaim_index.sqlite3`) is the one large file Reclaim writes: 11 GB for a
12.8M-entry scan of `C:\Users\gaura` (13.8 GB once the dedup warm-up has persisted hashes), on a
volume that was 97% full. The data folder was hard-wired to the install directory
(`app_paths.data_root()`), so the only way to put the index elsewhere was a per-command `--db`,
which the scheduled tasks and the dashboard do not pass. A junction from `data\` to another volume
was rejected: the uninstaller's `DelTree` would follow it.

## Decision

* New config section `[storage]` with `index_dir` (absolute path, no `..`; empty = the old
  `data/` folder).
* The location is decided in exactly one function, `index_location.resolve_index_db`, called once
  from `cli.main` for every subcommand that has `--db` (scan, index-prune, reconcile, apply, undo,
  purge, serve, dashboard, mcp-serve). Order: explicit `--db`, then `[storage] index_dir`, then the
  legacy default. `check-disk-space` and `auto-clean` (so both scheduled tasks) never open the
  index and have no `--db`; a test fails if one gains it without being resolved.
* The approval channel file is defined as "next to the index" (`approvals.channel_file_path`), so
  it follows the index; `serve` and `mcp-serve` resolve the same path and therefore still meet.
* The quarantine vault is NOT moved: it stays on the volume of the files it holds, so quarantining
  stays a same-volume rename.
* Fail closed: a config that exists but cannot be parsed or has an invalid `index_dir` raises
  (commands with their own config handling report it in their own words; scan/reconcile/index-prune
  exit 2). Nothing falls back silently to the legacy index. If `index_dir` is set, has no index and
  a legacy index exists, a warning says the index was not moved.

## Consequences

* Moving an existing index is manual: stop the dashboard/MCP process, copy
  `reclaim_index.sqlite3` (checkpoint the `-wal` first), verify, set `index_dir`, then delete the
  old copy. Not automated here because the move is a delete-after-copy of a multi-GB file.
* The uninstaller's "delete data folder" does not know about `index_dir`; an index on another
  volume survives uninstall and is deleted by hand.
* A config.toml containing `[storage]` is rejected by older builds (unknown top-level key, the
  ADR-0027 hard reject): downgrading requires removing the section.
* The `--config` default is anchored like the data folder (`data_root()/config.toml`: the executable's
  directory when frozen, plain `config.toml` in dev/test), so every command reads the same config
  whatever the launch cwd. Before this change the default was cwd-relative for commands that have
  `--config`. A task with a different cwd and no `--config` therefore now reads the installed
  config (exclusions, `[autoclean]`) instead of built-in defaults.
* An explicit `--config` that is not an existing file exits 2 for commands that resolve the index
  (a typo must not fall back to the legacy index). `check-disk-space` and `auto-clean` never open
  the index and keep their pre-existing tolerance of a missing config.
* `index_dir` must be a directory: a value ending in `.sqlite3`/`.sqlite`/`.db` is rejected.

## Alternatives

* Junction `data\` to D: -- uninstaller and `DelTree` hazard.
* Environment variable -- invisible to scheduled tasks and the protocol handler.
* Move the vault too -- breaks the same-volume-rename property.
