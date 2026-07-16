from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.build_golden_tree import build_golden_tree

from reclaim.config import Config
from reclaim.executor import (
    QuarantineManifestEntry,
    SafetyInvariantError,
    append_manifest_entries,
    apply_batch,
)
from reclaim.models import Candidate, Tier, Verdict
from reclaim.purge import purge_expired
from reclaim.safety import SafetyValidator

_NOW = 1_700_000_000.0
_DAY = 86400.0

# ADR-0001, "Consequences": "the CI hard gate ... must be extended to independently prove zero
# protected-category files can ever reach *either* new delete path — including under an
# adversarial/malicious config.toml that tries to force a protected path through retention=none
# or into a purge-eligible vault entry. This is a new, separate assertion in the eval suite, not
# a reuse of the existing Tier-A-candidate-generation gate, because the attack surface is
# different (config-driven retention assignment vs. detector output)."
#
# Both tests below simulate exactly that attack surface: a `Candidate`/manifest entry that
# *already* carries an incorrect ELIGIBLE verdict (as if a bug in candidate generation, or a
# config that was tightened after the fact, let a protected file slip through) — proving the
# fresh pre-delete/pre-purge re-check, not the original candidate-generation gate, is what
# actually stops it.


def _protected_pem_case_path(tmp_path: Path) -> Path:
    """Reuses Stage 1's golden fixture tree (`evals/fixtures/build_golden_tree.py`) rather than
    a bespoke fixture — `protected_extension_pem` is one of Stage 1's hard-gated protected
    categories (`.pem` is a built-in protected credential extension, always blocked regardless
    of `[safety] protected_roots` overrides), so this is a genuine, already-verified-protected
    file, not a fixture invented just for this test.
    """
    cases = build_golden_tree(tmp_path)
    case = next(c for c in cases if c.id == "protected_extension_pem")
    assert case.expected_verdict == Verdict.BLOCKED  # sanity: still protected per Stage 1's gate
    return case.path


def test_direct_delete_pre_check_blocks_protected_file_forced_through_retention_none(
    tmp_path: Path,
) -> None:
    """The adversarial case ADR-0001 calls for: a genuinely protected file (Stage 1's
    `.pem` protected-extension category) ends up as a `retention_days=None` `Candidate` with an
    incorrectly-marked `safety_verdict=Verdict.ELIGIBLE` — simulating "candidate generation had
    a bug". `apply_batch(..., apply=True)` must raise `SafetyInvariantError` (the fresh re-check
    catches what the stale verdict missed), and the file must still be on disk, byte-unchanged,
    afterward.
    """
    protected_path = _protected_pem_case_path(tmp_path)
    original_content = protected_path.read_bytes()

    safety = SafetyValidator(Config())  # real, live, default config — real protected-extension deny

    malicious_candidate = Candidate(
        path=protected_path,
        is_dir=False,
        category="package_cache",
        category_group="package_caches",
        size_bytes=1500,
        tier=Tier.A,
        rationale="incorrectly generated candidate (simulated bug)",
        rebuild_instruction="Re-run the package manager; the cache repopulates automatically.",
        safety_verdict=Verdict.ELIGIBLE,  # the bug: stale/incorrect verdict
        safety_reason_code="DEFAULT_ELIGIBLE",
        retention_days=None,  # forces the direct-delete path
    )

    with pytest.raises(SafetyInvariantError, match="pre-delete safety re-check"):
        apply_batch(
            [malicious_candidate],
            safety=safety,
            apply=True,
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
        )

    # --- The file is still on disk, byte-unchanged, afterward.
    assert protected_path.exists()
    assert protected_path.read_bytes() == original_content


def test_purge_pre_check_blocks_protected_original_path_even_past_retention(
    tmp_path: Path,
) -> None:
    """Equivalent adversarial case for `purge_expired`: a manifest entry whose `original_path`
    matches a protected pattern (Stage 1's `.pem` protected-extension category, matched via the
    manifest-reconstructed `FileRecord`'s `ext`), with `retention_until` in the past, must not
    be purged.
    """
    protected_path = _protected_pem_case_path(tmp_path)

    vault_path = tmp_path / "vault" / "batch_test" / "cert.pem"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(b"vaulted-copy-of-a-protected-file")

    manifest_path = tmp_path / "manifest.jsonl"
    entry = QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=protected_path,
        size_bytes=1500,
        is_dir=False,
        category="old_installer",
        category_group="old_installers",
        rationale="incorrectly vaulted candidate (simulated bug/config drift)",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=vault_path,
        retention_days=30,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=_NOW - 10 * _DAY,  # already past retention
    )
    append_manifest_entries(manifest_path, [entry])

    safety = SafetyValidator(Config())  # real, live, default config

    with pytest.raises(SafetyInvariantError, match="pre-purge safety re-check"):
        purge_expired(
            apply=True,
            manifest_path=manifest_path,
            vault_dir=tmp_path / "vault",
            safety=safety,
            now=_NOW,
        )

    # --- Nothing purged: the vault copy still exists, byte-unchanged.
    assert vault_path.exists()
    assert vault_path.read_bytes() == b"vaulted-copy-of-a-protected-file"


def test_purge_never_purges_future_retention_until_not_even_by_forcing_apply(
    tmp_path: Path,
) -> None:
    """Hard boundary, restated at the eval level (not just the fast unit test in
    tests/test_purge.py): a vault entry whose `retention_until` is still in the future is never
    purge-eligible in the first place — there is no `purge_expired` parameter that can force it,
    `--apply` or not."""
    vault_path = tmp_path / "vault" / "batch_test" / "not_yet.bin"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(b"not-yet-expired")

    manifest_path = tmp_path / "manifest.jsonl"
    entry = QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=tmp_path / "gone.bin",
        size_bytes=15,
        is_dir=False,
        category="old_installer",
        category_group="old_installers",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=vault_path,
        retention_days=30,
        quarantined_at=_NOW,
        retention_until=_NOW + 20 * _DAY,  # not yet expired
    )
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=SafetyValidator(Config()),
        now=_NOW,
    )

    assert report.files_processed == 0
    assert vault_path.exists()
    assert vault_path.read_bytes() == b"not-yet-expired"
