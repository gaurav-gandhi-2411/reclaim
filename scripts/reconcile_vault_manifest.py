from __future__ import annotations

# reconcile_vault_manifest.py -- K2c (audit finding, this session): real, runnable reconciliation
# of the quarantine vault manifest against actual on-disk state -- confirms every entry the
# manifest claims is "currently in the vault" (method="vault", not restored, not purged) really
# has a file/directory sitting at its recorded vault_path. This is the single most important
# check this session's investigation produces: if it fails, `reclaim undo` is already broken for
# real user data, since a manifest entry with no real vault copy behind it cannot be restored no
# matter what `restore_batch` does.
#
# Also reconciles Track A (Phase 2)'s separate, ad-hoc manual quarantine folder
# (`reclaim-emergency-quarantine-<timestamp>\`, NOT this tool's own vault -- a plain TSV-manifest
# folder created by hand during an earlier phase of this engagement, with no automatic
# reconciliation of any kind today) -- see `_reconcile_tsv_manifest` for the format and the
# path-mapping convention this script had to reverse-engineer and verify empirically, since the
# TSV format records no vault_path at all, only the original path.
#
# Usage:
#   uv run python scripts/reconcile_vault_manifest.py
#   uv run python scripts/reconcile_vault_manifest.py --manifest PATH --vault-dir PATH
#   uv run python scripts/reconcile_vault_manifest.py --emergency-quarantine-dir PATH
#   uv run python scripts/reconcile_vault_manifest.py --no-emergency-quarantine
import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from reclaim.executor import DEFAULT_MANIFEST_PATH, fold_latest_manifest_entries


@dataclass(frozen=True, slots=True)
class UnreconciledVaultEntry:
    """One `manifest.jsonl` entry the real filesystem does NOT back up -- the manifest claims
    this item is safely quarantined and restorable, but no real vault copy was found."""

    original_path: Path
    vault_path: Path | None
    batch_id: str
    reason: str


def reconcile_vault_manifest(
    manifest_path: Path,
) -> tuple[int, list[UnreconciledVaultEntry]]:
    """K2c: for every entry `fold_latest_manifest_entries` reports as CURRENTLY vaulted (
    `method == "vault"`, `restored is False`, `purged is False` -- the same predicate
    `purge.purge_eligible_entries` uses to mean "still really sitting in the vault"), verifies a
    real file or directory exists at its recorded `vault_path`. Uses the manifest's own public
    read/fold functions (`fold_latest_manifest_entries`), never hand-parses `manifest.jsonl`.

    Returns `(checked_count, unreconciled_entries)` -- `checked_count` is the real number of
    currently-vaulted entries this reconciliation actually examined, never an estimate.
    """
    entries = fold_latest_manifest_entries(manifest_path)
    vault_entries = [
        entry
        for entry in entries
        if entry.method == "vault" and not entry.restored and not entry.purged
    ]
    unreconciled: list[UnreconciledVaultEntry] = []
    for entry in vault_entries:
        if entry.vault_path is None:
            unreconciled.append(
                UnreconciledVaultEntry(
                    original_path=entry.original_path,
                    vault_path=None,
                    batch_id=entry.batch_id,
                    reason="manifest entry has method=vault but no vault_path recorded",
                )
            )
            continue
        if not entry.vault_path.exists():
            unreconciled.append(
                UnreconciledVaultEntry(
                    original_path=entry.original_path,
                    vault_path=entry.vault_path,
                    batch_id=entry.batch_id,
                    reason=f"nothing exists on disk at the recorded vault_path {entry.vault_path}",
                )
            )
    return len(vault_entries), unreconciled


@dataclass(frozen=True, slots=True)
class UnreconciledTsvEntry:
    """One Track-A TSV manifest line this reconciliation could not confirm is safe."""

    original_path: Path
    expected_vault_path: Path | None
    reason: str


