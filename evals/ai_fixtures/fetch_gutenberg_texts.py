from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

# Reproducible downloader for a handful of Project Gutenberg public-domain texts (ADR-0017) —
# the real-content source for Feature 1b's realistic document near-dup distribution, same role
# fetch_copydays.py plays for Feature 1a's Copydays download. All 8 works listed below were
# first published before 1928 and are unambiguously public domain (zero licensing risk,
# unlike QQP's non-commercial restriction or PAWS's adversarial-by-construction framing — see
# ADR-0017 for the full dataset-selection reasoning). Never committed (see .gitignore's
# `data/ai_datasets/`) — re-downloaded on demand.

_GUTENBERG_IDS: tuple[int, ...] = (
    1342,  # Pride and Prejudice — Jane Austen
    84,  # Frankenstein — Mary Shelley
    11,  # Alice's Adventures in Wonderland — Lewis Carroll
    1661,  # The Adventures of Sherlock Holmes — Arthur Conan Doyle
    2701,  # Moby Dick — Herman Melville
    98,  # A Tale of Two Cities — Charles Dickens
    76,  # Adventures of Huckleberry Finn — Mark Twain
    345,  # Dracula — Bram Stoker
)
_DEFAULT_ROOT = Path("data/ai_datasets/gutenberg_texts")

_START_MARKERS = (
    "*** START OF THE PROJECT GUTENBERG EBOOK",
    "*** START OF THIS PROJECT GUTENBERG EBOOK",
)
_END_MARKERS = (
    "*** END OF THE PROJECT GUTENBERG EBOOK",
    "*** END OF THIS PROJECT GUTENBERG EBOOK",
)


def _strip_gutenberg_boilerplate(raw_text: str) -> str:
    """Removes Gutenberg's standard license header/footer, keeping only the actual work —
    the boilerplate is identical across every Gutenberg file and would otherwise become a
    spurious, artificial source of "near-duplicate" text between otherwise-unrelated books."""
    text = raw_text
    for marker in _START_MARKERS:
        index = text.find(marker)
        if index != -1:
            newline_index = text.find("\n", index)
            text = text[newline_index + 1 :] if newline_index != -1 else text[index:]
            break
    for marker in _END_MARKERS:
        index = text.find(marker)
        if index != -1:
            text = text[:index]
            break
    return text.strip()


def fetch_gutenberg_texts(root: Path = _DEFAULT_ROOT, *, force: bool = False) -> Path:
    raw_dir = root / "raw"
    cleaned_dir = root / "cleaned"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cleaned_dir.mkdir(parents=True, exist_ok=True)

    for gutenberg_id in _GUTENBERG_IDS:
        cleaned_path = cleaned_dir / f"{gutenberg_id}.txt"
        if cleaned_path.exists() and not force:
            continue

        raw_path = raw_dir / f"{gutenberg_id}.txt"
        if not raw_path.exists() or force:
            url = f"https://www.gutenberg.org/cache/epub/{gutenberg_id}/pg{gutenberg_id}.txt"
            print(f"fetch_gutenberg_texts: downloading {url}")  # noqa: T201
            urllib.request.urlretrieve(url, raw_path)  # noqa: S310 -- fixed, hardcoded HTTPS URL

        raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
        cleaned_path.write_text(_strip_gutenberg_boilerplate(raw_text), encoding="utf-8")

    return cleaned_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch public-domain Gutenberg texts.")
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    cleaned_dir = fetch_gutenberg_texts(args.root, force=args.force)
    files = list(cleaned_dir.glob("*.txt"))
    total_chars = sum(f.stat().st_size for f in files)
    print(f"fetch_gutenberg_texts: {len(files)} texts, {total_chars:,} bytes at {cleaned_dir}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
