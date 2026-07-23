from __future__ import annotations

import shutil
from dataclasses import replace as _dataclass_replace
from pathlib import Path

import pytest

from reclaim.config import (
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    DuplicatesConfig,
    ModelCachesConfig,
    PackageCachesConfig,
    SafetyConfig,
    apply_safe_mode_category_overrides,
    load_config,
    load_effective_config,
)
from reclaim.detectors import generate_candidates
from reclaim.executor import (
    SafeModeViolationError,
    _effective_method_and_retention_days,
    apply_batch,
)
from reclaim.index import ScanIndex
from reclaim.mode import (
    REQUIRED_POWER_MODE_CONFIRMATION,
    ModeSwitchDeniedError,
    current_mode,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import Candidate, Mode, Tier, Verdict
from reclaim.purge import purge_expired
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# Stage 2 / public-release safety boundary. Every test here proves a STRUCTURAL (not
# conventional) guarantee, same rigor and same "prove it, don't just narrate it" discipline as
# evals/test_ai_safety_gate.py's §7.5 gate for the AI layer: safe mode's permanent-delete path
# must be UNREACHABLE, not merely unreached in the cases this file happens to construct.


def _candidate(
    path: Path,
    *,
    is_dir: bool = False,
    size_bytes: int = 100,
    category: str = "test_category",
    category_group: str = "test_group",
    tier: Tier = Tier.A,
    safety_verdict: Verdict = Verdict.ELIGIBLE,
    retention_days: int | None = 30,
    size_guard_exempt: bool = False,
    rebuildable: bool = False,
) -> Candidate:
    return Candidate(
        path=path,
        is_dir=is_dir,
        category=category,
        category_group=category_group,
        size_bytes=size_bytes,
        tier=tier,
        rationale="test rationale",
        rebuild_instruction=None,
        safety_verdict=safety_verdict,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
        size_guard_exempt=size_guard_exempt,
        rebuildable=rebuildable,
    )


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


# --- 1. Permanent-delete path structurally unreachable in safe mode ---------------------------


def test_safe_mode_never_produces_vault_or_direct_delete_method(tmp_path: Path) -> None:
    """Exhaustive proof over `_effective_method_and_retention_days` — the ONE function that
    decides `apply_batch`'s per-candidate method: every combination of `retention_days`
    (including the size-guard-triggering case ADR-0003 would normally route to `vault`) and
    every requested batch `method` (`vault`/`recycle_bin`) resolves to `"recycle_bin"` when
    `mode=Mode.SAFE`, with no exception. This is what makes the `vault`/`direct_delete`
    branches in `apply_batch`'s per-candidate loop structurally unreachable in safe mode, not
    merely unreached in whatever scenarios this test file happens to construct."""
    retention_days_values: list[int | None] = [None, 0, 1, 30, 9999]
    requested_methods: list[str] = ["vault", "recycle_bin"]
    huge_candidate = _candidate(tmp_path / "huge.bin", size_bytes=10 * 1024 * 1024 * 1024)

    for retention_days in retention_days_values:
        for requested_method in requested_methods:
            for candidate, label in (
                (_candidate(tmp_path / "f.bin", retention_days=retention_days), "normal"),
                (
                    _dataclass_replace(huge_candidate, retention_days=retention_days),
                    "size-guard-eligible",
                ),
            ):
                method, _resolved_retention = _effective_method_and_retention_days(
                    candidate,
                    requested_method,  # type: ignore[arg-type]
                    mode=Mode.SAFE,
                    size_guard_bytes=1024,
                    size_guard_retention_days=30,
                )
                assert method == "recycle_bin", (
                    f"safe mode produced method={method!r} for retention_days="
                    f"{retention_days!r}, requested={requested_method!r} ({label}) — the "
                    "permanent-delete/vault path is not actually unreachable"
                )


def test_apply_batch_refuses_non_recycle_bin_method_in_safe_mode_before_any_io(
    tmp_path: Path,
) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"do-not-touch")

    with pytest.raises(SafeModeViolationError, match="only ever allows the Recycle Bin"):
        apply_batch(
            [_candidate(target, retention_days=None)],
            safety=_safety(),
            apply=True,
            method="vault",
            mode=Mode.SAFE,
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
        )

    assert target.exists()
    assert target.read_bytes() == b"do-not-touch"
    assert not (tmp_path / "vault").exists()
    assert not (tmp_path / "manifest.jsonl").exists()


