"""End to end: detection from a real scan index, apply into the vault, restore, and the apply-time
re-proof (the scan-time verdict is never trusted)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_fixtures import (
    add_worktree,
    commit_file,
    git,
    make_repo,
    worktree_listed,
)

from reclaim import agent_artifacts as aa
from reclaim import executor
from reclaim.config import (
    AgentArtifactsConfig,
    CategoriesConfig,
    Config,
    ExclusionsConfig,
)
from reclaim.detectors import generate_candidates
from reclaim.executor import SafetyInvariantError, apply_batch, restore_batch
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Mode, Tier
from reclaim.procscan import ProcessSnapshot, fake_scan
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@dataclass
class Setup:
    root: Path  # the scanned tree
    repo: Path
    wt: Path
    clone: Path
    session: Path
    vault: Path
    manifest: Path
    config: Config
    safety: SafetyValidator
    index: ScanIndex


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    root = tmp_path / "home"
    root.mkdir()
    temp = root / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(aa, "scan_processes", lambda: fake_scan())
    # The default vault is data_root()-relative: on the CI runner that is the checkout on D:
    # while tmp_path is on C:, and rule V (vault on the candidate's volume) refuses every
    # proposal. Pin it beside the tree, as the real app's vault sits beside the user's data.
    monkeypatch.setattr(aa, "default_vault_dir", lambda: tmp_path / "vault")
    repo = make_repo(root, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    clone = make_repo(root, "proj-verify1")
    session = temp / "claude" / "C--work-proj" / "123e4567-e89b-12d3-a456-426614174000"
    (session / "scratchpad").mkdir(parents=True)
    (session / "scratchpad" / "n.txt").write_text("scratch", encoding="utf-8")
    stamp = time.time() - 30 * 86400
    for dirpath, dirnames, filenames in os.walk(session):
        for n in [*dirnames, *filenames]:
            os.utime(Path(dirpath) / n, (stamp, stamp))
        os.utime(dirpath, (stamp, stamp))
    return _build(tmp_path, root, repo, wt, clone, session)


def _build(
    tmp_path: Path, root: Path, repo: Path, wt: Path, clone: Path, session: Path,
    *, exclusions: tuple[str, ...] = (),
) -> Setup:  # fmt: skip
    config = Config(
        mode=Mode.POWER,
        exclusions=ExclusionsConfig(project_names=list(exclusions)),
        categories=CategoriesConfig(agent_artifacts=AgentArtifactsConfig(enabled=True)),
    )
    safety = SafetyValidator(config)
    index = ScanIndex(tmp_path / "index.sqlite3")
    scan_tree(root, index)
    vault = tmp_path / "vault"
    return Setup(
        root, repo, wt, clone, session, vault, vault / "manifest.jsonl", config, safety, index
    )


def _detect(s: Setup) -> dict[str, Candidate]:
    candidates, _ = aa.detect_agent_artifacts(
        s.index, s.safety, retention_days=14, vault_dir=s.vault
    )
    return {c.category: c for c in candidates}


def _apply(s: Setup, cands: list[Candidate], **kw: object) -> executor.BatchApplyReport:
    return apply_batch(
        cands,
        safety=s.safety,
        apply=True,
        method="vault",
        mode=Mode.POWER,
        vault_dir=s.vault,
        manifest_path=s.manifest,
        scan_index=s.index,
        allowed_roots=[s.root],
        **kw,  # type: ignore[arg-type]
    )


# --- detection --------------------------------------------------------------------------------


def test_all_three_kinds_are_detected_as_review_tier_vaulted_candidates(setup: Setup) -> None:
    found = _detect(setup)
    assert set(found) == set(aa.CATEGORIES)
    for cand in found.values():
        assert cand.tier is Tier.B  # the user clicks; never automatic
        assert cand.category_group == aa.GROUP
        assert cand.retention_days == 14  # vaulted, never a permanent delete
        assert cand.is_dir and cand.size_bytes > 0
        assert "Proven finished" in cand.rationale
    assert "proj" in found[aa.CAT_WORKTREE].rationale and "feat" in found[aa.CAT_WORKTREE].rationale
    assert setup.wt == found[aa.CAT_WORKTREE].path


def test_detection_is_read_only(setup: Setup) -> None:
    before = sorted(str(p) for p in setup.root.rglob("*") if ".git" not in p.parts[-3:])
    _detect(setup)
    after = sorted(str(p) for p in setup.root.rglob("*") if ".git" not in p.parts[-3:])
    assert before == after


def test_refused_artifacts_are_not_candidates_but_are_reported(setup: Setup) -> None:
    (setup.wt / "dirty.txt").write_text("dirty", encoding="utf-8")
    candidates, verdicts = aa.detect_agent_artifacts(
        setup.index, setup.safety, retention_days=14, vault_dir=setup.vault
    )
    assert aa.CAT_WORKTREE not in {c.category for c in candidates}
    refused = [v for v in verdicts if not v.offered]
    assert any(v.path == setup.wt and v.failed_rule == "W2" for v in refused)
    lines = aa.report_lines(verdicts)
    text = " ".join(lines)
    assert "KEEP" in text and "first failed rule W2" in text
    assert any("PROPOSE" in ln for ln in lines)


def test_generate_candidates_includes_them_only_when_enabled_in_power_mode(setup: Setup) -> None:
    def groups(config: Config) -> set[str]:
        return {c.category_group for c in generate_candidates(setup.index, config, setup.safety)}

    assert aa.GROUP in groups(setup.config)
    off = setup.config.model_copy(
        update={"categories": CategoriesConfig(agent_artifacts=AgentArtifactsConfig(enabled=False))}
    )
    assert aa.GROUP not in groups(off)
    assert aa.GROUP not in groups(setup.config.model_copy(update={"mode": Mode.SAFE}))


def test_the_category_is_forced_off_in_safe_mode() -> None:
    from reclaim.config import apply_safe_mode_category_overrides

    on = CategoriesConfig(agent_artifacts=AgentArtifactsConfig(enabled=True))
    assert apply_safe_mode_category_overrides(on).agent_artifacts.enabled is False


def test_an_offered_artifact_takes_over_candidates_inside_it(setup: Setup) -> None:
    out = generate_candidates(setup.index, setup.config, setup.safety)
    inside = [c for c in out if c.category_group != aa.GROUP and setup.wt in c.path.parents]
    assert inside == []


# --- apply and restore ------------------------------------------------------------------------


def test_apply_moves_each_kind_into_the_vault_and_prunes_the_worktree(setup: Setup) -> None:
    found = _detect(setup)
    report = _apply(setup, list(found.values()))
    assert report.files_succeeded == 3 and report.files_failed == 0
    for cand in found.values():
        assert not cand.path.exists()
    assert not worktree_listed(setup.repo, setup.wt)  # git worktree prune ran
    assert git(setup.repo, "branch", "--list", "feat").stdout.strip()  # the branch survives
    assert (setup.repo / "README.md").read_text(encoding="utf-8") == "hello\n"
    for item in report.items:
        assert item.method == "vault" and item.vault_path is not None
        assert item.vault_path.exists()
    wt_vault = next(i.vault_path for i in report.items if i.category == aa.CAT_WORKTREE)
    assert (wt_vault / aa.ADMIN_BACKUP_DIR / "meta.json").is_file()
    assert (wt_vault / "README.md").is_file()


def test_restore_puts_every_item_back_and_git_repairs_the_worktree(setup: Setup) -> None:
    found = _detect(setup)
    report = _apply(setup, list(found.values()))
    restored = restore_batch(
        report.batch_id,
        manifest_path=setup.manifest,
        vault_dir=setup.vault,
        safety=setup.safety,
    )
    assert restored.files_succeeded == 3 and restored.files_failed == 0
    for cand in found.values():
        assert cand.path.exists()
    assert not (setup.wt / aa.ADMIN_BACKUP_DIR).exists()
    assert worktree_listed(setup.repo, setup.wt)
    assert git(setup.wt, "status", "--porcelain").stdout.strip() == ""
    assert git(setup.wt, "log", "-1", "--format=%s").stdout.strip() == "initial"
    again = aa.evaluate_worktree(_env(setup), setup.wt, setup.repo)
    assert again.offered, (
        again.failed_rule,
        again.failed_detail,
    )  # restored == still provably clean


def _env(s: Setup) -> aa.AgentEnv:
    return aa.build_env(s.safety, vault_dir=s.vault, procs=fake_scan())


def test_dry_run_changes_nothing(setup: Setup) -> None:
    found = _detect(setup)
    report = apply_batch(
        list(found.values()),
        safety=setup.safety,
        apply=False,
        method="vault",
        mode=Mode.POWER,
        vault_dir=setup.vault,
        manifest_path=setup.manifest,
        scan_index=setup.index,
        allowed_roots=[setup.root],
    )
    assert report.apply is False
    for cand in found.values():
        assert cand.path.exists()
    assert worktree_listed(setup.repo, setup.wt)
    assert not setup.vault.exists()


def test_a_failed_prune_does_not_fail_the_vaulted_item(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = aa.run_git

    def run(args: object, cwd: Path) -> aa.GitResult:
        if list(args)[:2] == ["worktree", "prune"] or tuple(args)[:2] == ("worktree", "prune"):  # type: ignore[call-overload]
            return aa.GitResult(1, "", "prune broke")
        return real(args, cwd)  # type: ignore[arg-type]

    monkeypatch.setattr(aa, "run_git", run)
    monkeypatch.setattr(aa, "after_vault_move", lambda o, v, r=run: _orig_after(o, v, r))
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    assert report.files_succeeded == 1
    assert not setup.wt.exists()


_orig_after = aa.after_vault_move


# --- the scan-time verdict is never trusted ------------------------------------------------------


def _skipped(report: executor.BatchApplyReport) -> list[str | None]:
    return [i.skip_reason for i in report.items]


@pytest.mark.parametrize(
    "mutate",
    ["dirty", "untracked", "unpushed", "hidden_edit", "operation", "fresh_scratch_file"],
)
def test_a_change_after_detection_is_caught_at_apply_time(setup: Setup, mutate: str) -> None:
    found = _detect(setup)
    target = found[aa.CAT_SCRATCH if mutate == "fresh_scratch_file" else aa.CAT_WORKTREE]
    if mutate == "dirty":
        (setup.wt / "README.md").write_text("dirty\n", encoding="utf-8")
    elif mutate == "untracked":
        (setup.wt / "new.txt").write_text("new", encoding="utf-8")
    elif mutate == "unpushed":
        commit_file(setup.wt, "late.txt", "late\n", "committed after the scan")
    elif mutate == "hidden_edit":
        git(setup.wt, "update-index", "--skip-worktree", "README.md")
        (setup.wt / "README.md").write_text("hidden\n", encoding="utf-8")
    elif mutate == "operation":
        gitdir = Path(git(setup.wt, "rev-parse", "--absolute-git-dir").stdout.strip())
        (gitdir / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    else:
        (setup.session / "scratchpad" / "fresh.txt").write_text("now", encoding="utf-8")
    report = _apply(setup, [target])
    assert report.files_succeeded == 0
    assert _skipped(report) == ["agent_artifact_not_provably_finished"]
    assert target.path.exists()  # nothing moved
    assert not setup.vault.exists() or not any(setup.vault.rglob("README.md"))
    if target.category == aa.CAT_WORKTREE:
        assert worktree_listed(setup.repo, setup.wt)


def test_a_process_that_appears_after_detection_blocks_the_apply(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _detect(setup)
    live = fake_scan([ProcessSnapshot(5, "node.exe", str(setup.wt), "node dev.js")])
    monkeypatch.setattr(aa, "scan_processes", lambda: live)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    assert _skipped(report) == ["agent_artifact_not_provably_finished"]
    assert setup.wt.exists()


def test_the_apply_process_naming_the_worktree_does_not_hide_it(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build 13 (1 Nov): `reclaim.exe apply <worktree>` reported 0/0 eligible because the
    process scan saw the apply process's OWN command line name the worktree (rule W5)."""
    cmd = f"reclaim.exe apply {setup.wt} --tier B"
    own = fake_scan([ProcessSnapshot(os.getpid(), "reclaim.exe", str(setup.root), cmd)])
    monkeypatch.setattr(aa, "scan_processes", lambda: own)
    assert aa.CAT_WORKTREE in _detect(setup)
    other = fake_scan([ProcessSnapshot(os.getpid() + 1, "reclaim.exe", str(setup.root), cmd)])
    monkeypatch.setattr(aa, "scan_processes", lambda: other)
    assert aa.CAT_WORKTREE not in _detect(setup)


