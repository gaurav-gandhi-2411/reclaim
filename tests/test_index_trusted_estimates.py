"""D5: the candidates WARM-UP trusts the scan index for the directory exclusion subtree check and
the hardlink-aware reclaimable estimate; apply keeps every live check.

Groups:
1. Parity -- on a fresh index the index-based exclusion check and estimate agree with the live
   ones (several layouts); the fail-closed cases (no row, `inaccessible_paths`) are refused.
2. Teeth -- a protected folder or a hardlink created AFTER the scan can make the index-derived
   candidate look fine, but `apply_batch` still refuses it. Each would fail if the matching
   apply-time live check were removed (mutants run by hand, see the PR description).
3. API -- an index-derived estimate is marked as such (per candidate and on every aggregate),
   and a real apply drops the warm cache so the figures are recomputed afterwards.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.config import (
    CategoriesConfig,
    Config,
    ExclusionsConfig,
    PackageCachesConfig,
    SafetyConfig,
)
from reclaim.detectors import (
    _reclaimable_bytes_for_candidate,
    _reclaimable_bytes_from_index,
    generate_candidates,
)
from reclaim.executor import SafetyInvariantError, apply_batch
from reclaim.index import InaccessibleEntry, ScanIndex
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode
from reclaim.models import Candidate, Mode, RawCandidate, Tier, Verdict
from reclaim.safety import (
    UNREADABLE_PREFIX,
    SafetyValidator,
    index_subtree_exclusion_match,
    subtree_exclusion_match,
)
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)

_NOW = 1_700_000_000.0
# Not a substring of any test name: pytest puts the test name into tmp_path, and a match on the
# root path itself would block every candidate for the wrong reason.
_PROJECT_NAME = "zqprotected"
_PATTERNS = (f"*{_PROJECT_NAME}*",)


def _config(cache: Path, *, exclusions: bool = True) -> Config:
    return Config(
        safety=SafetyConfig(protected_roots=[]),
        exclusions=ExclusionsConfig(project_names=[_PROJECT_NAME] if exclusions else []),
        categories=CategoriesConfig(
            package_caches=PackageCachesConfig(enabled=True, paths=[cache.as_posix()])
        ),
        mode=Mode.POWER,
    )


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _make_venv(venv: Path, *, module: str) -> Path:
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = C:/Python\n", encoding="utf-8")
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ-fake-interpreter")
    return _write(venv / "Lib" / "site-packages" / "pkg" / module, b"venv-own")


def _scanned(tmp_path: Path, root: Path) -> ScanIndex:
    (tmp_path / "db").mkdir(exist_ok=True)
    index = ScanIndex(tmp_path / "db" / "i.sqlite3")
    scan_tree(root, index, incremental=False)
    return index


def _package_cache(candidates: list[Candidate], cache: Path) -> Candidate:
    (found,) = [c for c in candidates if c.path == cache]
    return found


def _apply(
    candidate: Candidate, index: ScanIndex, tmp_path: Path, safety: SafetyValidator
) -> object:
    return apply_batch(
        [candidate],
        safety=safety,
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
        scan_index=index,
    )


# --- 1. parity with the live walk on a fresh index ---------------------------------------------


def _layout_clean(root: Path) -> None:
    _write(root / "a" / "b" / "c.bin", b"c")


def _layout_nested_dir_match(root: Path) -> None:
    _write(root / "a" / "b" / f"{_PROJECT_NAME}-run" / "x.bin", b"x")
    _write(root / "a" / "other.bin", b"o")


def _layout_file_match(root: Path) -> None:
    _write(root / "a" / f"notes-{_PROJECT_NAME}.txt", b"n")


def _layout_match_in_opaque_tree(root: Path) -> None:
    _write(root / "node_modules" / f"{_PROJECT_NAME}-pkg" / "i.js", b"j")
    _write(root / "plain.bin", b"p")


def _layout_junction_named_like_a_match(root: Path) -> None:
    import _winapi

    _write(root / "real" / "x.bin", b"x")
    _winapi.CreateJunction(str(root / "real"), str(root / f"{_PROJECT_NAME}-link"))


def _layout_junction_to_a_protected_target(root: Path) -> None:
    import _winapi

    _write(root.parent / f"{_PROJECT_NAME}-elsewhere" / "x.bin", b"x")
    root.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(root.parent / f"{_PROJECT_NAME}-elsewhere"), str(root / "innocent"))


def _layout_empty(root: Path) -> None:
    (root / "empty").mkdir(parents=True)


@pytest.mark.parametrize(
    "layout",
    [
        _layout_clean,
        _layout_nested_dir_match,
        _layout_file_match,
        _layout_match_in_opaque_tree,
        _layout_junction_named_like_a_match,
        _layout_junction_to_a_protected_target,
        _layout_empty,
    ],
)
@pytest.mark.parametrize(
    "patterns",
    [_PATTERNS, (f"*/{_PROJECT_NAME}-*/*",), (f"re:{_PROJECT_NAME}-",), ("*never-matches*",)],
)
def test_index_exclusion_check_agrees_with_the_live_walk_on_a_fresh_index(
    tmp_path: Path, layout: object, patterns: tuple[str, ...]
) -> None:
    root = tmp_path / "world" / "cache"
    layout(root)  # type: ignore[operator]
    with _scanned(tmp_path, root) as index:
        live = subtree_exclusion_match(root, patterns)
        from_index = index_subtree_exclusion_match(index, root, patterns)
    assert (live is None) == (from_index is None), (live, from_index)
    if live is not None:
        assert from_index == live


def test_index_exclusion_check_fails_closed_without_rows_or_with_inaccessible_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "world" / "cache"
    _layout_clean(root)
    with _scanned(tmp_path, root) as index:
        never_scanned = index_subtree_exclusion_match(index, tmp_path / "ghost", _PATTERNS)
        assert never_scanned is not None and never_scanned.startswith(UNREADABLE_PREFIX)
        assert index_subtree_exclusion_match(index, root, _PATTERNS) is None
        index.replace_inaccessible_under_root(
            root,
            [InaccessibleEntry(f"{root.as_posix()}/a/locked", "denied", None, True)],
            scanned_at=_NOW,
        )
        unreadable = index_subtree_exclusion_match(index, root, _PATTERNS)
        assert unreadable is not None and unreadable.startswith(UNREADABLE_PREFIX)
        # ...and the validator turns that into BLOCKED, so the candidate is not offered at all.
        assert (
            generate_candidates(
                index, _config(root), SafetyValidator(_config(root)), now=_NOW, trust_index=True
            )
            == []
        )


def test_index_estimate_equals_the_live_estimate_on_a_fresh_index(tmp_path: Path) -> None:
    world = tmp_path / "world"
    cache = world / "cache"
    _write(cache / "plain.bin", b"p" * 100)
    _write(cache / "d" / "plain2.bin", b"q" * 50)
    outside_linked = _write(cache / "linked_out.bin", b"l" * 70)
    (world / "elsewhere").mkdir(parents=True)
    os.link(outside_linked, world / "elsewhere" / "alias.bin")
    inside_a = _write(cache / "pair_a.bin", b"i" * 30)
    os.link(inside_a, cache / "pair_b.bin")
    with _scanned(tmp_path, world) as index:
        rc = RawCandidate(
            path=cache,
            is_dir=True,
            category="package_cache",
            category_group="package_caches",
            suggested_tier=Tier.A,
            rationale="r",
            rebuild_instruction=None,
        )
        size = index.subtree_size_bytes(cache)
        live = _reclaimable_bytes_for_candidate(index, rc, size)
        from_index = _reclaimable_bytes_from_index(index, cache, True, size)
    # 100 + 50 (unique) + 30 (the pair, once); the 70-byte file keeps a name outside the cache.
    assert live == from_index == 180


# --- 2. teeth: created after the scan, still refused at apply ----------------------------------


def test_protected_folder_created_after_the_scan_is_offered_from_the_index_but_refused_at_apply(
    tmp_path: Path,
) -> None:
    world = tmp_path / "world"
    cache = world / "cache"
    _write(cache / "a" / "blob.bin", b"b" * 64)
    config = _config(cache)
    safety = SafetyValidator(config)
    with _scanned(tmp_path, world) as index:
        _write(cache / "a" / f"{_PROJECT_NAME}-late" / "keep.bin", b"keep")  # after the scan
        warm = generate_candidates(index, config, safety, now=_NOW, trust_index=True)
        candidate = _package_cache(warm, cache)  # the stale index still offers it...
        assert candidate.estimate_source == "index"
        # ...while a live generation sees the new folder and does not.
        assert generate_candidates(index, config, safety, now=_NOW) == []
        # Retention set so the vault path is taken: only the last-line exclusion check of
        # apply_batch stands between this candidate and the move (the direct-delete re-check
        # is covered by the next test).
        vaulted = replace(candidate, retention_days=30)
        with pytest.raises(SafetyInvariantError, match="user exclusion"):
            _apply(vaulted, index, tmp_path, safety)
    assert (cache / "a" / f"{_PROJECT_NAME}-late" / "keep.bin").read_bytes() == b"keep"
    assert (cache / "a" / "blob.bin").exists()


def test_protected_folder_created_after_the_scan_is_refused_for_a_direct_delete_candidate(
    tmp_path: Path,
) -> None:
    world = tmp_path / "world"
    cache = world / "cache"
    _write(cache / "blob.bin", b"b" * 64)
    config = _config(cache)
    safety = SafetyValidator(config)
    with _scanned(tmp_path, world) as index:
        candidate = _package_cache(
            generate_candidates(index, config, safety, now=_NOW, trust_index=True), cache
        )
        assert candidate.retention_days is None  # package caches default to direct delete
        _write(cache / f"{_PROJECT_NAME}-late" / "keep.bin", b"keep")
        with pytest.raises(SafetyInvariantError):
            _apply(candidate, index, tmp_path, safety)
    assert (cache / f"{_PROJECT_NAME}-late" / "keep.bin").exists()


def test_hardlink_added_after_the_scan_is_estimated_from_the_index_but_refused_at_apply(
    tmp_path: Path,
) -> None:
    world = tmp_path / "world"
    cache = world / "cache"
    blob = _write(cache / "blob.py", b"shared-bytes")
    other_venv_module = _make_venv(world / "proj" / ".venv", module="own.py")
    config = _config(cache, exclusions=False)
    safety = SafetyValidator(config)
    with _scanned(tmp_path, world) as index:
        os.link(blob, other_venv_module.with_name("alias.py"))  # after the scan
        candidate = _package_cache(
            generate_candidates(index, config, safety, now=_NOW, trust_index=True), cache
        )
        # The scan knew of one name only, so the index figure still claims the whole file...
        assert candidate.reclaimable_bytes == len(b"shared-bytes")
        assert candidate.estimate_source == "index"
        assert candidate.retention_days is None
        result = _apply(candidate, index, tmp_path, safety)
    (item,) = result.items  # type: ignore[attr-defined]
    assert item.succeeded is False and item.skip_reason is not None
    assert blob.read_bytes() == b"shared-bytes"
    assert other_venv_module.with_name("alias.py").read_bytes() == b"shared-bytes"


def test_apply_time_exclusion_check_never_takes_the_trusted_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the contract itself: `apply_batch`/`_reverify_direct_delete_candidates` call the
    validator without `trusted_index`, so a stale index cannot answer for them."""
    world = tmp_path / "world"
    cache = world / "cache"
    _write(cache / "blob.bin", b"b")
    config = _config(cache)
    safety = SafetyValidator(config)
    seen: list[object] = []
    real_match = SafetyValidator.exclusion_match
    real_eval = SafetyValidator.evaluate

    def spy_match(self: SafetyValidator, path: Path, **kw: object) -> str | None:
        seen.append(kw.get("trusted_index"))
        return real_match(self, path, **kw)  # type: ignore[arg-type]

    def spy_eval(self: SafetyValidator, record: object, **kw: object) -> object:
        seen.append(kw.get("trusted_index"))
        return real_eval(self, record, **kw)  # type: ignore[arg-type]

    with _scanned(tmp_path, world) as index:
        candidate = _package_cache(
            generate_candidates(index, config, safety, now=_NOW, trust_index=True), cache
        )
        monkeypatch.setattr(SafetyValidator, "exclusion_match", spy_match)
        monkeypatch.setattr(SafetyValidator, "evaluate", spy_eval)
        seen.clear()
        _apply(candidate, index, tmp_path, safety)
    assert seen and all(value is None for value in seen)


