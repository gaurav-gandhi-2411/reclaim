# 0033. Default scan scope is the invoking user's own profile, never a volume-root traversal

## Context

A real smoke-test scan by a non-admin local account (`ReclaimSmokeTest`) on a real
multi-project dev machine classified 13,927 of 13,991 `dev_artifacts` candidate paths as
belonging to OTHER users'/other projects' directories — unrelated ML projects under a
different user account's profile, reachable and deletable only because of broad ACLs
granting Modify rights at the volume root. Only agent-level discipline (not the product
itself) prevented an actual deletion during that smoke test.

Root cause (confirmed by reading `src/reclaim/api/static/app.js`, `src/reclaim/api/routes.py`,
`src/reclaim/api/service.py`, `src/reclaim/drives.py` directly, not assumed from README
language): SIMPLE mode's single primary action, "Clean My Computer" — the very first thing a
non-technical user sees, described in the UI as "Scans your whole computer and finds safe
things to clean up" — called `POST /api/scan/full-drive`, which enumerates every locally
attached `DRIVE_FIXED` volume (`reclaim.drives.list_fixed_drives`, typically just `C:\`) and
scans from each drive's literal root. There was no confirmation step, no warning, and no
narrower default — one click reached the entire volume, including every other local account's
profile directory that happened to be readable/writable by the scanning account.

Two other questions this ADR resolves, both checked directly against the code rather than
assumed:

1. **Does any existing rule check file/path ownership?** No. `grep`-ing `safety.py` and
   `detectors.py` for any SID/owner check (`GetNamedSecurityInfo`, owner comparison, an
   `os.stat`-derived owner field) returns zero matches. `DEFAULT_PROTECTED_ROOTS` in
   `config.py` protects Windows/Program Files/ProgramData — it says nothing about `C:/Users/*`
   at all. Ownership has never been a safety dimension this tool reasons about.
2. **Is this single-user-machine-safe, or a broader default-scope problem?** The latter. The
   code path that reaches other accounts' directories is not gated on "multiple real user
   accounts exist" in any way — it is gated purely on what `GetDriveTypeW` reports as
   `DRIVE_FIXED` and what the OS's actual ACLs happen to allow the scanning account to read.
   On a genuinely single-user machine the blast radius is smaller only because there is
   nothing else on the volume to reach, not because the scan is scoped any differently. An
   admin account's own "Clean My Computer" click reaches the identical code path and would
   surface any other local account's profile the same way, if Windows' default ACLs (which do,
   in some configurations, grant broad access to an admin account) permit it. This is a default
   design defect independent of how many real users exist on the machine, not a narrowly
   multi-user-only bug.

Every existing rule-based detector default that needs to look outside a literal
`%USERPROFILE%\...` path (package/model caches under `%LOCALAPPDATA%`, `.conda`, `.m2`,
`.gradle`, temp roots — see `config.py`'s `_default_*_paths` helpers) already resolves those
paths FROM `%USERPROFILE%`/`%LOCALAPPDATA%`, both of which sit inside the user's own profile
tree. Nothing about SIMPLE mode's existing candidate categories needed scope wider than
`Path.home()` — the volume-root default was never load-bearing for any real detector, only an
accident of "scan my whole computer" being interpreted literally.

## Decision

**The default scan root is now the invoking user's own profile (`Path.home()`), never a
volume-root/fixed-drive enumeration, for any default action.**

- `reclaim.api.service.user_scan_roots()` — new function, returns `[Path.home()]` (injectable
  `home=` for tests, mirroring `suggested_scan_roots`'s own convention).
- `POST /api/scan/my-files` — new endpoint, SIMPLE mode's new DEFAULT "Clean My Computer"
  action. Same background-task/single-flight/polling shape `run_scan` already provides for
  both `POST /api/scan` and `POST /api/scan/full-drive`; only the root differs.
- `app.js`'s `startSimpleScan()` now calls `/api/scan/my-files`; the idle-screen copy changed
  from "Scans your whole computer..." to "Scans your files..." to match.

**A whole-drive scan remains available, but strictly as an explicit, separately-surfaced
opt-in — never a default.** `POST /api/scan/full-drive` (and `list_fixed_drives`) are
unchanged in mechanism; what changed is reachability. SIMPLE mode now offers "Scan the whole
drive (advanced)" as a visually secondary control that opens a dedicated confirmation dialog
(`full-drive-confirm-dialog`) naming the actual risk in plain language ("this can include other
people's files if Windows' permissions happen to allow it") before `POST /api/scan/full-drive`
is ever called. ADVANCED mode's manual scan-path entry (`#scan-path` + `scan-confirm-dialog`)
was already an explicit typed-path action with no pre-filled default and its own confirmation
dialog — that flow needed no change; it already matched the "explicit opt-in, never silently
defaulted" bar this ADR sets for the SIMPLE-mode primary action.

**No remaining default code path reaches a volume-root scan.** `list_fixed_drives`/
`fixed_drive_roots`/`POST /api/scan/full-drive` all still exist and still work exactly as
before — the only change is that nothing in either mode's default/one-click flow calls them
anymore; every call site now requires either a typed path (ADVANCED mode) or an explicit,
warned confirmation (SIMPLE mode's whole-drive opt-in).

Ownership/ACL-based filtering (checking whether the scanning account actually owns each file
before proposing it) was considered and explicitly rejected for this fix: it would still let a
mis-scoped scan enumerate and stat another user's entire tree (a real privacy/perf cost) before
filtering candidates after the fact, and Windows ownership semantics (inherited ACLs, groups,
"Users" broad-Modify configurations) are not a reliable enough signal to gate deletion
proposals on alone. Narrowing the DEFAULT SCAN ROOT closes the finding at its actual source —
the walk never visits another account's directory at all — rather than filtering its output
after the fact.

## Consequences

- SIMPLE mode's default action now genuinely matches its own safety story ("Reclaim scans your
  files" in the first-run overlay) rather than contradicting it with a literal whole-drive walk
  behind a "Clean My Computer" label.
- A user who explicitly wants a whole-drive scan can still get one, with an honest warning
  about what it can reach — the capability was never removed, only ungated-by-default removed.
- `evals/test_scan_scope_gate.py` (registered in `scripts/verify.py`'s `_SAFETY_GATE_FILES` and
  `.github/workflows/eval.yml`'s safety-gate job) is the real regression proof: a fixture tree
  simulating two sibling local-account profiles, scanned via the exact primitive
  `POST /api/scan/my-files` uses, confirms zero candidates and zero indexed rows ever reach the
  sibling account's directory — plus a structural check on `app.js`'s own source proving the
  primary button stays wired to the user-scoped endpoint, not silently rewired back to
  `/api/scan/full-drive`.
- Residual risk, stated honestly: a user's own profile can itself still contain files that
  belong, in some looser sense, to someone else (a shared Downloads folder synced from a
  team drive, a mounted network path saved under the profile) — this fix closes the
  volume-root-traversal shape of the finding, not every conceivable cross-ownership shape.
  ADVANCED mode's explicit, confirmed, typed-path scan remains available for a user who
  deliberately wants to point Reclaim somewhere else, with the same confirmation-dialog pause
  it already had before this ADR.

## Alternatives considered

1. **Filter candidates by file owner (SID) after a volume-root scan.** Rejected: still walks
   and stats every other account's files before filtering (privacy + perf cost), and Windows
   ownership isn't a reliable-enough signal to gate deletion on by itself (a file's owner SID
   can be stale, inherited, or simply "Administrators" for files an ordinary user created under
   an admin-provisioned profile).
2. **Keep the volume-root default but add a warning dialog every time.** Rejected: a dialog a
   user clicks through on literally the first and only action SIMPLE mode offers trains exactly
   the "click through the warning" reflex safety dialogs exist to prevent, and does nothing for
   automated/scripted callers (`MCP` control surface, `reclaim serve` API consumers) that never
   see the dialog at all. Narrowing the actual default is the only fix that holds regardless of
   caller.
3. **Require an explicit `--scope` flag/setting with no safe default at all.** Rejected: SIMPLE
   mode's entire design premise (ADR-0023) is "a user who understands nothing about disk
   cleanup" — forcing a scope decision onto that user reintroduces exactly the complexity
   SIMPLE mode exists to remove. A safe, sensible default (the user's own files) that can be
   widened explicitly is the simpler solution that still satisfies the constraint.