def _reconcile_tsv_manifest(
    tsv_path: Path, *, source_root: Path | None, vault_subdir: Path
) -> tuple[int, int, list[UnreconciledTsvEntry], list[UnreconciledTsvEntry]]:
    r"""Reconciles one of Track A's ad-hoc `<Category>_manifest.tsv` files (no header row;
    tab-separated `STATUS\tSIZE_BYTES\tORIGINAL_PATH` per line). Real statuses observed in this
    machine's own files (verified via a direct `Group-Object` count before writing this function,
    not assumed): `MOVED` (quarantined -- the only status this function reconciles), and
    `SKIP_LOCKED`/`SKIP` (the quarantine script deliberately left the item in place, e.g. a file
    held open by another process -- there is no "moved" claim to verify for these, so they are
    counted separately, never reported as an unreconciled failure).

    Unlike `reclaim.executor`'s own `manifest.jsonl`, this ad-hoc format records NO vault_path at
    all -- only the original path survives. The vault destination must be DERIVED, using the
    directory-mirroring convention this specific quarantine run actually used -- verified
    empirically against real files in the real folder on this machine before this function was
    written (see this session's PLAN.md/report), not assumed from the format alone:
      - `source_root=None` (the AppCaches-shaped manifest): each original path is rooted under
        its own drive letter (e.g. `C:\`), and the vault mirrors the FULL path under that drive
        (`vault_subdir / original_path.relative_to(original_path.anchor)`).
      - `source_root=<a specific directory>` (the TEMP-shaped manifest): every original path was
        under one common root (`%TEMP%`), and the vault mirrors the path RELATIVE TO that root,
        directly under `vault_subdir` (no drive/user-path prefix).

    A mapping that turns out wrong for some `MOVED` entry is reported as a genuine reconciliation
    failure, not silently swallowed or distinguished from "the file is really missing" -- this
    function draws no such distinction; either way means this reconciliation could not confirm
    the item is safe, and that must be visible in the report, not quietly explained away.

    An `original_path` that has been REOCCUPIED since quarantine (a new, unrelated file/directory
    now sits there) but whose expected vault location genuinely holds a real copy is reported
    separately from a true reconciliation failure -- the original quarantine move demonstrably
    succeeded (the vault copy is real and present); something else created a NEW entry at the old
    path afterward, which is not this reconciliation's concern and not evidence of data loss. Both
    lists are still returned in full, in full detail, never summarized away -- the distinction is
    which bucket each finding belongs in, not whether it gets reported at all.

    Returns `(moved_checked, skipped_non_moved_count, unreconciled_entries,
    reoccupied_but_vault_intact_entries)`.

    `encoding="utf-8-sig"` (not plain `"utf-8"`): this phase's own quarantine script wrote these
    files WITH a UTF-8 byte-order mark -- verified directly against the real file's first bytes
    on this machine (`EF BB BF`) -- which plain `"utf-8"` decoding leaves attached to the first
    line's `STATUS` field (`"\ufeffMOVED"` != `"MOVED"`), silently misclassifying line 1 of every
    such file as an unrecognized status. `"utf-8-sig"` strips it correctly.
    """
    if not tsv_path.exists():
        return 0, 0, [], []
    checked = 0
    skipped_non_moved = 0
    unreconciled: list[UnreconciledTsvEntry] = []
    reoccupied_but_vault_intact: list[UnreconciledTsvEntry] = []
    for raw_line in tsv_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            unreconciled.append(
                UnreconciledTsvEntry(
                    original_path=Path(line),
                    expected_vault_path=None,
                    reason=f"malformed TSV line (expected 3 tab-separated fields): {line!r}",
                )
            )
            continue
        status, _size_bytes, original_path_str = parts[0], parts[1], parts[2]
        if status != "MOVED":
            # A deliberate, expected outcome of the original quarantine run (e.g. a file held
            # open elsewhere, correctly left untouched) -- no "moved" claim was ever made for
            # this line, so there is nothing here for this reconciliation to verify or fail.
            skipped_non_moved += 1
            continue
        original_path = Path(original_path_str)
        checked += 1

        root = source_root if source_root is not None else Path(original_path.anchor)
        try:
            relative = original_path.relative_to(root)
        except ValueError:
            unreconciled.append(
                UnreconciledTsvEntry(
                    original_path=original_path,
                    expected_vault_path=None,
                    reason=f"original_path is not under the expected source root {root} -- "
                    "cannot derive an expected vault location for it",
                )
            )
            continue
        expected_vault_path = vault_subdir / relative

        if original_path.exists():
            if expected_vault_path.exists():
                # The original quarantine move genuinely succeeded (a real vault copy is
                # present); something else has since created a NEW, unrelated entry at the old
                # path -- expected for a transient/regenerating file (a lock file, an app cache)
                # in a folder that has kept being used since the one-time quarantine sweep, not a
                # sign the quarantine or the vault copy is unsafe.
                reoccupied_but_vault_intact.append(
                    UnreconciledTsvEntry(
                        original_path=original_path,
                        expected_vault_path=expected_vault_path,
                        reason="original_path exists again, but a real vault copy IS present at "
                        "the expected location -- the original item was genuinely quarantined; "
                        "something (re)created a new entry at the old path since",
                    )
                )
            else:
                unreconciled.append(
                    UnreconciledTsvEntry(
                        original_path=original_path,
                        expected_vault_path=expected_vault_path,
                        reason="original_path still exists on disk AND no vault copy exists at "
                        "the expected location -- the TSV claims MOVED but nothing was found at "
                        "either location",
                    )
                )
            continue
        if not expected_vault_path.exists():
            unreconciled.append(
                UnreconciledTsvEntry(
                    original_path=original_path,
                    expected_vault_path=expected_vault_path,
                    reason="neither the original_path nor the expected vault location "
                    f"{expected_vault_path} exist -- the item may be genuinely lost, or this "
                    "script's path-mapping guess for this ad-hoc format may be wrong for this "
                    "specific entry",
                )
            )
    return checked, skipped_non_moved, unreconciled, reoccupied_but_vault_intact


