# 0010. Structural (not marker-only) Python environment detection

## Context

ADR-0009 fixed the exact incident it was written against — a shared, uv-managed Python build
lost 71 files (later found to be 186, across three installs) to `exact_duplicate` because
`_environment_root` only recognized `conda-meta/` (conda) and `pyvenv.cfg` (venv) as environment
markers, and none of the three affected installs had either.

A follow-up review asked the harder question directly: root-cause *why* marker-only detection
missed each of the three real installs, and treat that as evidence about the detection strategy
itself, not just about three specific paths.

**Per-install root cause, verified against each real directory on the machine the incident
happened on:**

| install | `conda-meta/`? | `pyvenv.cfg`? | why it was missed |
|---|---|---|---|
| uv-managed build (`~/AppData/Roaming/uv/python/cpython-3.12.12-windows-x86_64-none/`) | no | no | a raw redistributable Python build uv downloads and manages itself — never created by `conda create` or `python -m venv`, so neither tool ever had a reason to write its own marker there |
| `gcloud`'s bundled Python (`~/AppData/Local/Google/Cloud SDK/google-cloud-sdk/platform/bundledpython/`) | no | no | same shape — a vendored interpreter the Cloud SDK installer drops in place directly, not built via conda or venv |
| Android NDK's toolchain Python (`~/sdks/android-sdk/ndk/<ver>/toolchains/llvm/prebuilt/windows-x86_64/python3/`) | no | no | same shape again — a build-toolchain interpreter bundled with the NDK's prebuilt LLVM toolchain |

All three are "not the marker was outside the scanned subtree" or "the walk-up had a bug" —
`_environment_root`'s ancestor walk is unbounded (`path.parents`, no depth limit) and was
verified working correctly in ADR-0009. The actual cause is structural: **none of these three
tools is conda or venv, so neither of the two markers ADR-0008/0009 knew to check for was ever
going to exist.** Marker-file detection is fundamentally bounded by the list of tools someone
thought to name in advance — exactly the same failure shape as the keyword-scan-vs-systematic-
audit gap ADR-0009 already found once during its own recovery.

**A fourth case, checked for completeness and found already safe — but exposing the deeper
issue.** This project's own `.venv` was checked directly: it has `pyvenv.cfg` (real venvs always
do), so it was never actually at risk. But its directory structure was checked too, and it has
**no `python.exe` at its own root** — only `Scripts/python.exe` (the standard Windows `venv`
layout puts the interpreter in a `Scripts/` subdirectory, not the venv root itself). ADR-0009's
own fix — `python.exe` directly in the root, alongside `Lib/` — would NOT have caught a bare
venv missing its `pyvenv.cfg` for any reason (a corrupted write, a hand-rolled environment tool
that skips it, a future Python tooling change). That is the residual risk: even after ADR-0009,
`exact_duplicate` could not be proven safe against an arbitrary embedded Python or runtime
environment — only against the specific shapes already seen.

## Decision

Replace marker-file-first detection with structure-first detection as the default. `conda-meta/`
and `pyvenv.cfg` are checked first (still the fastest, most precise signals when a tool did leave
one), but `_environment_root`'s ancestor walk now also recognizes, as independently sufficient
signals:

1. **A Python executable directly in the directory** (`python.exe`/`pythonw.exe`) — unchanged
   from ADR-0009, but no longer required to co-occur with a sibling `Lib/`; either signal alone
   is now sufficient (see Alternatives for why the AND was dropped).
2. **A `Scripts/` or `bin/` subdirectory that itself holds an interpreter binary** — the standard
   Windows (`Scripts/`) and POSIX (`bin/`) `venv` layout, catching a venv whose `pyvenv.cfg` is
   missing, corrupted, or was never written by a nonstandard tool.
3. **A `Lib/site-packages/` (or `lib/site-packages/`) subdirectory** — existing at all is itself
   proof the directory is, or was built as, a Python environment root, independent of whether its
   interpreter binary is present, findable, or in a location any other check recognizes.

Any ONE of the five total signals (two marker files, three structural ones) is sufficient; the
walk-up returns the first ancestor satisfying any of them. The `pkgs/` short-circuit from
ADR-0009 is unchanged and still runs first — it's still true that conda's own extraction cache
is not a live environment regardless of what structural signals its extracted-package
subdirectories happen to carry.