# --- 3. API: index-derived estimates are marked, and an apply drops the warm cache --------------

_HOST = "127.0.0.1"
_PORT = 8421


def _client(tmp_path: Path, root: Path, cache: Path) -> TestClient:
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(cache, exclusions=False),
        config_path=tmp_path / "config.toml",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    token: str = app.state.reclaim.csrf_token
    client = TestClient(
        app, base_url=f"http://{_HOST}:{_PORT}", headers={security.CSRF_HEADER_NAME: token}
    )
    assert client.post("/api/scan", json={"path": str(root)}).status_code == 202
    assert client.get("/api/scan/status").json()["status"] == "completed"
    assert client.post("/api/candidates/warm").status_code == 202
    assert client.get("/api/candidates/warm-status").json()["status"] == "ready"
    return client


def test_api_marks_index_derived_estimates_on_candidates_and_every_aggregate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "world"
    cache = root / "cache"
    _write(cache / "blob.bin", b"b" * 64)
    client = _client(tmp_path, root, cache)

    listed = client.get("/api/candidates", params={"tier": "both"}).json()
    (row,) = [c for c in listed["candidates"] if c["path"] == cache.as_posix()]
    assert row["estimate_source"] == "index"
    assert row["reclaimable_bytes"] == 64
    assert isinstance(row["estimate_as_of"], float)  # the completed scan's time
    as_of = row["estimate_as_of"]
    assert (listed["estimate_source"], listed["estimate_as_of"]) == ("index", as_of)

    summary = client.get("/api/summary").json()
    assert (summary["estimate_source"], summary["estimate_as_of"]) == ("index", as_of)
    (card,) = [c for c in summary["categories"] if c["category_group"] == "package_caches"]
    assert (card["estimate_source"], card["estimate_as_of"]) == ("index", as_of)


