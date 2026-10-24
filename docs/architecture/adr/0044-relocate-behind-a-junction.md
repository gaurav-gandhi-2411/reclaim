# ADR-0044: Relocate a folder to another drive behind a junction

Status: accepted (2026-10-24, autonomous run Phase B). Spec: `docs/specs/relocate.md`.
ADR-0043 is agent-artifact cleanup, on `integration/run-2026-10`.

## Context

The first real run (2026-10-09, `scripts/relocate_dir.ps1`) moved 185 GiB of model caches from C:
to D: and lifted C: free space from 15 to 208 GiB, with no file differences. Deletion can't do
that: the data is in use and is not junk. The prototype is a PowerShell script run by hand. The
product needs the same stages in the app, as a review-tier action (the user clicks), with the
data-safety bar of the rest of Reclaim. That means a dry run first, a rollback at every stage,
and nothing deleted that has no other proven copy.

## Decision

`src/reclaim/relocate.py` carries the prototype's stages over to Python and adds a journal:

1. **Classify, exclusion first.** The user's exclusions (`SafetyValidator.exclusion_match`, path
   text and then the names beneath it) are checked before anything else. A hit ends the
   inspection, so an excluded tree is never sized or probed. Next come the built-in protected
   roots, the profile roots (too broad), Reclaim's own data and index, and the hardlink caches
   (uv, pnpm, conda `pkgs`). Docker and WSL are classified as guided moves, using their own
   steps. Then one read-only walk refuses trees that a plain copy would damage: links,
   hardlinked files, Python environments, cloud placeholders, unreadable entries, no files.
2. **Plan (the dry run).** The plan checks that:
   - the target is on a different volume (by volume serial);
   - the target is absent, and not inside the source or an excluded or protected path;
   - the target volume has 1.2x the folder's size free;
   - no `.reclaim-moved`, `.reclaim-probe` or `.reclaim-restore` is left from earlier;
   - a rename round-trip succeeds. Windows refuses to rename a folder while a file under it is
     open, so the round-trip proves there are no live holders; it never kills a holder.

   The plan changes nothing; unlike the prototype, it does not even create the target's parent.
