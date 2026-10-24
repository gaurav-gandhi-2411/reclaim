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

Relocate classifies both as GUIDED (`relocate.default_policy`) and lists them at 0 bytes, because `classify()` returns before sizing a guided folder. That is a defect, logged for the relocate branch: the list must show the real size, read-only.

## 2. Why relocate's junction move is the wrong tool here

- **Docker Desktop** stores its data in a VHDX that its WSL backend mounts. It has its own setting for the location (Settings > Resources > Advanced > Disk image location), which moves the image and updates its own configuration. A junction under a running backend risks a corrupt image, and an update could replace the folder.
- **A WSL distribution** is registered by path in the user's registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss`). It is moved with `wsl --export` / `--unregister` / `--import`. Newer WSL releases also offer `wsl --manage <name> --move <folder>` (BELIEVED: not checked on this machine's `wsl --version`). `--unregister` deletes the distribution's disk irreversibly.

## 3. Phases

**Phase 0 (a fix, not a feature):** size guided rows read-only in `relocate list` and on the page. Show the steps beside the real size.

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
   - the target has free space of at least 2 x the VHDX size, for the export file plus the new disk;
   - the journal drive has room;
   - the name is not excluded.
2. **Prefer `wsl --manage <name> --move <folder>`** where the installed WSL has it: one program-owned step.
3. **Otherwise, a journaled sequence like relocate's:**
   - export to a tar on the target;
   - verify the tar is readable and its size is plausible;
   - import under a temporary name into the target folder;
   - prove it starts (`wsl -d <temp> -- true`) and that `/etc/wsl.conf`'s default user survives (an import resets it otherwise);
   - only then unregister the original;
   - re-import or rename to the original name;
   - keep the tar until the user confirms, with the same keep period and `delete-old` step as relocate's old copy.

   Every step is recorded in the journal so that `recover` can finish or undo it. Unregistering the original is the one irreversible step; it runs last, only after the new copy has started, and the tar is still on disk then.
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