def _find_emergency_quarantine_dir() -> Path | None:
    """Locates Track A's `reclaim-emergency-quarantine-<timestamp>` folder under the user's home
    directory, if one still exists on this machine -- picks the most recently modified match if
    more than one is present (a prior session could have run this more than once)."""
    home = Path("~").expanduser()
    candidates = sorted(
        home.glob("reclaim-emergency-quarantine-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _print_vault_report(checked: int, unreconciled: list[UnreconciledVaultEntry]) -> None:
    print(
        f"reclaim.manifest.jsonl vault reconciliation: {checked} currently-vaulted entr"
        f"{'y' if checked == 1 else 'ies'} checked"
    )
    if not unreconciled:
        print(
            f"  -> 100% reconciled: all {checked} vault entries have a real file/directory "
            "at their recorded vault_path."
        )
        return
    print(f"  -> {len(unreconciled)} of {checked} did NOT reconcile:")
    for item in unreconciled:
        print(
            f"     - batch={item.batch_id} original_path={item.original_path} "
            f"vault_path={item.vault_path}: {item.reason}"
        )


def _print_tsv_report(
    label: str,
    checked: int,
    skipped_non_moved: int,
    unreconciled: list[UnreconciledTsvEntry],
    reoccupied_but_vault_intact: list[UnreconciledTsvEntry],
) -> None:
    print(
        f"{label}: {checked} 'MOVED' entr{'y' if checked == 1 else 'ies'} checked "
        f"({skipped_non_moved} non-MOVED entries -- e.g. SKIP_LOCKED -- excluded, not applicable)"
    )
    if checked == 0:
        print("  -> file not found or empty; nothing to reconcile.")
        return
    if reoccupied_but_vault_intact:
        print(
            f"  -> {len(reoccupied_but_vault_intact)} entries: original_path reoccupied since "
            "quarantine, but a real vault copy IS present (genuinely quarantined; something "
            "else since recreated a new entry at the old path -- not a data-safety concern):"
        )
        for item in reoccupied_but_vault_intact:
            print(f"     - {item.original_path} (vault copy: {item.expected_vault_path})")
    if not unreconciled:
        remaining = checked - len(reoccupied_but_vault_intact)
        print(
            f"  -> 100% reconciled for the remaining {remaining} entries: each has a real vault "
            "copy at its expected (derived) location and its original_path is genuinely gone."
        )
        return
    print(f"  -> {len(unreconciled)} of {checked} did NOT reconcile:")
    for item in unreconciled:
        print(
            f"     - original_path={item.original_path} "
            f"expected_vault_path={item.expected_vault_path}: {item.reason}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--emergency-quarantine-dir", type=Path, default=None)
    parser.add_argument(
        "--no-emergency-quarantine",
        action="store_true",
        help="Skip Track A's ad-hoc emergency-quarantine TSV reconciliation entirely.",
    )
    args = parser.parse_args()

    exit_code = 0

    checked, unreconciled = reconcile_vault_manifest(args.manifest)
    _print_vault_report(checked, unreconciled)
    if unreconciled:
        exit_code = 1

    print()

    if not args.no_emergency_quarantine:
        quarantine_dir = args.emergency_quarantine_dir or _find_emergency_quarantine_dir()
        if quarantine_dir is None or not quarantine_dir.exists():
            print("Track A emergency-quarantine folder: none found on this machine.")
        else:
            print(f"Track A emergency-quarantine folder: {quarantine_dir}")
            temp_root = Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp")))
            temp_checked, temp_skipped, temp_unreconciled, temp_reoccupied = (
                _reconcile_tsv_manifest(
                    quarantine_dir / "TEMP_manifest.tsv",
                    source_root=temp_root,
                    vault_subdir=quarantine_dir / "TEMP",
                )
            )
            _print_tsv_report(
                "  TEMP_manifest.tsv",
                temp_checked,
                temp_skipped,
                temp_unreconciled,
                temp_reoccupied,
            )
            if temp_unreconciled:
                exit_code = 1

            appcaches_checked, appcaches_skipped, appcaches_unreconciled, appcaches_reoccupied = (
                _reconcile_tsv_manifest(
                    quarantine_dir / "AppCaches_manifest.tsv",
                    source_root=None,
                    vault_subdir=quarantine_dir / "AppCaches",
                )
            )
            _print_tsv_report(
                "  AppCaches_manifest.tsv",
                appcaches_checked,
                appcaches_skipped,
                appcaches_unreconciled,
                appcaches_reoccupied,
            )
            if appcaches_unreconciled:
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
