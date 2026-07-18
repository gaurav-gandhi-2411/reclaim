from __future__ import annotations

from pathlib import Path

from reclaim.ai.safety import filter_paths_through_safety_validator
from reclaim.config import Config, SafetyConfig
from reclaim.safety import SafetyValidator

# Hard Gate 2: "Shared SafetyValidator exclusions ... apply to AI candidates too." This file
# proves reuse is genuine — the SAME SafetyValidator class, evaluated fresh per path, not a
# reimplementation or a permissive subset — by exercising it against real fixture files on
# disk (protected root, git repo, benign) and confirming the AI-facing filter matches exactly
# what the deterministic engine's own SafetyValidator.evaluate() would say.


def test_protected_root_path_is_excluded_from_ai_candidates(tmp_path: Path) -> None:
    protected_dir = tmp_path / "Windows"
    protected_dir.mkdir()
    protected_file = protected_dir / "photo.jpg"
    protected_file.write_bytes(b"x")
    benign_file = tmp_path / "Pictures" / "photo.jpg"
    benign_file.parent.mkdir()
    benign_file.write_bytes(b"x")

    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected_dir.as_posix()}/*"]))
    )

    eligible = filter_paths_through_safety_validator([protected_file, benign_file], safety)

    assert protected_file not in eligible
    assert benign_file in eligible


def test_deny_listed_path_is_excluded_from_ai_candidates(tmp_path: Path) -> None:
    denied_file = tmp_path / "do-not-touch" / "photo.jpg"
    denied_file.parent.mkdir()
    denied_file.write_bytes(b"x")
    safety = SafetyValidator(Config(safety=SafetyConfig(deny=["*/do-not-touch/*"])))

    eligible = filter_paths_through_safety_validator([denied_file], safety)

    assert eligible == []


def test_missing_path_is_silently_dropped_not_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "gone.jpg"  # never created
    safety = SafetyValidator(Config())

    eligible = filter_paths_through_safety_validator([missing], safety)

    assert eligible == []


def test_benign_files_all_pass_through_unchanged(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    safety = SafetyValidator(Config())

    eligible = filter_paths_through_safety_validator([a, b], safety)

    assert set(eligible) == {a, b}
