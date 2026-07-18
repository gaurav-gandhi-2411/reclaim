# 0009. Standalone Python installation exclusion from exact_duplicate

## Context

The real, live `exact_duplicate` apply this ADR follows (retention_days=0, method=recycle_bin,
10,247 candidates selected, 10,134 succeeded) broke this project's own development environment
within minutes of completing: `.venv\Scripts\python -c "import socket"` failed with
`ModuleNotFoundError: No module named 'socket'`, cascading into `asyncio` and everything that
imports it.

Root cause: `C:\Users\dev\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none\
Lib\socket.py` — a shared, uv-managed Python 3.12.12 build that this project's `.venv` (and
likely others on the machine) is provisioned from — was byte-identical to
`anaconda3\envs\tes-cleanroom-080\Lib\socket.py` (Python stdlib files are identical across same-
version installs by construction) and got selected as an `exact_duplicate` candidate, with the
conda env's copy kept. ADR-0008's cross-environment protection never fired: `_environment_root`
recognized conda environments (`conda-meta/`) and venvs (`pyvenv.cfg`), but a uv-managed
standalone Python *build* has neither marker — it isn't a venv (nothing ran `python -m venv`
against it) and isn't a conda environment. `_environment_root` returned `None` for it, so
`_is_cross_environment_duplicate` short-circuited to `False` immediately (per its own documented
behavior: "returns `False` immediately if duplicate's own environment root is `None`"), and the
file was treated as an ordinary, safely-deletable duplicate.