def _bare(path: str, **kw: object) -> Candidate:
    return Candidate(
        path=Path(path),
        is_dir=True,
        category="package_cache",
        category_group="package_caches",
        size_bytes=10,
        tier=Tier.A,
        rationale="r",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="X",
        retention_days=None,
        **kw,  # type: ignore[arg-type]
    )


def test_aggregate_marking_is_live_index_or_mixed_with_the_oldest_scan_time() -> None:
    live = _bare("C:/live")
    old = _bare("C:/old", estimate_source="index", estimate_scan_time=100.0)
    new = _bare("C:/new", estimate_source="index", estimate_scan_time=200.0)
    unstamped = _bare("C:/unstamped", estimate_source="index")
    mark = service._estimate_marking
    assert mark([]) == {"estimate_source": "live", "estimate_as_of": None}
    assert mark([live]) == {"estimate_source": "live", "estimate_as_of": None}
    assert mark([old, new]) == {"estimate_source": "index", "estimate_as_of": 100.0}
    assert mark([live, new, old]) == {"estimate_source": "mixed", "estimate_as_of": 100.0}
    assert mark([unstamped]) == {"estimate_source": "index", "estimate_as_of": None}
    (card,) = service._category_cards([live, old])
    assert (card.estimate_source, card.estimate_as_of) == ("mixed", 100.0)
    (card,) = service._category_cards([live])
    assert (card.estimate_source, card.estimate_as_of) == ("live", None)


