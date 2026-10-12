# Spec: Relocate to another drive

**Target user:** a Windows user whose system drive is nearly full while a second fixed volume (often a second SSD) has hundreds of GB free.
**Pain point:** most of the space on the system drive is data that must exist (model caches, container images, datasets), so deletion is the wrong tool and the user cannot safely move it by hand.
**Success metric:** GB recovered on the system volume with zero deletions of unique data, zero file differences after the move (per-file SHA-256), and zero broken paths for the programs that use the data.
**Who pays:** the user, by getting a usable disk back. No spend is involved.

Status: spec written 2026-10-09 from the first real run. Prototype: `scripts/relocate_dir.ps1`. Gap-plan origin: `docs/GAPS-2026-10.md` item 0.

## 1. Evidence that this is the biggest lever (VERIFIED, `D:\relocated\hf_move.log`, `%TEMP%\reclaim_soak\2026-10-09-attrib\sizes.csv`)
- Hugging Face cache `hub`, `datasets`, `xet`: 1,221 files, 185.24 GiB moved from C: to D: behind junctions. Per-file SHA-256 differences = 0 on all three, before the swap and again before each old copy was deleted.
- Wall-clock: hub 153.4 GiB 16.1 min, datasets 31.8 GiB 3.3 min (copy, full hash, freeze, delta, swap); deleting the old copies re-hashes everything again (about 14 min for hub).
- C: free went 15.45 -> 208.05 GiB. The sampler shows each step as a rise equal to the bytes deleted (+31.81 vs 31.84 expected, +153.38 vs 153.40); there was no delayed or missing space.
- Rehearsal first: a 7.23 GiB, 44,859-file Android SDK folder went through the same script; `adb version` then ran through the junction path.
- Next best deletion item in the gaps plan is at most ~60 GB.

## 2. What it does
Detect fixed volumes with free space, list candidate directories on the system volume with size, owner and live handle holders, and on confirmation move one directory to the other volume leaving a directory junction at the old path (same path, data elsewhere), so no program's configuration changes. Every move is reversible until the user chooses to delete the old copy, and that deletion is a separate step.

## 3. Safety rules (each was exercised in the first run or in the verifier pass)
1. **Dry run by default.** It reports size, file count, target free space and the handle probe, and changes nothing but creating the target's parent directory.
2. **Rehearsal first.** The first move on a machine, and any change to the script, is run end to end on one real, non-sensitive 5-10 GB directory before a large or shared one.
3. **Full hash, not a sample.** After copying, file count, total bytes and the SHA-256 of every file are compared. A first version hashed a sample (54 of 200 files); the verifier pass rejected that.
4. **Freeze and re-sync.** After verification the source is frozen by renaming it to `<name>.moved`, a `/MIR` delta copy picks up anything created, changed or deleted during the copy window, every file written since the copy started is re-hashed, and counts and bytes are re-checked. Only then is the junction made. A test edit and a new file written mid-move both reached the target.
5. **Handle check.** A directory rename fails while any file under it is open, so a rename round-trip before the copy and the freeze rename itself are the probes. If either fails the move does not proceed and nothing has changed. The caller may retry (the first run retried every 30 min for up to 24 h); the script never forces or kills a holder.
6. **Links are preserved or the move is refused.** The source is scanned first. Reparse points (symlinks, junctions) inside the tree make the preflight refuse, because a plain copy would follow or drop them. The Hugging Face tree was checked: 0 reparse points and 0 multi-linked files in hub, datasets and xet, with source and destination both re-scanned (0 / 0). A future version that wants to move trees containing links must copy them as links and prove link count and targets match.
7. **Refuse hardlink-dependent caches.** The uv cache and conda `pkgs` are refused, as is any path that is inside, contains, or is a virtual environment (`.venv`, `venv`, `site-packages`, searched to depth 6). Hardlinks cannot cross volumes. uv's documentation says the cache must be on the same filesystem as the environment or it "will instead need to fallback to slow copy operations" (docs.astral.sh/uv/concepts/cache, fetched 2026-10-09), so moving it would make every venv left on C: a full copy and increase C: use. conda's hardlink behaviour is BELIEVED to be the same, not fetched.
8. **Refuse protected locations.** Anything on the exclusion list (including projects the owner has marked off limits), the Reclaim vault (it must stay on the volume of the files it vaults so quarantine stays a rename and rollback holds, ADR-0001/0005), and system directories. Moving a protected project's files counts as touching them and needs the owner's explicit approval.
9. **Space rule.** The target volume must have at least 1.2x the source size free, and must be a different volume.
10. **Keep the old copy until a real use test passes.** The old copy stays as `<name>.moved`. For a model cache that means loading real models read-only, offline, through the junction path, including one used by each kind of consumer (the first run loaded a model for each of three projects, read every tensor of the smallest weight file, and confirmed the real path resolves to the target volume).
11. **Delete is a separate run that re-checks.** `-DeleteMoved` compares counts and bytes, then SHA-256 of every file against the target, and deletes only if all are equal. A same-size corrupted target is refused (tested). The check and the delete are never one command from the operator's side.
12. **Automatic rollback.** Any failure after the freeze removes the junction if made and renames `.moved` back. The copy left in the target is then an orphan the user must remove; a retry with `-ResumeTarget` re-syncs it (`/MIR`) and still runs the full hash.
13. **One confirmation per move**, with the figures from the dry run on the confirmation screen.

## 4. Known limits (do not hide these in the UI)
- Paths over 260 characters make the preflight throw (fail closed, nothing changed).
- `/COPY:DAT` does not copy ACLs or owner; the junction target inherits the destination's permissions. Not yet verified for directories with custom ACLs.
- Behaviour tested on Windows PowerShell 5.1 and pwsh 7; a volume filling mid-copy is untested.
- Docker Desktop's disk image and WSL distros are not junction-movable: Docker has its own setting, WSL needs `wsl --export` / `--import`. Those are guided steps, not automatic ones.
- Moving a tree out from under a program that has it open is refused, not forced; a tree always open (a running service) cannot be moved while it runs.
- The index and vault cannot use this yet: `data_root()` is the executable's directory and there is no config key for the index path. The prerequisite is a `[storage] index_path` setting.
- Shadow copies: free space rose by the full deleted amount in the first run, so System Restore did not retain it there. Whether it can on other machines is unmeasured (`vssadmin` needs admin). The "freed" figure should therefore always come from the volume's free-space delta, not from the bytes deleted.

## 5. Ranking and UI
Rank by GB recovered, then by risk (fewest holders, no protected owner). Show for each candidate: size, last-write age, the processes holding it, the target volume and its free space, and the class: MOVABLE, MOVABLE WITH GUIDED STEPS (Docker, WSL), or MUST STAY (with the reason). Every screen has loading, empty and error states; the empty state says why nothing is movable (no second volume, no candidate over the threshold).

## 6. Acceptance tests
- A scratch tree moves with 0 hash differences and a working junction; a file edited and a file added mid-move reach the target.
- A same-size corrupted target is refused by the delete step.
- A directory containing a reparse point, a `.venv`, or the uv/conda cache path is refused before any copy.
- A directory with an open file is refused at the probe and nothing changes.
- A stale `<name>.moved` is refused before any copy.
- A target with `[ ]` in its name swaps correctly.
- The freed-space figure equals the volume free-space delta.

## 7. Open items
- Bring the logic into the app (Python, with the same stages), keep the script as the reference.
- Link-preserving copy for trees that contain links.
- ACL-preserving copy mode and its test.
- `%TEMP%\claude` scratch of other tools: movable in principle, but live handles and excluded-project data make it an owner decision.
