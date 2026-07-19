from __future__ import annotations

import argparse
import hashlib
import tarfile
import urllib.request
from pathlib import Path

# Reproducible downloader for the public INRIA Copydays dataset (ADR-0015) — the real,
# human-construction-verified gold set Feature 1a Track A's operating point is measured
# against (evals/test_ai_copydays_gold.py). Never committed (see .gitignore's
# `data/ai_datasets/`) — this script re-derives it on demand, same posture as
# build_image_similarity_fixtures.py re-deriving synthetic fixtures on demand, except this
# fetches real bytes instead of generating them.
#
# The dataset's original host (pascal.inrialpes.fr, the URL cited by the still-live
# description page at thoth.inrialpes.fr/~jegou/data.php.html) is unreachable as of this
# writing (TCP connect timeout, not a DNS or auth failure — the legacy INRIA server appears to
# be down). Meta/FAIR re-hosts the identical dataset (same INRIA copyright, same citation
# requirement — see ADR-0015) at a stable CDN URL for their own published copy-detection
# research (facebookresearch/sscd-copy-detection, facebookresearch/vissl). This script uses
# that mirror. The FAIR mirror does NOT carry the `copydays_jpeg` (graduated JPEG-quality
# ladder) or `copydays_crop` splits that the original host advertises — only `original` and
# `strong` were found reachable; see ADR-0015's "Consequences" for what that gap means for
# threshold generalization.

_MIRROR_BASE = "https://dl.fbaipublicfiles.com/vissl/datasets"
_FILES: dict[str, str] = {
    "copydays_original.tar.gz": (
        "8b85b7914fd0145bc258bde3440f6a622243ca91ee085fe2025ce7bdeb0bfb8e"
    ),
    "copydays_strong.tar.gz": ("7852c64aa4a37e8329ce08159fc947c16f09c66737db0a2479a90fc9ae40760c"),
}
_DEFAULT_ROOT = Path("data/ai_datasets/copydays")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_copydays(root: Path = _DEFAULT_ROOT, *, force: bool = False) -> Path:
    """Downloads + extracts Copydays into `root/extracted/`, returning that path. Idempotent:
    skips re-download when the file already exists with a matching checksum, skips
    re-extraction when `extracted/` is already populated — safe to call every eval run."""
    raw_dir = root / "raw"
    extracted_dir = root / "extracted"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for filename, expected_sha256 in _FILES.items():
        archive_path = raw_dir / filename
        if archive_path.exists() and not force:
            if _sha256(archive_path) == expected_sha256:
                continue
            print(f"fetch_copydays: {filename} checksum mismatch, re-downloading")  # noqa: T201

        url = f"{_MIRROR_BASE}/{filename}"
        print(f"fetch_copydays: downloading {url}")  # noqa: T201
        urllib.request.urlretrieve(url, archive_path)  # noqa: S310 -- fixed, hardcoded HTTPS URL

        actual_sha256 = _sha256(archive_path)
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"fetch_copydays: {filename} checksum mismatch after download — "
                f"expected {expected_sha256}, got {actual_sha256}. Refusing to extract "
                "unverified data; the mirror may have changed or the download was corrupted."
            )

    if extracted_dir.exists() and any(extracted_dir.glob("*.jpg")) and not force:
        return extracted_dir
    extracted_dir.mkdir(parents=True, exist_ok=True)
    for filename in _FILES:
        with tarfile.open(raw_dir / filename, "r:gz") as tar:
            tar.extractall(extracted_dir, filter="data")  # checksum-verified above
    return extracted_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the INRIA Copydays gold-set dataset.")
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    parser.add_argument("--force", action="store_true", help="Re-download and re-extract.")
    args = parser.parse_args(argv)

    extracted = fetch_copydays(args.root, force=args.force)
    n_images = len(list(extracted.glob("*.jpg")))
    print(f"fetch_copydays: {n_images} images available at {extracted}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