def test_an_exclusion_added_after_detection_refuses_the_whole_batch(setup: Setup) -> None:
    found = _detect(setup)
    stricter = SafetyValidator(
        setup.config.model_copy(update={"exclusions": ExclusionsConfig(project_names=["proj"])})
    )
    with pytest.raises(SafetyInvariantError):
        apply_batch(
            list(found.values()),
            safety=stricter,
            apply=True,
            method="vault",
            mode=Mode.POWER,
            vault_dir=setup.vault,
            manifest_path=setup.manifest,
            scan_index=setup.index,
            allowed_roots=[setup.root],
        )
    for cand in found.values():
        assert cand.path.exists()


@pytest.mark.parametrize(
    ("mode", "method"), [(Mode.SAFE, "recycle_bin"), (Mode.POWER, "recycle_bin")]
)
def test_these_items_are_never_sent_to_the_recycle_bin(
    setup: Setup, mode: Mode, method: str
) -> None:
    found = _detect(setup)
    report = apply_batch(
        [found[aa.CAT_WORKTREE]],
        safety=setup.safety,
        apply=True,
        method=method,  # type: ignore[arg-type]
        mode=mode,
        vault_dir=setup.vault,
        manifest_path=setup.manifest,
        scan_index=setup.index,
        allowed_roots=[setup.root],
    )
    assert _skipped(report) == ["agent_artifact_requires_vault"]
    assert setup.wt.exists()