## Consequences

- **Verified this closes the actual gap, not just the three original paths.** Re-ran the fixed
  detector against the entire 10,134-entry applied batch: still exactly 186 violations, the same
  set already found and restored under ADR-0009 — the broader structural signals introduce no
  NEW violations in this batch, confirming the 186-file recovery was already complete and this
  ADR is closing a forward-looking gap, not uncovering a second live incident.
- **This project's own `.venv` is now protected by two independent signals, not one.** Its real
  `pyvenv.cfg` already protected it; a synthetic fixture reproducing its exact real shape (no
  `python.exe` at the root, only `Scripts/python.exe`, no marker file at all) is now separately
  protected by the new `Scripts/` structural check — proving the fix generalizes past the three
  real incident paths to the layout class they represent.
- **Broader match surface, deliberately.** Requiring only ONE signal (not `python.exe` AND
  `Lib/` together, as ADR-0009 originally required) means a lone, unrelated `python.exe` copy
  sitting somewhere — a backup, a downloaded artifact, anything — is now enough to exclude a
  cluster member from `exact_duplicate`. This is an intentional trade favoring false exclusions
  (a real duplicate that goes untouched) over false inclusions (a live environment file deleted)
  — the asymmetry this whole ADR chain exists to enforce.
- **Still not a completeness proof.** This closes every shape found on this machine to date
  (conda, venv, uv, `gcloud`, NDK) via two marker files and three structural signals. It does not
  and cannot prove no other embedded-runtime shape exists that satisfies none of the five —
  `exact_duplicate` remains, honestly, unable to guarantee safety against an arbitrary unknown
  runtime layout. What changed is the category of thing being trusted: not "does this look like
  one of the tools we thought to name," but "does this directory structurally look like *any*
  Python environment," which is a strictly larger, harder-to-miss set.

## Alternatives considered

1. **Keep requiring `python.exe` AND `Lib/` together (ADR-0009's original condition), and add
   `Scripts/`+`bin/`+`site-packages` as additional AND'd refinements rather than independent OR
   conditions.** Rejected: the whole point of `Scripts/`-based detection is to catch a venv-style
   layout where the interpreter is NOT co-located with `Lib/` at the same level (it's one level
   down, inside `Scripts/`) — requiring both together would still miss exactly the case this
   ADR exists to close.
2. **Only add the `Scripts/`/`bin/` check (the literal gap this project's own `.venv` exposed)
   and skip the `site-packages`-alone signal.** Rejected: `site-packages` existing without a
   findable interpreter (moved, renamed, deleted, or simply not checked here) is still strong,
   independent evidence of an environment root — a fifth signal for a marginal implementation
   cost, and the whole thesis of this ADR is that redundant, independent structural signals are
   safer than relying on any single one.
3. **Build a general "known embedded-runtime registry" (uv, gcloud, NDK, rustup, nvm, ... by
   name) instead of structural detection.** Rejected again, more firmly than in ADR-0009: this
   is the literal failure mode already proven twice now (the ADR-0009 keyword scan, and the
   marker-file list itself) — a registry is only as complete as the list of tools someone
   thought to enumerate, and the whole point of this ADR is to stop depending on that.

## Test coverage

- Unit: `_has_python_executable`, `_has_interpreter_bin_dir` (both Windows `Scripts/` and POSIX
  `bin/` layouts, with and without a real interpreter inside), and `_has_site_packages` (both
  `Lib/site-packages` and `lib/site-packages`) each tested directly against real fixture
  directories.
- Regression fixture, closing the residual risk: a synthetic venv-shaped directory with `Scripts/
  python.exe` and `Lib/site-packages/`, deliberately built WITHOUT `pyvenv.cfg` or `conda-meta/`
  — `_environment_root` resolves it via the new structural signal alone.
- Regression fixtures, one per real incident install: the uv-managed build, `gcloud`'s bundled
  Python, and the NDK's toolchain Python, each reproduced with `python.exe` directly at the root
  (matching their real, verified directory listings) and neither marker file present.
- Full applied-batch re-audit: re-ran the fixed detector against all 10,134 manifest entries from
  the real `exact_duplicate` apply; violation count unchanged at 186 (all already restored under
  ADR-0009) — no new violations introduced or newly discovered by the broader structural match.