**First response, and why it was incomplete.** A keyword scan of the batch's manifest for known
shared-toolchain path segments (`\uv\python\`, `.rustup`, `.cargo`, `nvm`, `.dotnet`, `.nuget`,
`pyenv`, `scoop`, `.rbenv`, Go's module cache) found 71 files under the uv-managed build and
nothing else, so the first recovery pass restored exactly those 71 and stopped there. That scan
was itself a version of the same mistake this whole ADR is about: a keyword list, like a
`Lib/site-packages`-only path check, is only as complete as the tools someone thought to name in
advance. A second, structural pass — re-running the JUST-FIXED `_environment_root` (with the new
`python.exe`+`Lib/` marker) against every one of the batch's 10,134 manifest entries, and
flagging any entry where the deleted file's environment root differed from its recorded keep
copy's environment root — found **186 true cross-environment violations**, not 71:

| shared install (deleted-from) | files | evidence it's "shared, load-bearing infrastructure" |
|---|---|---|
| `~/AppData/Roaming/uv/python/cpython-3.12.12-windows-x86_64-none/` | 71 | this project's own `.venv` (and others) provision from it |
| `~/AppData/Local/Google/Cloud SDK/google-cloud-sdk/platform/bundledpython/` | 108 | `gcloud` CLI's bundled interpreter |
| `~/sdks/android-sdk/ndk/28.2.13676358/toolchains/llvm/prebuilt/windows-x86_64/python3/` | 7 | Android NDK's bundled build-toolchain Python |

The keep-side survivors for these 186 were spread across many different named conda
environments, `C:\Python314\`, and at least seven different projects' own `.venv` directories —
none of those survivors were themselves at risk (they were never proposed for deletion), but
none of them make the DELETED shared-install copy any less load-bearing for whatever depends on
that install specifically continuing to have its own files in place.

## Recovery

Because the apply used `method=recycle_bin` (this project's own earlier design choice, made
specifically because "the surviving twin is the backup; Recycle Bin is the net"), all 186 files
were still physically present in `C:\$Recycle.Bin\<user-SID>\`, unpurged. Recovered in two
passes — the 71-file first pass (keyword-scan-driven), then a 115-file second pass (systematic-
audit-driven) once the true scope was found:

1. Parsing every `$I*` index file in the Recycle Bin's `$Recycle.Bin` structure (binary format:
   an 8-byte version header, 8-byte original size, 8-byte deletion `FILETIME`, 4-byte path
   length in UTF-16 characters, then the original path as UTF-16LE) to map each Recycle Bin
   entry back to the original path it came from — this project's own `undo` command explicitly
   does NOT support `recycle_bin`-method restoration (`RecycleBinRestoreUnsupportedError` by
   design), so this had to be done directly against the Recycle Bin's on-disk structure.
2. Matching target paths (read from the batch's own manifest entries) against the parsed index,
   confirming a live `$R*` data file existed for each match (it did, for all 186 across both
   passes — zero missing), then dry-running the restore (print-only) before executing it for
   real, each time.
3. Moving each `$R*` data file back to its original path and removing the consumed `$I`/`$R`
   pair, restoring every affected shared installation to its pre-apply state.
4. Verifying recovery after each pass: `import socket, asyncio, mailbox, _pydecimal, imaplib,
   msilib.schema` succeeds again, this project's own full test suite (285 tests) passes clean,
   and — after the second pass — a direct filesystem check confirms all 186 previously-missing
   paths exist on disk again with zero exceptions.

This is the concrete, load-bearing payoff of choosing `recycle_bin` over `direct_delete` for this
apply: the mistake was real, it was larger than the first pass realized, and it was still fully
recoverable both times. A `retention_days=None` category (direct permanent delete) would have
made this unrecoverable — and a keyword-scan-only response would have left 115 of the 186 broken
files undiscovered indefinitely.

## Decision

Extend `_environment_root`'s marker detection with a third, tool-agnostic signal: a directory
containing `python.exe` or `pythonw.exe` directly, alongside a `Lib/` subdirectory, is a complete
standalone CPython installation root — regardless of whether conda, `venv`, uv, or a plain
Windows installer produced it. This is the general filesystem signature every one of those tools'
installations shares structurally, unlike `conda-meta/` (conda-specific) or `pyvenv.cfg`
(venv-specific). Checked in the same ancestor walk-up as the other two markers, same
short-circuit-before-`pkgs/`-check ordering, same cost profile (bounded `is_dir`/`is_file` calls
per candidate, never a whole-disk walk).

## Consequences

- **Verified against the real incident path.** `_environment_root(Path(r"C:\Users\dev\
  AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none\Lib\socket.py"))` now returns
  the install root instead of `None` — the exact case that broke.
- **This is now the third correction to `_environment_root`/`_is_cross_environment_duplicate`
  within the ADR-0008/0009 arc** (Lib/site-packages-only → marker-based walk-up →
  asymmetric-vs-cache → this). Each one was found by actually running the pipeline against real
  disk state, not by more code review of the same function in isolation — the pattern across all
  three is that a plausible-looking heuristic missed a real filesystem layout until real data
  exercised it.
- **The `exact_duplicate` apply this ADR follows is NOT re-run as part of this ADR.** The 9,948
  files that succeeded and were NOT part of the 186-file incident remain in the Recycle Bin,
  unaffected by this fix or by the recovery. Whether/when to empty the Recycle Bin (the only way
  this apply's reclaimable estimate becomes real freed disk space) is the user's call, not
  automated by this tool.
- **Scope boundary, stated honestly:** this closes the specific gap found (shared interpreter
  builds with a `python.exe`+`Lib/` signature) — verified not just against the one incident path
  but by re-running the fixed detector against the ENTIRE applied batch (10,134 entries) and
  confirming zero remaining unexplained cross-environment violations after both recovery passes.
  It does not claim to have enumerated every possible "shared toolchain infrastructure" pattern
  that could exist on some other machine, or on this one in a category `exact_duplicate` hasn't
  touched yet — the keyword scan in this ADR's Context section already proved that kind of
  enumeration is exactly the failure mode this fix replaces with a structural test.
- **Residual risk, found on later review and closed by ADR-0010 — stated plainly, not
  glossed over.** This ADR's own fix (`python.exe`/`pythonw.exe` directly in the root, alongside
  `Lib/`) is itself still marker-dependent in one sense: it assumes the interpreter binary sits
  at the environment's own root. That assumption holds for every install this incident actually
  touched (conda base/env, the uv-managed build, `gcloud`'s bundled Python, the Android NDK's
  toolchain Python — confirmed against each one's real directory listing), but it does NOT hold
  for the standard Windows `venv` layout, which puts the interpreter in `Scripts/`, not the venv
  root — confirmed against this very project's own `.venv`, which has no `python.exe` at its
  root at all. This project's `.venv` was never actually at risk (its real `pyvenv.cfg` already
  protected it, both before and after this ADR), but the finding stands on its own: **marker-file
  detection is not, and was never claimed to be, a complete defense** — `exact_duplicate` cannot
  be proven safe against an arbitrary embedded Python or runtime environment whose marker file is
  missing, corrupted, or simply never written by a tool this codebase knows to check for. ADR-0010
  replaces marker-only detection with structure-first detection (interpreter binary, `Scripts/`/
  `bin/` directory, or `site-packages` — any one sufficient) as the new default, specifically to
  stop depending on tools remembering to leave a marker behind at all.

## Alternatives considered

1. **Add a uv-specific marker check (e.g. a literal `uv\python\` path-segment check, mirroring
   the HF-cache structural check in ADR-0008).** Rejected: narrower than necessary, and brittle
   to uv changing its own storage layout. The `python.exe`+`Lib/` signature is a property of
   *being a Python installation*, independent of which tool created it — it would have caught
   this exact incident even if uv stored its builds somewhere entirely different, and would also
   catch a manually-downloaded, unmanaged standalone Python distribution (like `C:\Python314\`,
   which this machine already has, currently unprotected except by file permissions).
2. **Treat every unrecognized directory as "possibly an environment" and refuse to propose any
   duplicate unless BOTH sides are confirmed non-environment.** Rejected: this would make
   `exact_duplicate` effectively useless (nearly every deep, nested duplicate would be silently
   excluded) — the goal is a precise, structural test for "this really is an interpreter
   installation," not a blanket retreat from the feature.
3. **Rely on Windows file permissions (as happened to protect `C:\Python314\` by accident) as
   the actual safety mechanism instead of fixing the detection logic.** Rejected outright: this
   ADR's own investigation found that mechanism only protected one of the two standalone
   installs it encountered, purely because of who happened to own which files — not something to
   design safety around.

## Test coverage

- Unit: `_environment_root` recognizes a `python.exe`+`Lib/` pair as an installation root and
  returns it for a file nested inside; a lone `Lib/` directory with no sibling `python.exe` is
  NOT misidentified as an installation root.
- Full pipeline: the exact incident reproduced — a standalone-install stdlib file
  (`Lib/socket.py`, no `conda-meta/`/`pyvenv.cfg`) byte-identical to a named conda environment's
  own copy of the same file produces zero candidates (previously: one, which is what actually
  applied and broke the shared interpreter for real).