def test_a_candidate_cannot_be_applied_without_the_means_to_reprove_it(setup: Setup) -> None:
    found = _detect(setup)
    reason = executor._preflight_skip_reason(
        found[aa.CAT_WORKTREE],
        item_method="vault",
        scan_index=setup.index,
        allowed_roots=[setup.root],
    )
    assert reason == "agent_artifact_not_provably_finished"  # no validator or vault given: refuse


def test_mcp_style_reversible_batches_vault_these_items(setup: Setup) -> None:
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_SCRATCH]], reversible_only=True)
    assert report.files_succeeded == 1
    assert report.items[0].method == "vault"
    assert not setup.session.exists()


# --- move safety: rename only, a held directory is left alone ----------------------------------


def test_a_refused_rename_never_falls_back_to_copy_and_delete(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _detect(setup)
    real_rename = os.rename
    copies: list[object] = []
    real_copytree = shutil.copytree

    def deny(src: object, dst: object, *a: object, **k: object) -> None:
        if str(src).rstrip(chr(92)).endswith("proj-wt-feat"):
            raise PermissionError(5, "Access is denied")
        real_rename(src, dst, *a, **k)  # type: ignore[arg-type]

    def record_copy(*args: object, **kwargs: object) -> object:
        copies.append(args)  # RECORD the call: a raised error would be swallowed by the batch
        return real_copytree(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", deny)
    monkeypatch.setattr(shutil, "copytree", record_copy)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    assert copies == [], "the copy fallback ran for an agent artifact"
    assert report.files_failed == 1 and report.files_succeeded == 0
    assert setup.wt.exists() and (setup.wt / "README.md").is_file()
    assert worktree_listed(setup.repo, setup.wt)  # prune did not run
    assert not any(setup.vault.glob("batch_*/*")) if setup.vault.exists() else True


def test_a_real_open_file_handle_keeps_the_directory_in_place(setup: Setup) -> None:
    found = _detect(setup)
    handle = (setup.wt / "README.md").open("rb")  # no share-delete: Windows refuses a parent rename
    try:
        report = _apply(setup, [found[aa.CAT_WORKTREE]])
    finally:
        handle.close()
    assert report.files_succeeded == 0
    assert setup.wt.exists() and (setup.wt / "README.md").is_file()


def test_other_categories_still_get_the_copy_fallback() -> None:
    assert executor._rename_only("dev_artifacts") == {}
    assert executor._rename_only(aa.GROUP) == {"allow_copy_fallback": False}


# --- the dry-run command ------------------------------------------------------------------------


def test_cli_dry_run_lists_proposals_and_changes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from reclaim.cli import main

    config_file = tmp_path / "config.toml"
    config_file.write_text("[categories.agent_artifacts]\nenabled = false\n", encoding="utf-8")
    setup.index.close()
    db = tmp_path / "index.sqlite3"
    snapshot = sorted(str(p) for p in setup.root.rglob("*") if ".git" not in p.parts[-3:])
    rc = main(["agent-artifacts", "--db", str(db), "--config", str(config_file), "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and "would propose 3" in out
    for kind in aa.CATEGORIES:
        assert kind in out
    assert str(setup.wt) in out and "PROPOSE" in out
    assert snapshot == sorted(
        str(p) for p in setup.root.rglob("*") if ".git" not in p.parts[-3:]
    )  # read-only


def test_cli_dry_run_without_an_index_is_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from reclaim.cli import main

    rc = main(["agent-artifacts", "--db", str(tmp_path / "missing.sqlite3")])
    assert rc == 1
    assert "run `reclaim scan` first" in capsys.readouterr().err


def test_detection_marks_excluded_projects_and_offers_nothing_from_them(setup: Setup) -> None:
    strict = SafetyValidator(
        setup.config.model_copy(update={"exclusions": ExclusionsConfig(project_names=["proj"])})
    )
    candidates, verdicts = aa.detect_agent_artifacts(
        setup.index, strict, retention_days=14, vault_dir=setup.vault
    )
    flagged = [v for v in verdicts if v.excluded]
    assert {v.category for v in flagged} >= {aa.CAT_WORKTREE, aa.CAT_CLONE}
    assert all(c.path not in {v.path for v in flagged} for c in candidates)
    assert all("proj" not in line for line in aa.report_lines(verdicts) if "PROPOSE" in line)


def test_cli_dry_run_hides_excluded_projects_and_counts_them(
    setup: Setup, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from reclaim.cli import main

    config_file = tmp_path / "config.toml"
    config_file.write_text('[exclusions]\nproject_names = ["proj"]\n', encoding="utf-8")
    setup.index.close()
    rc = main(
        [
            "agent-artifacts",
            "--db",
            str(tmp_path / "index.sqlite3"),
            "--config",
            str(config_file),
            "--all",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert str(setup.wt) not in out and "proj-verify1" not in out  # never surfaced
    assert "belong to excluded projects (not inspected, not listed)" in out


# === regression tests for the independent review (pass A) =========================================


def test_review_a_vaulting_one_worktree_leaves_every_other_admin_entry_alone(
    setup: Setup,
) -> None:
    """F7: a plain `git worktree prune` also drops the entry of ANOTHER prunable worktree (one whose
    directory is merely elsewhere or offline), losing its staged index and an unpushed commit."""
    other = add_worktree(setup.repo, "proj-wt-other", None)  # detached
    commit_file(other, "precious.txt", "unpushed work\n", "unpushed detached commit")
    (other / "staged.txt").write_text("staged", encoding="utf-8")
    git(other, "add", "staged.txt")
    other_admin = Path(git(other, "rev-parse", "--absolute-git-dir").stdout.strip())
    sha = git(other, "rev-parse", "HEAD").stdout.strip()
    moved = other.parent / "proj-wt-other-offline"
    other.rename(moved)  # its directory is gone from where git expects it: prunable
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    assert report.files_succeeded == 1 and not setup.wt.exists()
    assert other_admin.is_dir(), "another worktree's admin entry was removed"
    assert (other_admin / "index").is_file() and (other_admin / "HEAD").is_file()
    assert git(setup.repo, "cat-file", "-t", sha).stdout.strip() == "commit"
    moved.rename(other)  # bring it back: it still works
    assert git(other, "status", "--porcelain").stdout.strip().startswith("A  staged.txt")


def test_review_a_only_the_vaulted_worktrees_own_entry_is_removed(setup: Setup) -> None:
    admin = Path(git(setup.wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    found = _detect(setup)
    _apply(setup, [found[aa.CAT_WORKTREE]])
    assert not admin.exists()
    assert (setup.repo / ".git" / "worktrees").is_dir()
    assert not worktree_listed(setup.repo, setup.wt)


def test_review_a_a_pointer_to_someone_elses_admin_dir_is_not_removed(
    setup: Setup, tmp_path: Path
) -> None:
    """An edited `.git` pointer must not make us delete (or back up) a DIFFERENT entry."""
    other = add_worktree(setup.repo, "proj-wt-victim", "victim")
    victim_admin = Path(git(other, "rev-parse", "--absolute-git-dir").stdout.strip())
    found = _detect(setup)
    pointer = setup.wt / ".git"
    pointer.unlink()
    pointer.write_bytes(("gitdir: " + victim_admin.as_posix() + chr(10)).encode("utf-8"))
    vaulted = tmp_path / "vaulted-copy"
    shutil.copytree(setup.wt, vaulted)
    note = aa.after_vault_move(found[aa.CAT_WORKTREE].path, vaulted)
    assert note is not None and "belongs to another path" in note
    assert victim_admin.is_dir()


def test_review_a_a_candidate_that_lies_about_its_group_still_gets_the_reproof(
    setup: Setup,
) -> None:
    """W4: the apply-time re-proof and the rename-only move key on group OR category."""
    import dataclasses

    found = _detect(setup)
    (setup.wt / "README.md").write_text("uncommitted work", encoding="utf-8")
    liar = dataclasses.replace(found[aa.CAT_WORKTREE], category_group="temp_and_browser_caches")
    report = _apply(setup, [liar])
    assert _skipped(report) == ["agent_artifact_not_provably_finished"]
    assert (setup.wt / "README.md").read_text(encoding="utf-8") == "uncommitted work"
    liar2 = dataclasses.replace(found[aa.CAT_SCRATCH], category="dev_artifact_pycache")
    (setup.session / "scratchpad" / "fresh.txt").write_text("now", encoding="utf-8")
    assert _skipped(_apply(setup, [liar2])) == ["agent_artifact_not_provably_finished"]
    assert executor._rename_only("temp_and_browser_caches", aa.CAT_WORKTREE) == {
        "allow_copy_fallback": False
    }
    assert executor._rename_only("dev_artifacts", "dev_artifact_pycache") == {}


def test_review_a_restore_after_the_repository_moved_writes_nothing_at_the_old_path(
    setup: Setup,
) -> None:
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    moved_repo = setup.repo.parent / "proj-moved"
    setup.repo.rename(moved_repo)
    restored = restore_batch(
        report.batch_id, manifest_path=setup.manifest, vault_dir=setup.vault, safety=setup.safety
    )
    assert restored.files_succeeded == 1  # the directory is back
    assert setup.wt.is_dir()
    assert not setup.repo.exists(), "a ghost .git/worktrees skeleton was created at the old path"
    assert (setup.wt / aa.ADMIN_BACKUP_DIR / "meta.json").is_file()  # kept for a manual repair


def test_review_a_restore_into_a_different_repository_writes_nothing_into_it(
    setup: Setup,
) -> None:
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    moved_repo = setup.repo.parent / "proj-moved"
    setup.repo.rename(moved_repo)
    stranger = make_repo(setup.root, "proj")  # a different repository now lives at the old path
    commit_file(stranger, "other.txt", "different history\n", "a different root")
    restore_batch(
        report.batch_id, manifest_path=setup.manifest, vault_dir=setup.vault, safety=setup.safety
    )
    assert setup.wt.is_dir()
    assert not (stranger / ".git" / "worktrees").exists()


def test_review_a_the_move_note_names_what_happened_when_prune_is_not_needed(setup: Setup) -> None:
    found = _detect(setup)
    moved = setup.vault / "manual"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(setup.wt), str(moved))
    assert aa.after_vault_move(found[aa.CAT_WORKTREE].path, moved) is None
    assert not worktree_listed(setup.repo, setup.wt)


def test_survivor_b11_after_restore_says_when_the_repository_is_no_longer_there(
    setup: Setup,
) -> None:
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    vaulted = next(i.vault_path for i in report.items if i.category == aa.CAT_WORKTREE)
    assert vaulted is not None
    shutil.move(str(vaulted), str(setup.wt))  # put the directory back by hand
    setup.repo.rename(setup.repo.parent / "proj-gone")
    note = aa.after_restore(setup.wt)
    assert note is not None and "no longer at" in note
    assert not setup.repo.exists()


def test_survivor_b17_after_vault_move_notices_if_git_still_lists_the_worktree(
    setup: Setup,
) -> None:
    found = _detect(setup)
    real = aa.run_git

    def lying(args: object, cwd: Path) -> aa.GitResult:
        if tuple(args)[:2] == ("worktree", "list"):  # type: ignore[call-overload]
            main = "worktree " + str(setup.repo)
            ours = "worktree " + str(setup.wt)
            return aa.GitResult(0, "\0".join([main, "HEAD x", "", ours, "HEAD y", "", ""]))
        return real(args, cwd)  # type: ignore[arg-type]

    moved = setup.vault / "manual"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(setup.wt), str(moved))
    note = aa.after_vault_move(found[aa.CAT_WORKTREE].path, moved, lying)
    assert note is not None and "still lists the worktree" in note


def test_review_a3_restore_never_takes_over_a_newer_worktree_that_reused_the_admin_name(
    setup: Setup,
) -> None:
    """F2: after the move, `git worktree add` reuses the freed admin name; restoring then
    deleted our backup and `worktree repair` repointed the NEW worktree's index and HEAD."""
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    newer = setup.root / "other" / setup.wt.name
    newer.parent.mkdir()
    git(setup.repo, "worktree", "add", "-q", "--detach", str(newer))
    admin = Path(git(newer, "rev-parse", "--absolute-git-dir").stdout.strip())
    assert admin.name == setup.wt.name  # the trap: the same admin dir name as the vaulted one
    (newer / "staged.txt").write_text("staged work", encoding="utf-8")
    git(newer, "add", "staged.txt")
    owner_before = (admin / "gitdir").read_text(encoding="utf-8")
    restore_batch(
        report.batch_id, manifest_path=setup.manifest, vault_dir=setup.vault, safety=setup.safety
    )
    assert setup.wt.is_dir()  # the directory itself is back
    assert (admin / "gitdir").read_text(encoding="utf-8") == owner_before
    assert worktree_listed(setup.repo, newer)
    assert "A  staged.txt" in git(newer, "status", "--porcelain").stdout
    assert (setup.wt / aa.ADMIN_BACKUP_DIR / "meta.json").is_file()  # kept for a manual repair
    note = aa.after_restore(setup.wt)
    assert note is not None and "belongs to another worktree" in note


def test_review_a3b_restore_treats_an_admin_dir_without_a_readable_owner_as_not_ours(
    setup: Setup,
) -> None:
    """N2b: an admin dir at the saved path whose `gitdir` file is missing names no owner; it may
    belong to another worktree, so restore writes nothing and keeps the backup."""
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    vaulted = next(i.vault_path for i in report.items if i.category == aa.CAT_WORKTREE)
    assert vaulted is not None
    shutil.move(str(vaulted), str(setup.wt))  # put the directory back by hand
    meta = (setup.wt / aa.ADMIN_BACKUP_DIR / "meta.json").read_text(encoding="utf-8")
    admin = Path(json.loads(meta)["admin"])
    admin.mkdir(parents=True)  # present, but no `gitdir` file to say whose it is
    note = aa.after_restore(setup.wt)
    assert note is not None and "belongs to another worktree" in note
    assert list(admin.iterdir()) == []  # nothing was copied into it
    assert (setup.wt / aa.ADMIN_BACKUP_DIR / "meta.json").is_file()


def test_review_a3b_a_commit_made_after_the_proof_keeps_the_admin_entry(setup: Setup) -> None:
    """N5: a detached commit made between the re-proof and the move is reachable only through
    the worktree's admin entry; removing it would let `git gc` drop the commit."""
    found = _detect(setup)
    moved = setup.vault / "manual"
    moved.parent.mkdir(parents=True, exist_ok=True)
    git(setup.wt, "checkout", "-q", "--detach")
    commit_file(setup.wt, "late.txt", "written after the proof\n", "late commit")
    late = git(setup.wt, "rev-parse", "HEAD").stdout.strip()
    shutil.move(str(setup.wt), str(moved))
    note = aa.after_vault_move(found[aa.CAT_WORKTREE].path, moved)
    assert note is not None and "admin entry was kept" in note
    admin = setup.repo / ".git" / "worktrees" / setup.wt.name
    assert (admin / "HEAD").read_text(encoding="utf-8").strip() == late
    assert not (moved / aa.ADMIN_BACKUP_DIR).exists()
    git(setup.repo, "gc", "-q", "--prune=now")
    assert git(setup.repo, "cat-file", "-e", late, check=False).returncode == 0


def test_review_a3b_restore_does_not_adopt_a_strangers_admin_dir_at_the_same_path(
    setup: Setup,
) -> None:
    """N6/Q2b: another worktree was added at THIS path after the move and its folder deleted;
    its admin dir names our path but holds its own HEAD and index."""
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    git(setup.repo, "worktree", "add", "-q", "-b", "stranger", str(setup.wt))
    shutil.rmtree(setup.wt)  # its folder is gone, its admin entry stays
    admin = setup.repo / ".git" / "worktrees" / setup.wt.name
    stranger_head = (admin / "HEAD").read_bytes()
    restore_batch(
        report.batch_id, manifest_path=setup.manifest, vault_dir=setup.vault, safety=setup.safety
    )
    assert setup.wt.is_dir()
    assert (admin / "HEAD").read_bytes() == stranger_head  # nothing written into it
    assert (setup.wt / aa.ADMIN_BACKUP_DIR / "meta.json").is_file()  # our backup is kept
    assert not (setup.wt / ".git").exists()  # detached: git in here cannot use the stranger's
    assert (setup.wt / aa.DETACHED_POINTER).is_file()


def test_review_a3b_a_refused_restore_detaches_the_pointer_from_the_other_worktree(
    setup: Setup,
) -> None:
    """N6/p2: after the F2 refusal, git run in the restored folder used the NEWER worktree's
    admin dir (its index and HEAD)."""
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    newer = setup.root / "other" / setup.wt.name
    newer.parent.mkdir()
    git(setup.repo, "worktree", "add", "-q", "--detach", str(newer))
    restore_batch(
        report.batch_id, manifest_path=setup.manifest, vault_dir=setup.vault, safety=setup.safety
    )
    assert not (setup.wt / ".git").exists()
    assert (setup.wt / aa.DETACHED_POINTER).is_file()
    assert worktree_listed(setup.repo, newer)


def test_review_a3b_restore_refuses_an_admin_dir_with_our_head_but_no_owner(setup: Setup) -> None:
    """N2b: the same HEAD is not proof of ownership; a stranger worktree at our commit leaves an
    identical HEAD. Without a readable `gitdir` the admin dir is not ours."""
    found = _detect(setup)
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    vaulted = next(i.vault_path for i in report.items if i.category == aa.CAT_WORKTREE)
    assert vaulted is not None
    shutil.move(str(vaulted), str(setup.wt))  # put the directory back by hand
    backup = setup.wt / aa.ADMIN_BACKUP_DIR
    admin = Path(json.loads((backup / "meta.json").read_text(encoding="utf-8"))["admin"])
    admin.mkdir(parents=True)
    shutil.copyfile(backup / "admin" / "HEAD", admin / "HEAD")  # same HEAD, no `gitdir`
    note = aa.after_restore(setup.wt)
    assert note is not None and "belongs to another worktree" in note
    assert sorted(p.name for p in admin.iterdir()) == ["HEAD"]  # nothing was copied into it
    assert (backup / "meta.json").is_file()


def test_review_a3b_after_vault_move_keeps_an_admin_entry_with_no_readable_owner(
    setup: Setup,
) -> None:
    found = _detect(setup)
    admin = setup.repo / ".git" / "worktrees" / setup.wt.name
    moved = setup.vault / "manual"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(setup.wt), str(moved))
    (admin / "gitdir").unlink()
    note = aa.after_vault_move(found[aa.CAT_WORKTREE].path, moved)
    assert note is not None and "belongs to another path" in note
    assert (admin / "HEAD").is_file()
    assert not (moved / aa.ADMIN_BACKUP_DIR).exists()


@pytest.mark.parametrize("relative_config", [False, True])
def test_review_a4_restore_does_not_rewrite_another_worktrees_links(
    setup: Setup, relative_config: bool
) -> None:
    """F3: `git worktree repair` rewrites EVERY linked worktree into the configured path style;
    with `worktree.useRelativePaths=true` it rewrote another worktree's `.git` file and admin
    `gitdir`, outside the vault."""
    found = _detect(setup)
    other = setup.root / "proj-wt-abs"
    git(setup.repo, "worktree", "add", "-q", "-b", "absb", str(other))
    if relative_config:
        git(setup.repo, "config", "worktree.useRelativePaths", "true")
    other_admin = setup.repo / ".git" / "worktrees" / other.name
    pointer, owner = (other / ".git").read_bytes(), (other_admin / "gitdir").read_bytes()
    report = _apply(setup, [found[aa.CAT_WORKTREE]])
    restore_batch(
        report.batch_id, manifest_path=setup.manifest, vault_dir=setup.vault, safety=setup.safety
    )
    assert worktree_listed(setup.repo, setup.wt)  # ours is repaired
    assert (other / ".git").read_bytes() == pointer
    assert (other_admin / "gitdir").read_bytes() == owner