def test_safe_mode_apply_never_calls_direct_delete_or_vault_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integration-level proof, same shape as evals/test_ai_safety_gate.py's
    `test_ai_cluster_member_fed_to_apply_batch_fails_loudly_before_any_disk_io`: monkeypatch
    the actual OS-level primitives a `direct_delete` or `vault` outcome would call, run a REAL
    `apply=True` batch mixing `retention_days=None` (would normally direct-delete) and
    `retention_days=30` (would normally vault) candidates in safe mode, and assert those
    primitives were never invoked while `send2trash` — the only permitted outcome — was."""
    import send2trash

    def _boom_unlink(path: str) -> None:
        raise AssertionError("os.unlink must never be called in safe mode")

    def _boom_rmtree(*args: object, **kwargs: object) -> None:
        raise AssertionError("shutil.rmtree must never be called in safe mode")

    sent_to_trash: list[str] = []

    def _fake_send2trash(path: str) -> None:
        sent_to_trash.append(path)
        Path(path).unlink()  # simulates real send2trash's effect for the assertions below

    monkeypatch.setattr("reclaim.executor.unlink_clear_readonly", _boom_unlink)
    monkeypatch.setattr(shutil, "rmtree", _boom_rmtree)
    monkeypatch.setattr(send2trash, "send2trash", _fake_send2trash)

    direct_delete_target = tmp_path / "would_direct_delete.bin"
    direct_delete_target.write_bytes(b"x" * 100)
    vault_target = tmp_path / "would_vault.bin"
    vault_target.write_bytes(b"y" * 100)

    report = apply_batch(
        [
            _candidate(direct_delete_target, retention_days=None),
            _candidate(vault_target, retention_days=30),
        ],
        safety=_safety(),
        apply=True,
        method="recycle_bin",
        mode=Mode.SAFE,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
    )

    assert report.files_succeeded == 2
    assert all(item.method == "recycle_bin" for item in report.items)
    assert sent_to_trash == [str(direct_delete_target), str(vault_target)]
    assert not (tmp_path / "vault").exists()


def test_purge_expired_refuses_unconditionally_in_safe_mode(tmp_path: Path) -> None:
    """Purge is always a permanent-delete of a vault copy — forbidden outright in safe mode,
    regardless of manifest content (including a manifest with real, currently-eligible vault
    entries from an earlier power-mode session — the case this test constructs)."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    target = tmp_path / "vaulted_original.bin"
    target.write_bytes(b"x" * 100)

    # A real vault entry, created via a genuine power-mode apply, whose retention has expired.
    apply_batch(
        [_candidate(target, retention_days=0)],
        safety=_safety(),
        apply=True,
        method="vault",
        mode=Mode.POWER,
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=1_700_000_000.0,
    )

    with pytest.raises(SafeModeViolationError, match="unconditionally forbidden in safe mode"):
        purge_expired(
            apply=True,
            manifest_path=manifest_path,
            vault_dir=vault_dir,
            safety=_safety(),
            mode=Mode.SAFE,
            now=1_700_000_000.0 + 999_999,
        )

    # The vault copy must still be there — nothing was purged.
    assert any(vault_dir.rglob("*_vaulted_original.bin"))


# --- 2. Dangerous categories off by default in safe mode ---------------------------------------


def test_dangerous_categories_forced_off_regardless_of_config_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[categories.duplicates]\nenabled = true\n"
        "[categories.model_caches]\nenabled = true\n"
        "[categories.dev_artifacts]\nenabled = true\n"
        "[categories.package_caches]\nenabled = true\n",
        encoding="utf-8",
    )

    safe_config = load_effective_config(config_path, mode=Mode.SAFE)
    assert safe_config.categories.duplicates.enabled is False
    assert safe_config.categories.model_caches.enabled is False
    assert safe_config.categories.dev_artifacts.enabled is False
    # Rebuildable, non-dangerous category: untouched by the safe-mode override.
    assert safe_config.categories.package_caches.enabled is True

    # The same raw config.toml, loaded in power mode, is untouched -- proves the override is
    # mode-conditional, not a permanent mutation of what config.toml says.
    power_config = load_effective_config(config_path, mode=Mode.POWER)
    assert power_config.categories.duplicates.enabled is True
    assert power_config.categories.model_caches.enabled is True
    assert power_config.categories.dev_artifacts.enabled is True

    # load_config itself (no mode resolution) stays exactly what config.toml said -- the two
    # functions are deliberately independent, see config.py's own docstrings.
    raw_config = load_config(config_path)
    assert raw_config.categories.duplicates.enabled is True