def test_a_real_apply_drops_the_warm_cache_and_a_dry_run_does_not(tmp_path: Path) -> None:
    root = tmp_path / "world"
    cache = root / "cache"
    _write(cache / "blob.bin", b"b" * 64)
    client = _client(tmp_path, root, cache)
    state = client.app.state.reclaim  # type: ignore[attr-defined]
    assert service.is_candidates_cache_warm(state)
    body = {"tier": "both", "paths": [cache.as_posix()], "method": "vault"}

    assert client.post("/api/apply", json={**body, "dry_run": True}).status_code == 202
    assert service.is_candidates_cache_warm(state)  # nothing was applied

    assert client.post("/api/apply", json={**body, "dry_run": False}).status_code == 202
    assert client.get("/api/apply/status").json()["status"] == "completed"
    assert not service.is_candidates_cache_warm(state)
    assert state.candidates_cache is None and state.candidates_clusters_cache is None

    # The next read re-warms (from the index) instead of serving the pre-apply figures.
    assert client.post("/api/candidates/warm").status_code == 202
    assert client.get("/api/candidates/warm-status").json()["status"] == "ready"
    assert service.is_candidates_cache_warm(state)


def test_candidate_out_defaults_to_live_and_carries_the_scan_time() -> None:
    base = Candidate(
        path=Path("C:/x"),
        is_dir=True,
        category="package_cache",
        category_group="package_caches",
        size_bytes=1,
        tier=Tier.A,
        rationale="r",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="X",
        retention_days=None,
    )
    assert service._candidate_out(base, None).estimate_source == "live"
    marked = service._candidate_out(
        replace(base, estimate_source="index", estimate_scan_time=_NOW), None
    )
    assert (marked.estimate_source, marked.estimate_as_of) == ("index", _NOW)
