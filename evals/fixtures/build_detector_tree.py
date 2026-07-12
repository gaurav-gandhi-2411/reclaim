from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_SECONDS_PER_DAY = 86400.0
# Above the spec's default 50MB large-log threshold; well above so the assertion isn't
# borderline-sensitive to the exact threshold value.
_LARGE_LOG_SIZE_BYTES = 60 * 1024 * 1024


def _write_file(path: Path, *, size_bytes: int, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"reclaim-detector-fixture\n")
    os.truncate(path, size_bytes)  # sparse — cheap even for the 60MB log fixtures
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@dataclass(frozen=True, slots=True)
class DetectorFixtureTree:
    """Key paths from the materialized Stage 3 detector fixture tree, for eval assertions."""

    root: Path
    node_modules_with_manifest_dir: Path
    node_modules_without_manifest_dir: Path
    archive_zip: Path
    extracted_dir: Path
    extracted_dir_file: Path
    old_installer: Path
    recent_installer: Path
    old_log: Path
    recent_log: Path
    blocked_node_modules_dir: Path


def build_detector_fixture_tree(root: Path, *, now: float) -> DetectorFixtureTree:
    """Materializes a real filesystem tree under `root` covering every Stage 3 CI-gate case:
    node_modules with/without an adjacent manifest, an archive+extracted-directory pair, an
    old/recent installer pair under Downloads, an old/recent large-log pair, and a dev-artifact
    directory that SafetyValidator blocks outright (sitting inside a protected root) despite
    having a valid adjacent manifest — proving the safety boundary excludes it end-to-end.

    Never touches anything outside `root`.
    """
    node_modules_with_manifest_dir = root / "Project" / "node_modules"
    _write_file(root / "Project" / "package.json", size_bytes=64, mtime=now)
    _write_file(node_modules_with_manifest_dir / "pkg" / "index.js", size_bytes=128, mtime=now)

    node_modules_without_manifest_dir = root / "NoManifestProject" / "node_modules"
    _write_file(node_modules_without_manifest_dir / "pkg" / "index.js", size_bytes=128, mtime=now)

    archive_zip = root / "Archives" / "photos.zip"
    _write_file(archive_zip, size_bytes=2048, mtime=now)
    extracted_dir = root / "Archives" / "photos"
    extracted_dir_file = extracted_dir / "img1.jpg"
    _write_file(extracted_dir_file, size_bytes=4096, mtime=now)

    old_installer = root / "Downloads" / "old_installer.exe"
    _write_file(old_installer, size_bytes=51200, mtime=now - 120 * _SECONDS_PER_DAY)
    recent_installer = root / "Downloads" / "recent_installer.exe"
    _write_file(recent_installer, size_bytes=51200, mtime=now - 10 * _SECONDS_PER_DAY)

    old_log = root / "Logs" / "old_big.log"
    _write_file(old_log, size_bytes=_LARGE_LOG_SIZE_BYTES, mtime=now - 45 * _SECONDS_PER_DAY)
    recent_log = root / "Logs" / "recent_big.log"
    _write_file(recent_log, size_bytes=_LARGE_LOG_SIZE_BYTES, mtime=now - 2 * _SECONDS_PER_DAY)

    blocked_node_modules_dir = root / "Windows" / "System32" / "SomeApp" / "node_modules"
    _write_file(
        root / "Windows" / "System32" / "SomeApp" / "package.json", size_bytes=64, mtime=now
    )
    _write_file(blocked_node_modules_dir / "pkg" / "index.js", size_bytes=128, mtime=now)

    return DetectorFixtureTree(
        root=root,
        node_modules_with_manifest_dir=node_modules_with_manifest_dir,
        node_modules_without_manifest_dir=node_modules_without_manifest_dir,
        archive_zip=archive_zip,
        extracted_dir=extracted_dir,
        extracted_dir_file=extracted_dir_file,
        old_installer=old_installer,
        recent_installer=recent_installer,
        old_log=old_log,
        recent_log=recent_log,
        blocked_node_modules_dir=blocked_node_modules_dir,
    )