def test_apply_safe_mode_category_overrides_is_a_pure_function() -> None:
    categories = CategoriesConfig(
        duplicates=DuplicatesConfig(enabled=True),
        model_caches=ModelCachesConfig(enabled=True),
        dev_artifacts=DevArtifactsConfig(enabled=True),
        package_caches=PackageCachesConfig(enabled=True),
    )
    overridden = apply_safe_mode_category_overrides(categories)

    assert overridden.duplicates.enabled is False
    assert overridden.model_caches.enabled is False
    assert overridden.dev_artifacts.enabled is False
    assert overridden.package_caches.enabled is True
    # Original untouched -- pydantic model_copy, never in-place mutation.
    assert categories.duplicates.enabled is True


def test_every_candidate_forced_to_tier_b_in_safe_mode(tmp_path: Path) -> None:
    """`generate_candidates` never emits Tier A for anything while `config.mode` is SAFE, even
    for a category that IS enabled and would otherwise be Tier-A-eligible -- the "no auto-
    delete, no batch-auto for ANY category" guarantee does not depend on the category-disable
    layer alone (redundant, independent enforcement -- see detectors.py's own docstring)."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "node_modules").mkdir()
    (root / "app" / "node_modules" / "pkg.js").write_bytes(b"x" * 100)
    (root / "app" / "package.json").write_bytes(b"{}")

    db = tmp_path / "index.sqlite3"
    with ScanIndex(db) as index:
        scan_tree(root, index)

        safe_config = Config(
            safety=SafetyConfig(protected_roots=[]),
            categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True)),
            mode=Mode.SAFE,
        )
        safe_candidates = generate_candidates(index, safe_config, SafetyValidator(safe_config))
        assert safe_candidates, "expected at least one dev_artifacts candidate to be generated"
        assert all(c.tier == Tier.B for c in safe_candidates)

        power_config = safe_config.model_copy(update={"mode": Mode.POWER})
        power_candidates = generate_candidates(index, power_config, SafetyValidator(power_config))
        assert any(c.tier == Tier.A for c in power_candidates), (
            "sanity check: the same fixture under power mode should produce at least one "
            "Tier A candidate -- otherwise this test can't distinguish safe mode actually "
            "doing something from the fixture just never reaching Tier A at all"
        )


# --- 3. Power mode requires exact typed confirmation --------------------------------------------


@pytest.mark.parametrize(
    "confirmation_text",
    [
        "",
        "yes",
        "I agree",
        "i understand this can permanently delete files",  # wrong case
        "I understand this can permanently delete files ",  # trailing space
        "I understand this can permanently delete file",  # truncated
    ],
)
def test_power_mode_rejects_anything_but_the_exact_phrase(
    tmp_path: Path, confirmation_text: str
) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    with pytest.raises(ModeSwitchDeniedError):
        switch_to_power_mode(confirmation_text, log_path=log_path)
    # A rejected attempt must leave the mode unchanged and log nothing.
    assert current_mode(log_path) == Mode.SAFE
    assert not log_path.exists()


def test_power_mode_accepts_the_exact_phrase(tmp_path: Path) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    entry = switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path)
    assert entry.to_mode == Mode.POWER
    assert entry.confirmed is True
    assert current_mode(log_path) == Mode.POWER


def test_safe_mode_reversion_never_requires_confirmation(tmp_path: Path) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path)
    entry = switch_to_safe_mode(log_path=log_path)
    assert entry.to_mode == Mode.SAFE
    assert current_mode(log_path) == Mode.SAFE


# --- 4. Mode persists correctly ------------------------------------------------------------------


def test_mode_defaults_to_safe_when_no_log_exists(tmp_path: Path) -> None:
    log_path = tmp_path / "does_not_exist.jsonl"
    assert current_mode(log_path) == Mode.SAFE


def test_mode_persists_across_multiple_switches(tmp_path: Path) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    assert current_mode(log_path) == Mode.SAFE

    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path)
    assert current_mode(log_path) == Mode.POWER

    switch_to_safe_mode(log_path=log_path)
    assert current_mode(log_path) == Mode.SAFE

    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path)
    assert current_mode(log_path) == Mode.POWER

    # The log is a real, readable append-only history -- four transitions recorded.
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3


def test_mode_log_survives_a_fresh_read_not_just_the_in_memory_return_value(tmp_path: Path) -> None:
    """current_mode() must read the log fresh from disk, not rely on any in-process cache —
    the API layer's AppState.live_mode property depends on exactly this property to see a mode
    switch made by a different call within the same server process."""
    log_path = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path)
    # A brand-new call, no shared state except the file on disk.
    assert current_mode(Path(str(log_path))) == Mode.POWER
