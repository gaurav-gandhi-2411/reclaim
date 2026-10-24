# Plan: Docker and WSL relocation through the product (plan only -- NOT built)

**Target user:** a developer whose system drive holds a Docker Desktop disk image and one or more WSL distributions, with a second fixed drive that has room.
**Pain point:** these are the largest single files on such a machine. A junction cannot move them, because each is owned by a program with its own registration. Today relocate only prints the steps, and lists them at 0 bytes.
**Success metric:** GB moved off the system volume with the container and distribution still starting afterwards, and zero data loss. The old copy is removed only after the new one is proven to start.
**Who pays:** the user, in disk space. No spend is involved.

Status: plan written 2026-10-24 (autonomous run, item D8). Not built. Any part that runs `wsl.exe` or changes Docker settings needs the owner's decision and its own ADR.

## 1. Evidence (owner's machine, read-only `du`/`find`, 2026-10-24, VERIFIED)

| Item | Size |
|---|---|
| `%LOCALAPPDATA%\Docker` | 58 GiB, of which `wsl\disk\docker_data.vhdx` is **57.08 GiB** (`wsl\main\ext4.vhdx` 0.10 GiB) |
| `%LOCALAPPDATA%\wsl\{a0df38c4-...}\ext4.vhdx` | **27.42 GiB** (one WSL distribution) |

That is about 85 GiB, the largest lever after the 185 GiB Hugging Face move (relocate spec, section 1).

The Docker image is growing: GAPS-2026-10 recorded `docker_data.vhdx` at 50.60 GB (2026-10-07, itself +34.5 GB since the 2026-10-02 compaction), and it is 57.08 GiB now. GAPS does not say whether its GB is decimal, so the growth over those 17 days is 6.5-10.0 GiB, about 2.7-4.1 GiB per week (BELIEVED: two readings from different tools).

Relocate classifies both as GUIDED (`relocate.default_policy`). It listed them at 0 bytes, because `classify()` returned before sizing a guided folder. Fixed on `feat/relocate` (`eba2f55`, PR #202): guided rows are sized read-only from their files, without opening them.

## 2. Why relocate's junction move is the wrong tool here

- **Docker Desktop** stores its data in a VHDX that its WSL backend mounts. It has its own setting for the location (Settings > Resources > Advanced > Disk image location), which moves the image and updates its own configuration. A junction under a running backend risks a corrupt image, and an update could replace the folder (both BELIEVED: not tested, and no Docker documentation was checked for them).
- **A WSL distribution** is registered by path in the user's registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss`). It is moved with `wsl --export` / `--unregister` / `--import`. Newer WSL releases also offer `wsl --manage <name> --move <folder>` (BELIEVED: not checked on this machine's `wsl --version`). `--unregister` deletes the distribution's disk irreversibly.

## 3. Phases

**Phase 0 (a fix, not a feature):** size guided rows read-only in `relocate list` and on the page. Show the steps beside the real size. Done on `feat/relocate` (`eba2f55`).

**Phase 1, guided (read-only, recommended next):**
- Detect Docker Desktop and every WSL distribution: `wsl --list --verbose` is read-only, and the registry key is read, never written.
- Show each one's VHDX path, size and state (running or stopped).
- Show the exact steps with the user's own names filled in. For Docker, open Docker Desktop's settings page; never edit its configuration.
- After the user acts, re-detect. An image now on another drive shows as "already moved".

**Phase 2, product-run WSL move (needs the owner's decision):** only for user distributions, never `docker-desktop` or `docker-desktop-data`.
1. **Preconditions, each refusing:**
   - the distribution is stopped (`wsl --terminate <name>`, then `wsl --list --verbose` shows Stopped);
   - no `wsl.exe` or `vmmem` work is attributable to it;
   - the target is a local fixed NTFS drive;
   - the target has free space of at least 2 x the VHDX size: the tar plus one imported disk. The temporary import in step 2 is unregistered before step 4, so the two imports never coexist (if a later design keeps both, this becomes 3 x);
   - the journal drive has room;
   - the name is not excluded.
2. **Prefer `wsl --manage <name> --move <folder>`** where the installed WSL has it: one program-owned step.
3. **Otherwise, a journaled sequence like relocate's.** WSL has no rename for a distribution, so the copy that is proven to start must be the one imported under the original name:
   1. with the distribution Stopped (precondition above, re-checked here), export to a tar on the target; record the tar's size and SHA-256, and check it is readable;
   2. import the tar under a temporary name into a scratch folder on the target, prove it starts (`wsl -d <temp> -- true`), terminate it, then unregister the temporary one. This proves the tar, not the final copy;
   3. re-check the tar's size and SHA-256 against step 1, then unregister the original. This is the one irreversible step, and the proven tar is on disk when it runs;
   4. import the tar under the original name into the target folder;
   5. prove it starts, and that the default user from `/etc/wsl.conf` survives (BELIEVED: an import resets the default user otherwise; not tested here);
   6. if step 4 or 5 fails: when a distribution is registered under the original name (a failed step 5, or a step 4 that registered before failing), terminate and unregister that failed copy first, because `--import` refuses a name that is taken. Then import the tar back to the old location under the original name, prove it starts, and report. The proven tar is on disk throughout, so nothing is lost;
   7. keep the tar until the user confirms, with the same keep period and `delete-old` step as relocate's old copy.

   Every step is recorded in the journal so that `recover` can finish or undo it.
4. **Docker** stays guided in every phase. The product never moves Docker's image itself.

## 4. Eval design

- **Unit level:** every phase-2 step runs through an injected `wsl` runner (no real `wsl.exe` in tests, the same pattern as the schtasks fakes). Kill sweeps at each journal write, then `recover`. The gate: no state where neither the original distribution nor a verified tar exists.
- **Real machine (owner approval needed):**
  - rehearse on a throwaway distribution imported from a tiny root filesystem, never on the owner's real one first;
  - measure the wall time and the C: free-space delta;
  - check that the distribution starts and keeps its default user.
- **Reported numbers:** GB moved, C: delta, minutes, and the start check's result.

## 5. Not in scope

Moving Docker's image by the product. Hyper-V VMs. Any registry write in phase 1.