3. **Move.** The move runs on a fresh plan:
   1. Copy, keeping data, timestamps and the A/H/S/R attributes.
   2. Compare the SHA-256 of every file whose source has not changed since the copy started.
   3. Freeze: rename the source to `<name>.reclaim-moved`.
   4. Delta re-sync. Files created, changed or deleted during the copy are brought across. Every
      re-copied file is hashed against the frozen source, and the full listings and byte totals
      must match.
   5. Make the junction, then check it: it must point to the target, and the listing through it
      must equal the old copy's.

   Junctions are written with `FSCTL_SET_REPARSE_POINT` directly. On a machine with
   `LongPathsEnabled=0`, `_winapi.CreateJunction` fails for targets over 260 characters, and a
   prefixed target makes it write a broken name (measured 2026-10-24). Every filesystem call
   uses `\\?\` paths, so paths over 260 characters work.
4. **Journal.** The journal is a JSON file per move, written atomically before each step; its
   history is the audit log. Any ordinary error rolls back before returning. A killed process
   leaves the journal, and `recover` either finishes the move (the junction exists and checks
   out) or rolls it back.
5. **Rollback works at any stage, from the disk's state, not only the journal's.**
   - Before the junction exists, our copy is removed; nothing else wrote to it.
   - After the junction exists, rollback first probes the target for open handles, then copies
     back into the old copy every file a program added or rewrote through the junction. A
     rewrite is detected by size or mtime: the copy carries the original's mtime exactly. This
     happens while the junction still stands, and a final pass runs after the junction is
     removed; if that final pass fails, the junction is put back. Rollback deletes nothing in
     the old copy.
   - Once the old copy is deleted, rollback copies the target back into a verified
     `.reclaim-restore` first.
   - Our copy in the target is removed only when every file in it is byte-equal to the
     restored folder. Otherwise it is kept and the outcome says why.
6. **Deleting the old copy is a separate call.** It is allowed after 7 days, or earlier with a
   confirmation. Every file of the old copy must either have the same SHA-256 in the target or
   a different mtime there (rewritten through the junction). A same-size, same-mtime difference
   is a bad copy and refuses the delete. A file missing from the target refuses the delete
   unless the user accepts it. The freed figure is the volume's free-space delta.

CLI: `reclaim relocate list | plan | move --yes | status | rollback | recover | delete-old`
(`src/reclaim/relocate_cli.py`). The dashboard page and the MCP approval card are a follow-up
PR on top of this module.

## Consequences

- **Zero data loss is a CI gate:** `evals/test_relocate_gate.py`, with one fixture per case in
  `evals/fixtures/relocate/` and one real process kill. Every case checks that each original
  file is still byte-identical at its original path, whatever the outcome.
- **What the tests found in this module before review:**
  - A copy-time hash check against the live source aborted every move of a folder that was
    written during the copy. Changed files are now left to the delta, which runs on the frozen
    source.
  - Rollback over a corrupted target copy removed the junction and then failed, leaving the
    original path missing. Copy-back now happens before the junction is removed, the old
    copy's bytes win over a same-mtime difference, and the target is kept.
  - A clock-window rule let a corrupted file pass the delete check. It was replaced by the
    exact-mtime rule.
- **Not supported yet:**
  - Trees containing links (they must stay).
  - ACLs and owners: the copy inherits the target's permissions, as robocopy `/COPY:DAT` did.
  - A case-only rename during the copy fails the move closed, with nothing lost.
- **The junction's target is whatever the user chose.** Moving to removable or network drives
  is not blocked here. The CLI suggests only fixed drives; the UI will offer only those.
- **Real machine, this run:** dry run only (`reclaim relocate list`), per the run spec.

## Alternatives

- **Keep the PowerShell script and shell out to it.** Rejected: it can't be tested hermetically,
  it has no journal (a kill between freeze and swap needs manual repair), and it can't handle
  paths over 260 characters.
- **robocopy for the copy.** Rejected for the app: the per-file hash is the verification anyway,
  robocopy's exit codes are a second failure surface, and it needs a real process runner. That
  runner is refused under pytest, so the move could not be tested.
- **A symlink instead of a junction.** Rejected: directory symlinks need Developer Mode or admin,
  and some programs treat them differently; a junction needs neither.

## Addendum 1 (2026-10-24): first hostile review (B1), FAIL, fixed

An independent data-safety review of `3a39526` found 11 problems, each shown by a probe test that
fails on that head (`tests/test_relocate_review_b1.py`). No case deleted an old copy whose data
was not in the target, but four left the original path missing, empty or dangling (two of them
reported as success) and one lost data:

1. **A kill inside the junction's creation** (after its `mkdir`, before the reparse data) left an
   empty folder at the original path, which rollback took as "restored". **2. A folder a program
   recreated** after the junction was removed was taken the same way. Rollback now treats any
   plain folder at the original path after the swap as suspect: an empty one is removed and the
   old copy put back; one with content counts as restored only if every file of the target is in
   it byte for byte (our own interrupted rollback); otherwise it refuses and says where the data
   is. The same applies when the old copy was already deleted (the data is copied back first).
3. **Moves that nest** (a target inside another move's target or `.reclaim-moved`) let one move's
   rollback reach the other's data. The plan now refuses any overlap with an open move's source,
   target, old copy or restore folder (read from the journals; an unreadable journal fails the
   plan), any path through one of Reclaim's working folders, and any path reached through a link.
4. **Named data streams** (`Zone.Identifier`, app metadata) were not copied, so deleting the old
   copy lost them. Every copy now carries them, every hash covers them, and a folder that carries
   streams (folders are created, not copied) must stay.
5. **A journal folder inside the source** was renamed away at the freeze. It is refused, and the
   CLI's journal folder joins the must-stay list.
6. **A kill inside rollback's probe of the target** left the target at `.reclaim-probe`; rollback
   now puts it back, and a missing target is reported as missing, not "in use".
7. **A kill inside the plan's probe** left the source at `.reclaim-probe` with no journal. The
   move's probe runs after its journal exists again, and a dry run puts back its own interrupted
   probe (only the probe makes that name).
8. **A writer that restores the mtime** between the copy and the freeze was missed (stale bytes
   served behind the junction until a later delete check caught it). The full SHA-256 compare now
   runs after the freeze, on the frozen source, for every file; a file still different is copied
   once more and a second difference fails the move. The pre-freeze compare is gone, so the data
   is still read twice in all.
9. **The shared exclusion walk follows junctions** (`safety.subtree_exclusion_match`). Accepted,
   not changed here: it descends junctions on purpose (content reached through a junction counts,
   the pnpm case), PR #190 bounds junction cycles, and the verdict for such a folder is must-stay.
10. **A case-only rename** during the copy failed the move (closed, nothing lost). The re-sync now
    renames the copy to the new spelling.
11. The misleading messages of 1, 2 and 6 are gone with their fixes.

All 14 probe tests and three changed engine tests fail on `3a39526` and pass on the fix.
## Addendum 2 (2026-10-24): second hostile review (B2), FAIL, fixed

A second independent review, of `50ffcdf`, found 14 problems, each shown by a probe that fails
on that head (`tests/test_relocate_review_b2.py`, which also sweeps a kill over every killable
call in a move and in a rollback, with and without the old copy deleted).

The high ones shared one cause: rollback decided what sat at the original path was "the
original" by looking at it, and after a kill at the wrong moment, or a program recreating its
folder inside rollback's own window, it reported a folder that was not the original as
restored (H1, H2), once after removing our target. **The fix is identity, not inspection:** the
journal records the source folder's NTFS file ID before anything happens (a rename keeps it),
and the restore copy's ID when rollback makes one. Only a folder with one of those IDs is ever
accepted at the original path. Anything else there is removed if empty and otherwise left alone
with nothing changed, saying where the data is. The old copy and our target are recognised the
same way, so a folder of the same name that is not ours is never used or removed. Because the
design rests on file IDs, streams and junctions, both volumes must be NTFS.

Also fixed:
- **H3:** a complete restore copy was wiped before checking the target still existed. It is now
  never removed unless the target is present, and is reused when it already matches.
- **H4:** a write into the old copy itself was deleted with it. An old-copy file newer than the
  target's, or added after the swap, now refuses the delete, even with `accept_missing`.
- **M1:** a kill at rollback's last journal save left a message telling the user to empty the
  original folder. With identity it simply finishes.
- **M2:** an excluded project appearing during the copy was moved. Exclusions are re-checked on
  the frozen tree.
- **M3:** a dry run could rename its own interrupted probe onto an excluded path. It no longer
  does.
- **L1-L7:**
  - an attribute-only change before the freeze is now synced;
  - folder streams are now copied and compared, no longer refused;
  - the root folder's hidden and system bits are kept on the junction;
  - folder case renames are aligned;
  - read-only folders can be removed from the copy;
  - a journal with wrong field types fails the plan cleanly;
  - a kill between the target's creation and the next save no longer blocks later plans.

**Mutation** (`D:\reclaim-build\tools\mutate_rl2.py`, 22 targeted mutants): 21 killed, after
six new pins for the first survivors. S4 (reuse a restore copy that already matches) is
equivalent: rebuilding it gives the same result.
## Addendum 3 (2026-10-24): third hostile review (B3), FAIL, fixed

A third independent review, of `603f189`, found no lost bytes in any kill sweep and no foreign
folder accepted as the original. It found 14 gaps around the identity design
(`tests/test_relocate_review_b3.py`, every probe fails on that head). Fixes:

- **The old copy is checked against a record, not judged by timestamps (H3, M1, L3).** The full
  SHA-256 compare after the freeze already hashes every file, so the move now writes the old
  copy's exact state at the swap to `relocate-<id>.manifest`: each file's size, mtime and hash,
  and each folder's attributes and streams. `delete_old_copy` deletes only if the old copy still
  matches that record, so a file pasted into the old copy (Explorer keeps mtimes) or an older
  version written into it refuses the delete.
- **Before the freeze, rollback never claims anything (H1, H2).** The freeze checks that the
  folder still has the recorded file ID; one a program replaced during the copy is not frozen.
  Rollback knows whether the freeze was ever attempted. If not, this code never moved the
  original: it only puts back a probe a kill or a racing writer left, removes our copy if the
  original is in place, and otherwise keeps the copy. The move's own probe failure goes through
  that rollback instead of being journalled "nothing changed".
- **Copy-back carries folder metadata (M2).** Copy-back now brings folder attributes and
  streams and file attributes, and our copy is discarded only if those match too.
- **Type conflicts are set aside, not stuck (M3).** Where a program replaced a folder with a
  file (or the reverse), the old copy's entry is renamed to `<name>.reclaim-conflict`.
- **A missing target with an intact old copy restores the old copy (M4).** The message says
  that anything written to the unreachable target is only there.
- **Path-based deny patterns are re-checked at the live path before the freeze (M5).**
- **Messages name where the original really is (M6):** the probe folder, the old copy, the
  restore copy, or the target.
- **Low findings:**
  - a folder carrying a named stream is not "empty" (L1);
  - `recover` keeps the root's hidden and system bits (L2);
  - a half-made restore folder counts as ours only while empty (L4).
- **Journals without IDs (L5) do not exist:** the feature has never shipped.

**One regression of my own, caught before commit:** rewriting rollback dropped the condition
that copy-back runs only once a junction may have existed. With it gone, a kill during the delta
pass made rollback copy the target's stale file over the newer original. The existing kill test
(`delta_file`) failed, and the condition is back with a comment. A mutant (T3) now pins it.

**Mutation** (`D:\reclaim-build\tools\mutate_rl3.py`, 13 mutants of the new guards): all 13 killed, four
after pins added for the first survivors (T8, T9, T12, T13).
## Addendum 4 (2026-10-24): fourth hostile review (B4), FAIL, fixed

A fourth independent review, of `463f6da`, passed every kill sweep, including new sweeps over
`delete_old_copy` (inside its tree removal) and over the probe-failure paths, followed by recover
or rollback. It found 9 problems (`tests/test_relocate_review_b4.py`, every probe fails on that
head):

- **H1, H2: rollback's copy-back still assumed the old copy was unchanged since the swap.**
  - A write made into `.reclaim-moved` was overwritten by the target's stale version, and our
    copy was then discarded, so the edit existed nowhere.
  - A stream added to an old-copy folder was removed.
  - The fix: copy-back now reads the swap manifest. An old-copy file that no longer matches it
    is set aside as `<name>.reclaim-conflict` before the target's version is copied in, so both
    survive. Folder streams are only added or replaced, never removed. A same-named stream is
    replaced only while that folder still matches the record. A manifest that should exist but
    cannot be read stops the rollback before anything changes.
- **M1, L2: "missing" meant "could not be checked".** A transient access error on the target made
  rollback treat it as gone and restore the stale old copy. A failed check also let the delete
  report success while the old copy was still there. "Missing" is now only not-found; any
  other error refuses and says to retry.
- **M2:** the junction-was-removed branch now gives the same warning as the missing-target
  branch: post-move writes are only on the unreachable target.
- **M3:** the folder ID is checked again on the frozen name right after the rename. A folder
  replaced in the moment between the check and the rename is put straight back, and the move
  stops as if it had never frozen.
- **Low findings:**
  - a conflict name must be free on both sides, so a set-aside can never merge into a target
    folder (L1);
  - an unreadable manifest's message says what to do next (L3);
  - a file left at the original path gets a clear message and is never taken for a folder
    (L4).

**Mutation** (`D:\reclaim-build\tools\mutate_rl4.py`, 11 mutants): 9 killed, 2 after new pins (V2, V6).
Two are equivalent:
- V5 (a conflict name checked on one side only): the manifest set-aside still preserves the old
  content, because it no longer matches the record.
- V10 (a file accepted as the original): NTFS file IDs are unique per volume, so no file can carry the
  original folder's ID.
## Addendum 5 (2026-10-24): fifth hostile review (B5), FAIL, and a redesign of rollback after the swap

The fifth independent review, of `dfa7eb2`, again passed every kill sweep. It found a loss with
no kill involved (H1): an edit made in `.reclaim-moved` was overwritten by rollback when a
program had renamed the same file through the junction in a different case. It also found eleven
more problems: unrecoverable states after a race at the freeze (M1), a link a program made in
the moved folder blocking rollback for ever (M2), a resumed delete that skipped the old-copy check
(M3), and eight low ones.

**The pattern, not the instance.** Reviews B3, B4 and B5 each found a real loss in the same place:
rollback's copy-back, which merged two trees that had both diverged since the swap (the moved
folder, written by programs through the junction, and the old copy, written directly) into the
old copy. Each patch closed one case and exposed the next (timestamps, then the manifest, then
case). **Decision:** remove the merge instead of patching it a fourth time. Rollback after the swap
now:

1. Copies the moved folder, as the programs left it, to `<name>.reclaim-restore`: a fresh copy,
   proved file by file and folder by folder. Disk space is checked first, and a refusal changes
   nothing.
2. Removes the junction. It then brings the restore copy level with anything written in the
   meantime; the restore copy is ours, so only our own copies are changed. If that step fails,
   the junction is put back.
3. Renames the restore copy into place, recognised by its own file ID. Our moved copy is then
   removed only if it is byte-identical to the restored folder, including attributes and
   streams.
4. Never writes into `.reclaim-moved`. It is kept exactly as it was, with the pre-move state
   and anything written into it, and the message says where it is. `delete_old_copy` can delete
   it later, comparing it with the restored folder under the same manifest checks as before.

A file that has the recorded mtime in the moved folder but other bytes is a bad copy, not an edit.
Its original is taken from the old copy, which is read, never written. With `use_old_copy`, or a
moved folder that is gone or holds something a copy cannot keep (a link: M2), the old copy is
renamed back instead, and the moved folder is left exactly as it is.

Removed with the merge: `_sync_back`, the conflict set-aside, `_merge_folder_meta`, and the
manifest-gated stream rules. H1, L1-L4 and the whole class behind them go with them. Also fixed:
- the freeze race recovers through the pre-freeze path, putting a frozen foreign folder back
  (M1);
- `recover` at the delete stage goes through `delete_old_copy`, which re-checks what is left of
  the old copy (M3);
- messages: a touched old copy is never deleted by Reclaim and the message says so (L6), and
  "nothing changed" appears only where true (L5).

**Decided, not changed (L7):** a rollback the user asked for brings back everything the programs
wrote, including a folder named like an excluded project. Refusing would strand that data on the
other drive. The requirement is that it is never lost and lands at its original path.

**Cost:** a rollback after the swap needs room for one copy of the folder on its original volume.
`use_old_copy` needs none, but leaves post-move writes in the moved folder.

**Mutation** (`D:\reclaim-build\tools\mutate_rl5.py`, 9 mutants of the new rollback): all 9 killed.
## Addendum 6 (2026-10-24): sixth hostile review (B6), FAIL on edges, fixed

The sixth independent review, of `4eb7b06` (the redesigned rollback), found no high findings.
Its new sweep killed the new rollback at 99 points in 4 variants (old copy kept or deleted, then a
plain rollback or `use_old_copy`). The cases included edits on both sides, a case rename, a long
path, streams, read-only files, and writes and deletes during the copy-back. Not one byte of any
file was lost, and the old copy was never written.

It found ten problems (`tests/test_relocate_review_b6.py`):
- **M1, L5:** the space check, and the "volume gained" figure, measured the target volume. The
  check was handed the junction, and `shutil.disk_usage` follows it. Free space is now taken at
  the nearest real folder of the original path, so a rollback can no longer fill the drive it was
  meant to relieve.
- **M2:** a partly deleted old copy was put back as "the copy from before the move". An
  interrupted delete is now remembered before removal starts, and such a copy is never put back;
  the message says what to do.
- **M3:** a failure after the junction was removed (a held file at the final rename, a read error
  in the repair) left the original path empty. Every step after the removal is now inside the
  guard that puts the junction back.
- **M4:** `delete_old_copy` deleted folder streams the live folder no longer had. A recorded folder
  stream missing from the live folder now counts as missing, and needs `accept_missing`.
- **M5:** rolling back a swap that never reached DONE skipped the 7-day keep period. It now
  counts from the moment the swap was journalled.
- **Low findings:**
  - a partial restore no longer skips the space check (L1);
  - `use_old_copy` removes our own restore copy when the moved folder is still there (L2);
  - a refusal never leaves the original path empty: the junction is put back first;
  - messages name both places the data is (L3, L4).

**Mutation** (`D:\reclaim-build\tools\mutate_rl6.py`, 8 mutants): all 8 killed.
