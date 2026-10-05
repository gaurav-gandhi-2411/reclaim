from __future__ import annotations

# The ONLY sanctioned way for a harness, benchmark or agent to work on a COPY of a real Reclaim
# index. A real index is ~5 GB; SQLite adds a -wal (and -shm) the moment anything writes to a copy
# (an ANALYZE alone grew one), and a VACUUM needs ~1x the file size again. Copies made with a bare
# `Copy-Item`/`shutil.copy` were never cleaned up and are the suspected source of recurring
# multi-GB C: drops (BELIEVED; the 2026-10-05 sweep found one 4.9 GB copy, left by the session that
# measured the ANALYZE regression, and none from earlier sessions that were still on disk).
#
# This helper (1) refuses to copy unless free space covers `min_free_multiple` x the source size,
# (2) copies, (3) ALWAYS removes the copy and its -wal/-shm/-journal in `finally` -- on success,
# error and KeyboardInterrupt -- and (4) removes the scratch directory only if it created it.
#
# Library:  with scratch_index_copy(src) as db: ...            (db is a Path to the copy)
# CLI:      python scripts/scratch_index.py SRC [--dest-dir D] -- cmd arg {DB} ...
#           ({DB} in the command is replaced by the copy's path; exit code is the command's)
import argparse
import contextlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# 2x: the copy itself plus headroom for the WAL/VACUUM/ANALYZE temp space it will grow by.
DEFAULT_MIN_FREE_MULTIPLE = 2.0


class InsufficientSpaceError(RuntimeError):
    """Free space on the destination volume is below the required multiple of the index size."""


def _remove_copy(db: Path) -> None:
    for candidate in (db, *(db.with_name(db.name + s) for s in _SIDECAR_SUFFIXES)):
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()


@contextlib.contextmanager
def scratch_index_copy(
    src: Path,
    *,
    dest_dir: Path | None = None,
    min_free_multiple: float = DEFAULT_MIN_FREE_MULTIPLE,
) -> Iterator[Path]:
    """Yields the path of a private copy of `src`; deletes it (and sidecars) on exit, always."""
    if not src.is_file():
        raise FileNotFoundError(f"index not found: {src}")
    created_dir = dest_dir is None or not dest_dir.exists()
    base = Path(tempfile.mkdtemp(prefix="reclaim-scratch-index-")) if dest_dir is None else dest_dir
    base.mkdir(parents=True, exist_ok=True)
    db = base / "index.sqlite3"
    try:
        needed = int(src.stat().st_size * min_free_multiple)
        free = shutil.disk_usage(base).free
        if free < needed:
            raise InsufficientSpaceError(
                f"refusing to copy {src.stat().st_size / 1e9:.2f} GB: {free / 1e9:.2f} GB free on "
                f"{base.anchor or base}, need {needed / 1e9:.2f} GB (x{min_free_multiple})"
            )
        shutil.copyfile(src, db)
        yield db
    finally:
        _remove_copy(db)
        if created_dir:
            with contextlib.suppress(OSError):  # non-empty (foreign files) -> leave, never rmtree
                base.rmdir()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path)
    parser.add_argument("--dest-dir", type=Path, default=None)
    parser.add_argument("--min-free-multiple", type=float, default=DEFAULT_MIN_FREE_MULTIPLE)
    raw = list(sys.argv[1:] if argv is None else argv)
    # Split on the first `--` ourselves: argparse.REMAINDER swallows options that follow the
    # first positional, which would eat `--dest-dir`.
    own, command = (
        (raw[: raw.index("--")], raw[raw.index("--") + 1 :]) if "--" in raw else (raw, [])
    )
    args = parser.parse_args(own)
    if not command:
        parser.error("give a command after `--`; use {DB} where the copy's path goes")
    with scratch_index_copy(
        args.src, dest_dir=args.dest_dir, min_free_multiple=args.min_free_multiple
    ) as db:
        resolved = [part.replace("{DB}", str(db)) for part in command]
        return subprocess.run(resolved, check=False).returncode  # noqa: S603 -- caller's command


if __name__ == "__main__":
    sys.exit(main())
