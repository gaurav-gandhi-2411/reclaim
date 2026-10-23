"""Fixture suite for the agent-artifacts verdicts (docs/specs/agent-artifacts.md section 8).

Every case builds a REAL git repository in a tmp dir and states the correct verdict. The hard gate
is ZERO wrong offers: every `refused` case below must keep the artifact from being offered, and
only the explicit `offered` cases may offer one. The first failed rule is asserted too, so a
refusal for the wrong reason cannot hide a missing check."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from agent_fixtures import (
    World,
    add_worktree,
    commit_file,
    git,
    make_junction,
    make_repo,
    make_session,
    make_world,
    mark_pushed,
    push_branch,
)

from reclaim import agent_artifacts as aa
from reclaim.procscan import ProcessSnapshot, fake_scan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _wt(tmp_path: Path, *, branch: str | None = "feat") -> tuple[Path, Path]:
    repo = make_repo(tmp_path, "proj")
    return repo, add_worktree(repo, "proj-wt-feat", branch)


def _verdict(world: World, wt: Path, repo: Path) -> aa.ArtifactVerdict:
    return aa.evaluate_worktree(world.env, wt, repo)


def _refused(v: aa.ArtifactVerdict, rule: str) -> None:
    assert not v.offered, f"WRONG OFFER (rule {rule} should have refused): {v.held}"
    assert v.failed_rule == rule, (v.failed_rule, v.failed_detail)


# --- worktrees that ARE offered -----------------------------------------------------------


def test_clean_pushed_worktree_is_offered_with_every_rule_listed(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    world = make_world(tmp_path)
    v = _verdict(world, wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)
    rules = " ".join(v.held)
    for rule in ("W1", "W2", "W4", "W5", "W6", "W7", "W8", "V"):
        assert rule in rules, rule
    assert v.project == "proj" and v.branch == "feat"


def test_pushed_feature_branch_commit_is_offered(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, "feature.txt", "work\n", "feature work")
    push_branch(wt, "feat")
    assert _verdict(make_world(tmp_path), wt, repo).offered


def test_detached_head_at_a_pushed_commit_is_offered(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-detached", None)
    assert _verdict(make_world(tmp_path), wt, repo).offered


def test_a_stash_in_the_repository_does_not_block_a_worktree(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    (repo / "README.md").write_text("edited in the main checkout\n", encoding="utf-8")
    git(repo, "stash", "push", "-q")
    assert _verdict(make_world(tmp_path), wt, repo).offered  # stashes are repo-wide, they survive


def test_regenerable_ignored_directories_do_not_block(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", "node_modules/\n__pycache__/\n.venv/\n", "ignore deps")
    push_branch(wt, "feat")
    for d in ("node_modules/pkg", "__pycache__", ".venv/lib"):
        (wt / d).mkdir(parents=True)
        (wt / d / "x.bin").write_bytes(b"x")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)


# --- worktrees that must be REFUSED ---------------------------------------------------------


def test_dirty_tracked_file_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    (wt / "README.md").write_text("changed\n", encoding="utf-8")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


def test_untracked_file_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    (wt / "new.txt").write_text("new\n", encoding="utf-8")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_hidden_local_edit_is_refused(tmp_path: Path, flag: str) -> None:
    repo, wt = _wt(tmp_path)
    git(wt, "update-index", flag, "README.md")
    (wt / "README.md").write_text("hidden edit\n", encoding="utf-8")
    status = git(wt, "status", "--porcelain").stdout.strip()
    assert status == ""  # the whole point: plain status cannot see it
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


def test_unpushed_commit_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, "local.txt", "only here\n", "unpushed")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_detached_head_ahead_of_the_remote_is_refused(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-detached", None)
    commit_file(wt, "ahead.txt", "ahead\n", "detached commit")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_no_remote_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(repo, "remote", "remove", "origin")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


@pytest.mark.parametrize(
    "url",
    [
        "{bare}",
        "file:///{bare}",
        "C:/somewhere/else",
        r"\\server\share\repo.git",
        "../other",
        "/tmp/x",
    ],
)
def test_a_remote_that_is_a_local_path_is_not_a_backup(tmp_path: Path, url: str) -> None:
    repo, wt = _wt(tmp_path)
    bare = (tmp_path / "proj-remote.git").as_posix()
    git(repo, "remote", "set-url", "origin", url.format(bare=bare))
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


@pytest.mark.parametrize(
    ("url", "real"),
    [
        ("https://github.com/o/r.git", True),
        ("ssh://git@host/o/r.git", True),
        ("git://host/o/r.git", True),
        ("git@github.com:o/r.git", True),
        ("http://host/o/r.git", False),
        ("file:///tmp/r.git", False),
        ("C:/r.git", False),
        ("D:\\r.git", False),
        ("//server/share/r.git", False),
        ("../r.git", False),
        ("", False),
    ],
)
def test_remote_url_allow_list(url: str, real: bool) -> None:
    assert aa.is_real_remote_url(url) is real


def test_branch_not_on_any_remote_is_refused_even_if_main_is_pushed(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(wt, "commit", "-q", "--allow-empty", "-m", "empty local commit")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


@pytest.mark.parametrize(
    "marker", ["MERGE_HEAD", "REVERT_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "BISECT_LOG"]
)
def test_operation_in_progress_file_is_refused(tmp_path: Path, marker: str) -> None:
    repo, wt = _wt(tmp_path)
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    (gitdir / marker).write_text("0" * 40 + "\n", encoding="utf-8")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


@pytest.mark.parametrize("folder", ["rebase-merge", "rebase-apply", "sequencer"])
def test_operation_in_progress_directory_is_refused(tmp_path: Path, folder: str) -> None:
    repo, wt = _wt(tmp_path)
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    (gitdir / folder).mkdir()
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


def test_locked_worktree_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(repo, "worktree", "lock", "--reason", "in use", str(wt))
    _refused(_verdict(make_world(tmp_path), wt, repo), "W1")


def test_prunable_worktree_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    shutil.rmtree(wt)
    _refused(_verdict(make_world(tmp_path), wt, repo), "W1")


def test_the_main_checkout_is_never_a_worktree_candidate(tmp_path: Path) -> None:
    repo, _wt_path = _wt(tmp_path)
    _refused(_verdict(make_world(tmp_path), repo, repo), "W1")


def test_submodule_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    other = make_repo(tmp_path, "other")
    git(wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(other), "sub")
    git(wt, "commit", "-q", "-m", "add submodule")
    push_branch(wt, "feat")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


def test_submodule_hidden_by_ignore_all_is_still_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    other = make_repo(tmp_path, "other")
    git(wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(other), "sub")
    git(wt, "config", "submodule.sub.ignore", "all")
    git(wt, "commit", "-q", "-m", "add submodule")
    push_branch(wt, "feat")
    (wt / "sub" / "dirty.txt").write_text("dirty inside the submodule\n", encoding="utf-8")
    assert git(wt, "status", "--porcelain").stdout.strip() == ""  # hidden from plain status
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


@pytest.mark.parametrize("name", [".env", "data.db", "secret.pem", "build/out.bin", "dist/app.js"])
def test_ignored_content_that_may_not_be_regenerable_is_refused(tmp_path: Path, name: str) -> None:
    repo, wt = _wt(tmp_path)
    top = name.split("/", maxsplit=1)[0]
    commit_file(wt, ".gitignore", f"{top}\n{top}/\n", "ignore")
    push_branch(wt, "feat")
    target = wt / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("must survive", encoding="utf-8")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W8")
    assert target.read_text(encoding="utf-8") == "must survive"


def test_junction_inside_node_modules_is_refused_and_its_target_is_untouched(
    tmp_path: Path,
) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", "node_modules/\n", "ignore")
    push_branch(wt, "feat")
    outside = tmp_path / "outside-data"
    outside.mkdir()
    (outside / "precious.txt").write_text("precious", encoding="utf-8")
    (wt / "node_modules").mkdir()
    make_junction(wt / "node_modules" / "linked", outside)
    _refused(_verdict(make_world(tmp_path), wt, repo), "W8")
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "precious"


def test_excluded_repository_name_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    _refused(_verdict(make_world(tmp_path, exclusions=("proj",)), wt, repo), "W7")


def test_excluded_wt_suffix_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    _refused(_verdict(make_world(tmp_path, exclusions=("proj-wt-feat",)), wt, repo), "W7")


def test_exclusion_beats_a_perfectly_finished_worktree_through_its_content(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, "intent-router/readme.txt", "inside\n", "excluded name inside")
    push_branch(wt, "feat")
    _refused(_verdict(make_world(tmp_path, exclusions=("intent-router",)), wt, repo), "W7")


def test_live_process_with_cwd_inside_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    live = fake_scan([ProcessSnapshot(77, "node.exe", str(wt / "src"), "node server.js")])
    _refused(_verdict(make_world(tmp_path, procs=live), wt, repo), "W5")


@pytest.mark.parametrize("spelling", ["plain", "slashes", "upper", "extended"])
def test_live_process_with_the_path_in_its_command_line_is_refused(
    tmp_path: Path, spelling: str
) -> None:
    repo, wt = _wt(tmp_path)
    shown = {
        "plain": str(wt / "a.js"),
        "slashes": str(wt / "a.js").replace(chr(92), "/"),
        "upper": str(wt / "a.js").upper(),
        "extended": chr(92) * 2 + "?" + chr(92) + str(wt / "a.js"),
    }[spelling]
    live = fake_scan(
        [ProcessSnapshot(78, "code.exe", "C:" + chr(92) + "elsewhere", f'code "{shown}"')]
    )
    _refused(_verdict(make_world(tmp_path, procs=live), wt, repo), "W5")


def test_an_unreadable_same_user_process_fails_closed(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    blocked = fake_scan(blocked=[(5, "elevated.exe", "OpenProcess failed (5)")])
    _refused(_verdict(make_world(tmp_path, procs=blocked), wt, repo), "W5")


def test_process_inspection_unavailable_fails_closed(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    broken = aa.ProcessScan(False, (), (), 0, "no Win32")
    _refused(_verdict(make_world(tmp_path, procs=broken), wt, repo), "W5")


def test_a_recent_session_transcript_for_the_worktree_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    world = make_world(tmp_path)
    slug = aa._slug(wt)
    folder = world.projects / slug
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text("{}", encoding="utf-8")
    _refused(_verdict(world, wt, repo), "W6")


def test_an_old_session_transcript_does_not_block(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    world = make_world(tmp_path)
    folder = world.projects / aa._slug(wt)
    folder.mkdir(parents=True)
    old = folder / "session.jsonl"
    old.write_text("{}", encoding="utf-8")
    stamp = time.time() - 30 * 86400
    os.utime(old, (stamp, stamp))
    assert _verdict(world, wt, repo).offered


def test_a_worktree_on_another_volume_than_the_vault_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, wt = _wt(tmp_path)
    monkeypatch.setattr(aa, "same_volume", lambda *_a: False)
    _refused(_verdict(make_world(tmp_path), wt, repo), "V")


def test_git_missing_or_failing_fails_closed(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    world = make_world(tmp_path, run=lambda _args, _cwd: aa.GitResult(-1, "", "git not found"))
    v = _verdict(world, wt, repo)
    assert not v.offered


def test_a_git_error_code_other_than_zero_is_never_read_as_clean(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    real = aa.run_git

    def flaky(args: object, cwd: Path) -> aa.GitResult:
        if "status" in list(args):  # type: ignore[call-overload]
            return aa.GitResult(128, "", "fatal: bad ref")
        return real(args, cwd)  # type: ignore[arg-type]

    _refused(_verdict(make_world(tmp_path, run=flaky), wt, repo), "W2")


def test_worktree_whose_repository_is_gone_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    shutil.rmtree(repo / ".git" / "worktrees")
    v = aa.evaluate_path(make_world(tmp_path).env, wt, aa.CAT_WORKTREE)
    assert not v.offered


def test_unknown_category_is_never_offered(tmp_path: Path) -> None:
    assert not aa.evaluate_path(make_world(tmp_path).env, tmp_path, "something_else").offered


# --- throwaway clones -----------------------------------------------------------------------


def _clone(tmp_path: Path, name: str = "proj-verify1") -> Path:
    return make_repo(tmp_path, name)


def test_pushed_clean_verify_clone_is_offered(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    assert v.offered, (v.failed_rule, v.failed_detail)
    assert "C1" in " ".join(v.held)


@pytest.mark.parametrize("name", ["_verify305", "gg-portfolio-verify2", "X-VERIFY-copy"])
def test_clone_name_patterns_are_case_insensitive(tmp_path: Path, name: str) -> None:
    assert aa.evaluate_clone(make_world(tmp_path).env, _clone(tmp_path, name)).offered


def test_a_repository_without_a_throwaway_name_is_not_a_clone_candidate(tmp_path: Path) -> None:
    v = aa.evaluate_clone(make_world(tmp_path).env, make_repo(tmp_path, "real-project"))
    _refused(v, "C1")


def test_clone_with_an_unpushed_other_branch_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    git(clone, "checkout", "-q", "-b", "side")
    commit_file(clone, "side.txt", "only on side\n", "side work")
    git(clone, "checkout", "-q", "main")  # HEAD is pushed; the other branch is not
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_clone_with_an_unpushed_tag_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    commit_file(clone, "t.txt", "tagged\n", "tagged commit")
    git(clone, "tag", "v-local")
    git(clone, "reset", "-q", "--hard", "HEAD~1")  # HEAD is pushed, the tag points at lost work
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_clone_with_a_stash_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    (clone / "README.md").write_text("stashed edit\n", encoding="utf-8")
    git(clone, "stash", "push", "-q")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C2")


def test_clone_with_a_linked_worktree_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    add_worktree(clone, "proj-verify1-wt", "wt")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C6")


def test_dirty_clone_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    (clone / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "W2")


def test_clone_in_use_by_a_process_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    live = fake_scan([ProcessSnapshot(9, "python.exe", str(clone), "python -m pytest")])
    _refused(aa.evaluate_clone(make_world(tmp_path, procs=live).env, clone), "C5")


def test_excluded_clone_is_refused(tmp_path: Path) -> None:
    clone = _clone(tmp_path, "intent-router-verify")
    _refused(
        aa.evaluate_clone(make_world(tmp_path, exclusions=("intent-router",)).env, clone), "C7"
    )


def test_a_clone_whose_git_dir_is_a_file_is_not_standalone(tmp_path: Path) -> None:
    _repo, wt = _wt(tmp_path)
    renamed = wt.parent / "proj-verify-wt"
    shutil.move(str(wt), str(renamed))
    _refused(aa.evaluate_clone(make_world(tmp_path).env, renamed), "C1")


# --- session scratch --------------------------------------------------------------------------


def test_idle_session_scratch_is_offered(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=7.1)
    v = aa.evaluate_scratch(world.env, session)
    assert v.offered, (v.failed_rule, v.failed_detail)


def test_scratch_idle_for_6_9_days_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _refused(aa.evaluate_scratch(world.env, make_session(world, age_days=6.9)), "S2")


def test_one_fresh_file_deep_inside_keeps_the_whole_session(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=30)
    deep = session / "scratchpad" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "fresh.txt").write_text("now", encoding="utf-8")  # directory mtimes stay old
    _refused(aa.evaluate_scratch(world.env, session), "S2")


def test_session_with_extras_is_still_a_candidate(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    (session / "srv-b.pid").write_text("1234", encoding="utf-8")
    _age_tree(session, world, 20)  # creating the file bumped the directory mtime too
    assert aa.evaluate_scratch(world.env, session).offered


def test_non_uuid_sibling_is_never_a_session(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    sibling = world.temp_claude / "C--work-proj" / "shima"
    sibling.mkdir(parents=True)
    _refused(aa.evaluate_scratch(world.env, sibling), "S1")


def test_a_directory_outside_temp_claude_is_never_a_session(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    elsewhere = tmp_path / "other" / "slug" / "123e4567-e89b-12d3-a456-426614174000"
    elsewhere.mkdir(parents=True)
    _refused(aa.evaluate_scratch(world.env, elsewhere), "S1")


def test_a_fresh_session_transcript_keeps_old_scratch(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    folder = world.projects / session.parent.name
    folder.mkdir(parents=True)
    (folder / f"{session.name}.jsonl").write_text("{}", encoding="utf-8")
    _refused(aa.evaluate_scratch(world.env, session), "S3")


def test_a_transcript_of_another_session_does_not_block(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    folder = world.projects / session.parent.name
    folder.mkdir(parents=True)
    (folder / "123e4567-e89b-12d3-a456-426614174999.jsonl").write_text("{}", encoding="utf-8")
    assert aa.evaluate_scratch(world.env, session).offered


def test_scratch_with_a_live_process_by_path_or_by_session_id_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    by_path = fake_scan([ProcessSnapshot(1, "node.exe", str(session), "node")])
    by_id = fake_scan(
        [ProcessSnapshot(2, "claude.exe", "C:" + chr(92) + "w", f"claude --session {session.name}")]
    )
    for procs in (by_path, by_id):
        w = make_world(tmp_path, procs=procs)
        _refused(aa.evaluate_scratch(w.env, session), "S4")


def test_scratch_with_a_junction_inside_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "f.txt").write_text("keep", encoding="utf-8")
    make_junction(session / "scratchpad" / "link", outside)
    _refused(aa.evaluate_scratch(world.env, session), "S8")
    assert (outside / "f.txt").read_text(encoding="utf-8") == "keep"


def test_excluded_session_slug_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path, exclusions=("gg-portfolio",))
    session = make_session(world, slug="C--Users-gaura-ml-projects-gg-portfolio", age_days=20)
    _refused(aa.evaluate_scratch(world.env, session), "S5")


def test_mixed_case_slug_is_matched_for_transcripts(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, slug="C--Users-Gaura-Proj", age_days=20)
    folder = world.projects / "c--users-gaura-proj"
    folder.mkdir(parents=True)
    (folder / f"{session.name}.jsonl").write_text("{}", encoding="utf-8")
    _refused(aa.evaluate_scratch(world.env, session), "S3")


def _clone_into(session: Path, name: str = "clone") -> Path:
    repo = session / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_file(repo, "a.txt", "a\n", "initial")
    git(repo, "remote", "add", "origin", "https://example.invalid/c.git")
    mark_pushed(repo, "main")
    return repo


def _age_tree(root: Path, world: World, days: float) -> None:
    stamp = world.env.now - days * 86400
    for dirpath, dirnames, filenames in os.walk(root):
        for n in [*dirnames, *filenames]:
            os.utime(Path(dirpath) / n, (stamp, stamp))
        os.utime(dirpath, (stamp, stamp))


def test_scratch_with_a_pushed_clean_nested_clone_is_offered(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    _clone_into(session)
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    assert v.offered, (v.failed_rule, v.failed_detail)


def test_scratch_with_a_nested_clone_with_an_unpushed_commit_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    repo = _clone_into(session)
    commit_file(repo, "b.txt", "unpushed\n", "unpushed work")
    _age_tree(session, world, 20)
    _refused(aa.evaluate_scratch(world.env, session), "S7")


def test_nested_clone_pushed_head_but_another_branch_or_stash_is_refused(tmp_path: Path) -> None:
    for how in ("branch", "stash"):
        world = make_world(tmp_path / how)
        session = make_session(world, age_days=20)
        repo = _clone_into(session)
        if how == "branch":
            git(repo, "checkout", "-q", "-b", "side")
            commit_file(repo, "s.txt", "side\n", "side")
            git(repo, "checkout", "-q", "main")
        else:
            (repo / "a.txt").write_text("stash me\n", encoding="utf-8")
            git(repo, "stash", "push", "-q")
        _age_tree(session, world, 20)
        _refused(aa.evaluate_scratch(world.env, session), "S7")


def test_nested_git_file_pointing_at_a_missing_git_dir_is_refused(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    broken = session / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: C:/does/not/exist/.git\n", encoding="utf-8")
    _age_tree(session, world, 20)
    _refused(aa.evaluate_scratch(world.env, session), "S7")


def test_scratch_walk_error_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    real = os.scandir

    def flaky(path: object) -> object:
        if str(path).endswith("scratchpad"):
            raise PermissionError(5, "denied")
        return real(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", flaky)
    assert not aa.evaluate_scratch(world.env, session).offered


def test_scratch_on_another_volume_than_the_vault_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    monkeypatch.setattr(aa, "same_volume", lambda *_a: False)
    _refused(aa.evaluate_scratch(world.env, session), "V")


def test_git_invocations_are_read_only_and_never_run_fsmonitor() -> None:
    assert aa._GIT[:2] == ("git", "--no-optional-locks")
    assert "core.fsmonitor=false" in aa._GIT


def test_status_does_not_rewrite_the_index(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    before = (gitdir / "index").stat().st_mtime_ns
    time.sleep(0.05)
    assert _verdict(make_world(tmp_path), wt, repo).offered
    assert (gitdir / "index").stat().st_mtime_ns == before


def test_verdicts_never_modify_the_tree(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    before = sorted(str(p.relative_to(wt)) for p in wt.rglob("*"))
    _verdict(make_world(tmp_path), wt, repo)
    after = sorted(str(p.relative_to(wt)) for p in wt.rglob("*"))
    assert before == after


def test_subprocess_is_real_git_not_a_stub() -> None:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)  # noqa: S607
    assert out.stdout.startswith("git version")


# --- each check on its own: no other check may be what stops these -------------------------------


def test_gitlink_without_gitmodules_or_modules_dir_is_refused(tmp_path: Path) -> None:
    """Only the tracked-gitlink check can see this: no .gitmodules, no <gitdir>/modules."""
    repo, wt = _wt(tmp_path)
    sha = git(wt, "rev-parse", "HEAD").stdout.strip()
    git(wt, "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    git(wt, "commit", "-q", "-m", "gitlink without .gitmodules")
    push_branch(wt, "feat")
    assert not (wt / ".gitmodules").exists()
    assert not (
        Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()) / "modules"
    ).exists()
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W2")
    assert "gitlink" in v.failed_detail


def test_gitmodules_file_alone_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitmodules", "", "empty .gitmodules")
    push_branch(wt, "feat")
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W2")
    assert "submodules present" in v.failed_detail


def test_modules_directory_in_the_git_dir_alone_is_refused(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    (Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()) / "modules").mkdir()
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W2")
    assert "submodules present" in v.failed_detail


def _with(env: aa.AgentEnv, **changes: object) -> aa.AgentEnv:
    return dataclasses.replace(env, **changes)  # type: ignore[arg-type]


def test_user_exclusion_check_stands_on_its_own(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    env = _with(
        make_world(tmp_path).env,
        exclusion_match=lambda _p, _d: "*proj*",
        builtin_block=lambda _p: None,
        pattern_match=lambda _p: None,
    )
    v = aa.evaluate_worktree(env, wt, repo)
    _refused(v, "W7")
    assert v.excluded and "excluded project" in v.failed_detail


def test_validator_block_stands_on_its_own(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    env = _with(
        make_world(tmp_path).env,
        exclusion_match=lambda _p, _d: None,
        builtin_block=lambda _p: "PROTECTED_SYSTEM_ROOT",
        pattern_match=lambda _p: None,
    )
    v = aa.evaluate_worktree(env, wt, repo)
    _refused(v, "W7")
    assert "safety validator" in v.failed_detail


def test_repository_level_exclusion_stands_on_its_own(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    env = _with(
        make_world(tmp_path).env,
        exclusion_match=lambda _p, _d: None,
        builtin_block=lambda _p: None,
        pattern_match=lambda _p: "*proj*",
    )
    v = aa.evaluate_worktree(env, wt, repo)
    _refused(v, "W7")
    assert v.excluded  # recognised from the path, before any git call


def test_the_unpushed_check_names_the_commit(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, "x.txt", "x", "unpushed")
    sha = git(wt, "rev-parse", "HEAD").stdout.strip()
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W4")
    assert sha[:12] in v.failed_detail  # it found the specific commit, not a generic failure


# --- an excluded project is recognised from its PATH and never inspected or surfaced -------------


def _no_git(args: object, cwd: Path) -> aa.GitResult:
    raise AssertionError(f"git ran for an excluded project: {list(args)} in {cwd}")  # type: ignore[call-overload]


def test_excluded_repository_worktree_is_never_inspected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "secretproj")
    wt = add_worktree(repo, "wt-feature", "feat")  # the worktree's own name says nothing
    world = make_world(tmp_path, exclusions=("secretproj",), run=_no_git)
    by_repo = aa.evaluate_worktree(world.env, wt, repo)
    assert by_repo.excluded and not by_repo.offered and by_repo.failed_rule == "W7"
    via_path = aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)  # reads only the .git pointer
    assert via_path.excluded and via_path.failed_rule == "W7"


def test_worktree_with_an_excluded_name_is_never_inspected(tmp_path: Path) -> None:
    _repo, wt = _wt(tmp_path)
    world = make_world(tmp_path, exclusions=("proj-wt-feat",), run=_no_git)
    v = aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)  # path match: not even the pointer is read
    assert v.excluded and not v.offered


def test_excluded_clone_is_never_inspected(tmp_path: Path) -> None:
    clone = make_repo(tmp_path, "intent-router-verify")
    world = make_world(tmp_path, exclusions=("intent-router",), run=_no_git)
    v = aa.evaluate_clone(world.env, clone)
    assert v.excluded and not v.offered and v.failed_rule == "C7"


def test_excluded_scratch_is_never_walked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world(tmp_path, exclusions=("gg-portfolio",), run=_no_git)
    session = make_session(world, slug="C--Users-gaura-ml-projects-gg-portfolio", age_days=30)

    def no_walk(*_a: object, **_k: object) -> None:
        raise AssertionError("an excluded scratch directory was walked")

    monkeypatch.setattr(aa, "walk_facts", no_walk)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered and v.failed_rule == "S5"


def test_the_report_never_lists_an_excluded_project(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "secretproj")
    wt = add_worktree(repo, "wt-feature", "feat")
    world = make_world(tmp_path, exclusions=("secretproj",), run=_no_git)
    verdicts = [aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)]
    assert aa.report_lines(verdicts) == []
    assert "secretproj" not in " ".join(aa.report_lines(verdicts))


@pytest.mark.parametrize(
    ("rel", "regenerable"),
    [
        ("node_modules/", True),
        ("packages/a/node_modules/", True),
        (".ruff_cache/.gitignore", True),
        (".venv/lib/site-packages/x.py", True),
        ("__pycache__/m.pyc", True),
        ("node_modules", False),  # a FILE named like one
        ("node_modules_backup/", False),  # shares only a prefix
        ("build/", False),
        ("dist/app.js", False),
        (".env", False),
        ("data.db", False),
        ("src/node_modules_file", False),
        ("", False),
    ],
)
def test_ignored_entry_is_regenerable_only_inside_a_regenerable_directory(
    rel: str, regenerable: bool
) -> None:
    assert aa._ignored_entry_is_regenerable(rel) is regenerable


def test_an_ignored_file_inside_a_regenerable_directory_does_not_block(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", ".ruff_cache/\n", "ignore")
    push_branch(wt, "feat")
    (wt / ".ruff_cache").mkdir()
    (wt / ".ruff_cache" / "CACHEDIR.TAG").write_text("x", encoding="utf-8")
    assert _verdict(make_world(tmp_path), wt, repo).offered


def test_an_ignored_data_file_next_to_a_regenerable_directory_still_blocks(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", ".ruff_cache/\nkeep.db\n", "ignore")
    push_branch(wt, "feat")
    (wt / ".ruff_cache").mkdir()
    (wt / ".ruff_cache" / "x").write_text("x", encoding="utf-8")
    (wt / "keep.db").write_text("must survive", encoding="utf-8")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W8")


def test_the_git_pointer_of_an_excluded_worktree_is_not_even_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, wt = _wt(tmp_path)

    def boom(_path: Path) -> Path | None:
        raise AssertionError("the .git pointer of an excluded worktree was read")

    monkeypatch.setattr(aa, "find_repo_of_worktree", boom)
    world = make_world(tmp_path, exclusions=("proj-wt-feat",), run=_no_git)
    v = aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)
    assert v.excluded and not v.offered


# === regression tests for the independent review (pass A): each reproduces a reported hole ===


def _bare(tmp_path: Path, name: str = "elsewhere.git") -> Path:
    bare = tmp_path / name
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    return bare


def test_review_a_push_url_that_is_a_local_path_is_not_a_backup(tmp_path: Path) -> None:
    """F5: `pushurl` -> a local bare repo still updates refs/remotes/origin/*, but is no backup."""
    repo, wt = _wt(tmp_path)
    git(repo, "remote", "set-url", "--push", "origin", str(_bare(tmp_path)))
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W4")
    assert "no remote with a real URL" in v.failed_detail


def test_review_a_push_instead_of_rewriting_to_a_local_path_is_not_a_backup(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    bare = _bare(tmp_path)
    git(repo, "config", f"url.{bare.as_posix()}.pushInsteadOf", "https://example.invalid/")
    assert git(repo, "remote", "get-url", "--push", "origin").stdout.strip() == (
        bare.as_posix() + "proj.git"
    )
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a_a_second_push_url_that_is_local_taints_the_remote(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(repo, "remote", "set-url", "--push", "--add", "origin", "https://example.invalid/proj.git")
    git(repo, "remote", "set-url", "--push", "--add", "origin", str(_bare(tmp_path)))
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a_a_real_https_push_url_is_fine(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(repo, "remote", "set-url", "--push", "origin", "https://example.invalid/other.git")
    assert _verdict(make_world(tmp_path), wt, repo).offered


@pytest.mark.parametrize(
    ("url", "real"),
    [
        ("ssh://localhost/o/r.git", False),
        ("ssh://127.0.0.1/o/r.git", False),
        ("https://localhost/o/r.git", False),
        ("ssh://host/D:/backup/proj.git", False),
        ("ssh://host/d:\\backup\\proj.git", False),
        ("git@localhost:o/r.git", False),
        ("git@host:D:/backup/r.git", False),
        ("https://", False),
        ("ssh://git@github.com/o/r.git", True),
        ("https://gitlab.com/o/r.git", True),
        ("git@github.com:o/r.git", True),
    ],
)
def test_review_a_remote_url_allow_list_rejects_loopback_and_drive_paths(
    url: str, real: bool
) -> None:
    assert aa.is_real_remote_url(url) is real


@pytest.mark.parametrize(
    "ref", ["refs/wip/save", "refs/original/refs/heads/main", "refs/jj/keep/abc", "refs/custom/x"]
)
def test_review_a_commit_reachable_only_from_a_custom_ref_keeps_a_clone(
    tmp_path: Path, ref: str
) -> None:
    """F2: `--branches --tags HEAD` ignored every other ref namespace."""
    clone = _clone(tmp_path)
    commit_file(clone, "work.txt", "only here\n", "unpushed work")
    git(clone, "update-ref", ref, "HEAD")
    git(clone, "reset", "-q", "--hard", "HEAD~1")  # HEAD, branches and tags are all pushed again
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_review_a_unpushed_notes_keep_a_clone(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    git(clone, "notes", "add", "-m", "an unpushed note", "HEAD")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def _nested_repo(inside: Path, *, pushed: bool, dirty: bool = False) -> Path:
    inside.mkdir(parents=True)
    git(inside, "init", "-q", "-b", "main")
    commit_file(inside, "dep.py", "x\n", "dep")
    if pushed:
        git(inside, "remote", "add", "origin", "https://example.invalid/dep.git")
        mark_pushed(inside, "main")
    if dirty:
        (inside / "dirty.txt").write_text("d", encoding="utf-8")
    return inside


@pytest.mark.parametrize("where", ["node_modules/dep", ".venv/src/pkg"])
def test_review_a_unpushed_repo_inside_a_regenerable_directory_keeps_the_worktree(
    tmp_path: Path, where: str
) -> None:
    """F3: pip editable VCS installs create `.venv/src/<pkg>` repositories."""
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", "node_modules/\n.venv/\n", "ignore")
    push_branch(wt, "feat")
    nested = _nested_repo(wt / where, pushed=False)
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W9")
    assert str(nested) in v.failed_detail


def test_review_a_dirty_repo_inside_a_regenerable_directory_keeps_the_worktree(
    tmp_path: Path,
) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", ".venv/\n", "ignore")
    push_branch(wt, "feat")
    _nested_repo(wt / ".venv" / "src" / "pkg", pushed=True, dirty=True)
    _refused(_verdict(make_world(tmp_path), wt, repo), "W9")


def test_review_a_a_pushed_clean_repo_inside_does_not_block(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", "node_modules/\n", "ignore")
    push_branch(wt, "feat")
    _nested_repo(wt / "node_modules" / "dep", pushed=True)
    v = _verdict(make_world(tmp_path), wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)
    assert "W9 1 nested repositories" in " ".join(v.held)


def test_review_a_unpushed_repo_inside_a_clone_keeps_the_clone(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    commit_file(clone, ".gitignore", ".venv/\n", "ignore")
    mark_pushed(clone, "main")
    _nested_repo(clone / ".venv" / "src" / "pkg", pushed=False)
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C8")


def test_review_a_bare_repository_in_scratch_keeps_the_session(tmp_path: Path) -> None:
    """F6: the only copy of a commit can sit in a bare repository (no `.git` entry to find)."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    bare = session / "scratchpad" / "origin.git"
    # Build the bare repo at a short path and move it into scratch: pushing into it in place
    # made receive-pack write `objects/tmp_objdir-incoming-XXXXXX/<2>/<38>`, 265 characters
    # on the CI runner's longer temp path, past MAX_PATH ("Filename too long").
    staging = tmp_path / "_staging.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(staging))
    scratch_repo = tmp_path / "_writer"
    scratch_repo.mkdir()
    git(scratch_repo, "init", "-q", "-b", "main")
    commit_file(scratch_repo, "a", "a", "a")
    git(scratch_repo, "push", "-q", str(staging), "main")
    shutil.rmtree(
        scratch_repo, onexc=lambda func, path, _exc: (Path(path).chmod(0o700), func(path))
    )  # now the bare repo holds the only copy
    shutil.move(str(staging), str(bare))
    assert git(bare, "rev-parse", "--verify", "-q", "main").stdout.strip()
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert "bare repository" in v.failed_detail


def test_review_a_bare_repository_inside_a_worktree_keeps_it(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, ".gitignore", "node_modules/\n", "ignore")
    push_branch(wt, "feat")
    (wt / "node_modules").mkdir()
    git(wt, "init", "-q", "--bare", "-b", "main", str(wt / "node_modules" / "stash.git"))
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W9")
    assert "bare repository" in v.failed_detail


def test_review_a_the_repository_own_git_dir_is_not_mistaken_for_a_bare_repo(
    tmp_path: Path,
) -> None:
    clone = _clone(tmp_path)  # has .git/{HEAD,objects,refs}
    assert aa.evaluate_clone(make_world(tmp_path).env, clone).offered


def test_review_a_a_tracked_directory_named_venv_is_source_not_a_cache(tmp_path: Path) -> None:
    """F4: a project that ships a package called `venv` keeps its ignored files out of "cache"."""
    repo, wt = _wt(tmp_path)
    commit_file(wt, "venv/tool.py", "print(1)\n", "tracked source")
    commit_file(wt, ".gitignore", "*.secretlocal\n", "ignore")
    push_branch(wt, "feat")
    (wt / "venv" / "keys.secretlocal").write_text("only copy", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W8")
    assert "tracked source lives under 'venv'" in v.failed_detail


def test_review_a_tracked_node_modules_subdirectory_with_an_ignored_database(
    tmp_path: Path,
) -> None:
    repo, wt = _wt(tmp_path)
    commit_file(wt, "src/node_modules/x.js", "x\n", "tracked")
    commit_file(wt, ".gitignore", "*.db\n", "ignore")
    push_branch(wt, "feat")
    (wt / "src" / "node_modules" / "data.db").write_text("only copy", encoding="utf-8")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W8")


def test_review_a_index_lock_means_a_git_process_is_active(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    (gitdir / "index.lock").write_text("", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W2")
    assert "index.lock" in v.failed_detail


def test_review_a_a_junction_alias_of_an_excluded_repository_is_excluded(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "secretproj")
    wt = add_worktree(repo, "wt-feature", "feat")
    alias = tmp_path / "neutral-name"
    make_junction(alias, repo)
    world = make_world(tmp_path, exclusions=("secretproj",), run=_no_git)
    v = aa.evaluate_worktree(world.env, wt, alias)  # repo handed in through the alias
    assert v.excluded and not v.offered


@pytest.mark.parametrize("excluded", ["my_proj", "my.proj", "My_Proj"])
def test_review_a_exclusion_matches_the_claude_slug_rewrite(tmp_path: Path, excluded: str) -> None:
    world = make_world(tmp_path, exclusions=(excluded,), run=_no_git)
    session = make_session(world, slug="C--work-my-proj", age_days=30)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered and v.failed_rule == "S5"


def test_review_a_an_exclusion_found_inside_the_content_is_hidden_too(tmp_path: Path) -> None:
    world = make_world(tmp_path, exclusions=("intent-router",))
    session = make_session(world, age_days=30)
    (session / "scratchpad" / "intent-router-notes").mkdir()
    _age_tree(session, world, 30)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered
    assert "intent-router" not in v.failed_detail
    assert aa.report_lines([v]) == []


def test_review_a_relative_gitdir_pointer_does_not_depend_on_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, wt = _wt(tmp_path)
    admin = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    pointer = wt / ".git"
    pointer.unlink()  # a hidden file on Windows: cannot be opened for writing
    relative = os.path.relpath(admin, wt).replace(chr(92), "/")
    pointer.write_bytes(("gitdir: " + relative + chr(10)).encode("utf-8"))
    world = make_world(tmp_path)
    for cwd in (tmp_path, repo, wt, tmp_path.parent):
        monkeypatch.chdir(cwd)
        v = aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)
        assert v.offered, (str(cwd), v.failed_rule, v.failed_detail)


# === regression tests for the second independent review (A3): each reproduces a reported hole ===


def _unpushed_commit(wt: Path) -> str:
    """A commit that exists only locally and is reachable from no branch (`commit-tree`)."""
    tree = git(wt, "rev-parse", "HEAD^{tree}").stdout.strip()
    return git(wt, "commit-tree", tree, "-p", "HEAD", "-m", "wip").stdout.strip()


@pytest.mark.parametrize("ref", ["refs/worktree/wip", "refs/bisect/bad", "refs/rewritten/x"])
def test_review_a3_an_unpushed_commit_on_a_per_worktree_ref_keeps_the_worktree(
    tmp_path: Path, ref: str
) -> None:
    """F1: these refs live in the admin dir removed after the move; `git gc` then drops the
    commit for good."""
    repo, wt = _wt(tmp_path)
    git(wt, "update-ref", ref, _unpushed_commit(wt))
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W4")
    assert "is not on any remote ref" in v.failed_detail


def test_review_a3_a_per_worktree_ref_at_a_pushed_commit_does_not_block(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(wt, "update-ref", "refs/worktree/mark", "HEAD")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)
    assert "per-worktree refs" in " ".join(v.held)


def test_review_a3_a_git_warning_with_an_empty_status_is_a_refusal(tmp_path: Path) -> None:
    """F3: git that cannot open a directory warns on stderr, exits 0 and lists nothing."""
    repo, wt = _wt(tmp_path)

    def warns(args: object, cwd: Path) -> aa.GitResult:
        result = aa.run_git(args, cwd)  # type: ignore[arg-type]
        if tuple(args)[:1] == ("status",):  # type: ignore[call-overload]
            return aa.GitResult(
                0, "", "warning: could not open directory 'deep/': Filename too long\n"
            )
        return result

    v = _verdict(make_world(tmp_path, run=warns), wt, repo)
    _refused(v, "W2")
    assert "could not open directory" in v.failed_detail


def test_review_a3_an_untracked_file_deeper_than_max_path_keeps_the_worktree(
    tmp_path: Path,
) -> None:
    repo, wt = _wt(tmp_path)
    deep = Path("\\\\?\\" + str(wt)) / ("a" * 120) / ("b" * 120) / ("c" * 40)
    deep.mkdir(parents=True)
    (deep / "results.csv").write_text("only copy", encoding="utf-8")
    assert len(str(wt)) + 300 > 260
    _refused(_verdict(make_world(tmp_path), wt, repo), "W2")


@pytest.mark.parametrize("name", ["data.", "notes "])
def test_review_a3_a_name_git_cannot_open_keeps_the_worktree(tmp_path: Path, name: str) -> None:
    """F3: Win32 strips a trailing dot or space, so git cannot open `data.` or `notes `."""
    repo, wt = _wt(tmp_path)
    odd = Path("\\\\?\\" + str(wt / name))
    odd.mkdir(parents=True)
    (odd / "results.csv").write_text("only copy", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert not v.offered, f"WRONG OFFER: {v.held}"
    assert v.failed_rule in {"W2", "W8"}, (v.failed_rule, v.failed_detail)


def _upper_git(repo: Path, spelling: str) -> None:
    """Rename `.git` to another case (two steps: NTFS ignores a case-only rename target)."""
    (repo / ".git").rename(repo / ".git-tmp")
    (repo / ".git-tmp").rename(repo / spelling)


@pytest.mark.parametrize("spelling", [".GIT", ".Git"])
def test_review_a3_a_nested_repo_whose_git_dir_is_not_lowercase_is_proven_too(
    tmp_path: Path, spelling: str
) -> None:
    """F4: git on Windows uses `.GIT` as the repository; the walk compared case-sensitively."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    nested = _nested_repo(session / "scratchpad" / "dep", pushed=False)
    _upper_git(nested, spelling)
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert str(nested) in v.failed_detail


def test_review_a3_content_under_a_dot_git_spelled_upper_case_keeps_the_worktree(
    tmp_path: Path,
) -> None:
    """F4: git skips `notes/.GIT/` entirely, so untracked work in it never shows in status."""
    repo, wt = _wt(tmp_path)
    (wt / "notes" / ".GIT").mkdir(parents=True)
    (wt / "notes" / ".GIT" / "secret-work.txt").write_text("only copy", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert not v.offered, f"WRONG OFFER: {v.held}"


def test_review_a3_a_linked_worktree_of_an_excluded_repo_inside_scratch_is_never_touched(
    tmp_path: Path,
) -> None:
    """F5: only the nested `.git` pointer names the excluded repository; no git may run in it.
    (A stand-in name, never one of the real excluded projects.)"""
    excluded = make_repo(tmp_path / "elsewhere", "zzexclproj")
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    session = make_session(world, age_days=20)
    checkout = session / "scratchpad" / "checkout"
    git(excluded, "worktree", "add", "-q", "--detach", str(checkout))
    _age_tree(session, world, 20)
    ran_in: list[Path] = []

    def recording(args: object, cwd: Path) -> aa.GitResult:
        ran_in.append(Path(cwd))
        return aa.run_git(args, cwd)  # type: ignore[arg-type]

    world.env.run_git = recording
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered and v.failed_rule == "S7"
    assert aa.report_lines([v]) == []
    assert not [c for c in ran_in if str(c).lower().startswith(str(checkout).lower())]


def test_review_a3_an_unreadable_nested_git_pointer_counts_as_excluded(tmp_path: Path) -> None:
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    session = make_session(world, age_days=20)
    odd = session / "scratchpad" / "odd"
    odd.mkdir()
    (odd / ".git").write_text("not a pointer\n", encoding="utf-8")
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    assert not v.offered and v.excluded


def test_review_a3_a_local_remote_writing_into_origins_namespace_is_no_proof(
    tmp_path: Path,
) -> None:
    """F6a: a push to a local `bak` whose fetch refspec maps to refs/remotes/origin/* updates
    origin's tracking ref, so the commit looked pushed to origin."""
    repo, wt = _wt(tmp_path)
    bare = _bare(tmp_path)
    git(repo, "remote", "add", "bak", str(bare))
    git(repo, "config", "--replace-all", "remote.bak.fetch", "+refs/heads/*:refs/remotes/origin/*")
    commit_file(wt, "work.txt", "only here\n", "unpushed")
    git(wt, "push", "-q", "bak", "feat")
    assert (
        git(wt, "rev-parse", "refs/remotes/origin/feat").stdout
        == git(wt, "rev-parse", "HEAD").stdout
    )  # the trap: it looks pushed to origin
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a3_a_remote_named_under_origin_taints_origin(tmp_path: Path) -> None:
    """F6b: `origin/bak` (config accepts the name, `git remote add` does not) has its refs under
    refs/remotes/origin/bak/*, which `--remotes=origin` reads."""
    repo, wt = _wt(tmp_path)
    bare = _bare(tmp_path)
    git(repo, "config", "remote.origin/bak.url", str(bare))
    git(repo, "config", "remote.origin/bak.fetch", "+refs/heads/*:refs/remotes/origin/bak/*")
    commit_file(wt, "work.txt", "only here\n", "unpushed")
    git(wt, "update-ref", "refs/remotes/origin/bak/feat", "HEAD")  # what a push there records
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a3_a_second_real_remote_does_not_taint_origin(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(repo, "remote", "add", "upstream", "https://example.invalid/upstream.git")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)


@pytest.mark.parametrize(
    "url",
    [
        "ssh://127.0.0.2/o/r.git",
        "ssh://127.1/o/r.git",
        "ssh://2130706433/o/r.git",
        "ssh://0x7f000001/o/r.git",
        "ssh://localhost./o/r.git",
        "ssh://[0:0:0:0:0:0:0:1]/o/r.git",
        "ssh://[::ffff:127.0.0.1]/o/r.git",
        "git@127.0.0.2:o/r.git",
        "git@localhost.:o/r.git",
        "ssh://host//D:/backup/r.git",
        "ssh://host/%44:/backup/r.git",
        "ssh://host/d|/backup/r.git",
    ],
)
def test_review_a3_more_spellings_of_this_machine_or_a_drive_are_not_a_backup(url: str) -> None:
    assert aa.is_real_remote_url(url) is False


def test_review_a3_this_computers_own_name_is_not_a_backup() -> None:
    import socket

    assert aa.is_real_remote_url(f"https://{socket.gethostname()}/o/r.git") is False


# --- survivors of the A3b mutation run: each check pinned on its own ------------------------------


def test_review_a3b_a_session_directory_that_is_itself_a_repository_is_proven(
    tmp_path: Path,
) -> None:
    """M23: in scratch, a `.git` directly in the session dir is a nested repo, not the artifact's
    own; an unpushed commit there must keep the session."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    git(session, "init", "-q", "-b", "main")
    commit_file(session, "scratchpad/notes.txt", "scratch", "only copy")
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert f"nested repository {session}" in v.failed_detail


def test_review_a3b_git_lists_an_untracked_file_deeper_than_max_path(tmp_path: Path) -> None:
    """N3: with core.longpaths git SEES the deep file (an untracked entry), instead of only
    warning that it could not open the directory."""
    repo, wt = _wt(tmp_path)
    deep = Path("\\\\?\\" + str(wt)) / ("a" * 120) / ("b" * 120) / ("c" * 40)
    deep.mkdir(parents=True)
    (deep / "results.csv").write_text("only copy", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W2")
    assert "uncommitted or untracked changes" in v.failed_detail, v.failed_detail


@pytest.mark.parametrize("name", ["data.", "notes "])
def test_review_a3b_the_walk_records_names_git_cannot_open(tmp_path: Path, name: str) -> None:
    """N5c."""
    odd = Path("\\\\?\\" + str(tmp_path / "t" / name))
    odd.mkdir(parents=True)
    facts = aa.walk_facts(tmp_path / "t")
    assert [p.name for p in facts.odd_names] == [name]


def test_review_a3b_a_name_git_cannot_open_refuses_the_tree_on_its_own(tmp_path: Path) -> None:
    """N5: the tree check refuses by itself, whatever git printed."""
    tree = tmp_path / "t"
    Path("\\\\?\\" + str(tree / "data.")).mkdir(parents=True)
    with pytest.raises(aa._Refusal) as refused:
        aa._prove_tree(make_world(tmp_path).env, tree, [])
    assert refused.value.rule == "W8" and "dot or space" in refused.value.detail


def test_review_a3b_a_name_git_cannot_open_inside_a_nested_repo_keeps_the_session(
    tmp_path: Path,
) -> None:
    """N5b: refused before any git command runs in the nested repository."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    nested = _nested_repo(session / "scratchpad" / "dep", pushed=True)
    Path("\\\\?\\" + str(nested / "data.")).mkdir()
    (Path("\\\\?\\" + str(nested / "data.")) / "results.csv").write_text("x", encoding="utf-8")
    _age_tree(Path("\\\\?\\" + str(session)), world, 20)  # the plain form cannot reach `data.`
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert "git cannot see" in v.failed_detail


def test_review_a3b_a_pointer_whose_common_dir_is_an_excluded_repo_is_excluded(
    tmp_path: Path,
) -> None:
    """N8b: the admin dir sits under a neutral name, and only its `commondir` names the excluded
    repository. No git may run at all."""
    excluded_git = tmp_path / "elsewhere" / "zzexclproj" / ".git"
    excluded_git.mkdir(parents=True)
    admin = tmp_path / "neutral-admin"
    admin.mkdir()
    (admin / "commondir").write_text(str(excluded_git) + "\n", encoding="utf-8")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    session = make_session(world, age_days=20)
    checkout = session / "scratchpad" / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered and v.failed_rule == "S7"


# === regression tests for the third independent review (A3b): each reproduces a reported hole ===


@pytest.mark.parametrize("legacy", ["remotes", "branches"])
def test_review_a3b_a_legacy_remote_file_is_refused(tmp_path: Path, legacy: str) -> None:
    """N1: `.git/remotes/bak` is invisible to `git remote` and to `remote.*.fetch`, yet its
    `Pull:` line can write refs/remotes/origin/*."""
    clone = _clone(tmp_path)
    folder = clone / ".git" / legacy
    folder.mkdir()
    (folder / "bak").write_text(
        "URL: D:/usbstick/bak.git\nPull: +refs/heads/*:refs/remotes/origin/*\n", encoding="utf-8"
    )
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    assert not v.offered, f"WRONG OFFER: {v.held}"
    assert v.failed_rule == "W3" and "legacy remote definition" in v.failed_detail


def test_review_a3b_a_legacy_remote_file_of_the_main_repo_keeps_a_worktree(
    tmp_path: Path,
) -> None:
    repo, wt = _wt(tmp_path)
    (repo / ".git" / "remotes").mkdir()
    (repo / ".git" / "remotes" / "bak").write_text("URL: D:/x.git\n", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W3")
    assert "legacy remote definition" in v.failed_detail


def test_review_a3b_a_bare_repository_inside_a_git_dir_is_found(tmp_path: Path) -> None:
    """N2: the bare-repo check skipped everything below any `.git`."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    nested = _nested_repo(session / "scratchpad" / "dep", pushed=True)
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(nested / ".git" / "backup.git"))
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert "bare repository" in v.failed_detail and "backup.git" in v.failed_detail


def test_review_a3b_a_bare_repository_in_a_clones_own_git_dir_keeps_the_clone(
    tmp_path: Path,
) -> None:
    clone = _clone(tmp_path)
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(clone / ".git" / "stash.git"))
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    _refused(v, "C8")
    assert "bare repository" in v.failed_detail


def test_review_a3b_a_git_dir_whose_commondir_is_an_excluded_repo_is_never_touched(
    tmp_path: Path,
) -> None:
    """N4/F5e: a nested `.git` DIRECTORY with a `commondir` into the excluded repository."""
    excluded = make_repo(tmp_path / "elsewhere", "zzexclproj")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    session = make_session(world, age_days=20)
    fake = session / "scratchpad" / "checkout" / ".git"
    fake.mkdir(parents=True)
    (fake / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (fake / "commondir").write_text(str(excluded / ".git") + "\n", encoding="utf-8")
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered and v.failed_rule == "S7"


def test_review_a3b_a_shared_clone_of_an_excluded_repo_is_never_touched(tmp_path: Path) -> None:
    """N4/F5g: `clone --shared` reads the excluded object store through objects/info/alternates."""
    excluded = make_repo(tmp_path / "elsewhere", "zzexclproj")
    clone = tmp_path / "proj-verify1"
    git(tmp_path, "clone", "-q", "--shared", str(excluded), str(clone))
    git(clone, "remote", "set-url", "origin", "https://example.invalid/neutral.git")
    mark_pushed(clone, "main")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    v = aa.evaluate_clone(world.env, clone)
    assert v.excluded and not v.offered and v.failed_rule == "C7"


def test_review_a3b_a_worktree_whose_admin_commondir_is_excluded_is_never_touched(
    tmp_path: Path,
) -> None:
    """N4/F5h: the worktree is listed by a neutral repository, but its admin dir's commondir
    names the excluded one."""
    excluded = make_repo(tmp_path / "elsewhere", "zzexclproj")
    repo, wt = _wt(tmp_path)
    admin = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    (admin / "commondir").write_text(str(excluded / ".git") + "\n", encoding="utf-8")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    v = aa.evaluate_worktree(world.env, wt, repo)
    assert v.excluded and not v.offered and v.failed_rule == "W7"


def test_review_a3b_a_core_worktree_naming_an_excluded_repo_is_hidden(tmp_path: Path) -> None:
    """N4/F5f: refused before, but the excluded path showed in the visible refusal detail."""
    excluded = make_repo(tmp_path / "elsewhere", "zzexclproj")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    session = make_session(world, age_days=20)
    nested = _nested_repo(session / "scratchpad" / "dep", pushed=True)
    git(nested, "config", "core.worktree", str(excluded))
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered
    assert aa.report_lines([v]) == []


def test_review_a3b_an_alternates_chain_into_an_excluded_repo_is_followed(tmp_path: Path) -> None:
    excluded = make_repo(tmp_path / "elsewhere", "zzexclproj")
    middle = tmp_path / "middle.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(middle))
    (middle / "objects" / "info" / "alternates").write_text(
        str(excluded / ".git" / "objects") + "\n", encoding="utf-8"
    )
    clone = _clone(tmp_path)
    (clone / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (clone / ".git" / "objects" / "info" / "alternates").write_text(
        str(middle / "objects") + "\n", encoding="utf-8"
    )
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    v = aa.evaluate_clone(world.env, clone)
    assert v.excluded and not v.offered


# --- survivors of the bad3e67 mutation run: each check pinned on its own -------------------------


def test_review_a3b_a_glob_destination_covering_every_remote_taints_origin(tmp_path: Path) -> None:
    """N9b: `refs/remotes/*` is wider than origin's namespace, so a fetch from `bak` of its
    branch `origin/feat` writes refs/remotes/origin/feat. Only the reverse prefix test sees it."""
    repo, wt = _wt(tmp_path)
    bare = _bare(tmp_path)
    git(repo, "remote", "add", "bak", str(bare))
    git(repo, "config", "--replace-all", "remote.bak.fetch", "+refs/heads/*:refs/remotes/*")
    commit_file(wt, "work.txt", "only here\n", "unpushed")
    git(wt, "update-ref", "refs/remotes/origin/feat", "HEAD")  # what that fetch records
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a3b_a_remote_named_under_origin_taints_origin_without_a_refspec(
    tmp_path: Path,
) -> None:
    """N10: `origin/bak` with no fetch refspec of its own is no writer, yet a fetch named on the
    command line (`git fetch origin/bak feat:refs/remotes/origin/bak/feat`) lands inside
    origin's namespace. The name alone must taint origin."""
    repo, wt = _wt(tmp_path)
    bare = _bare(tmp_path)
    git(repo, "config", "remote.origin/bak.url", str(bare))
    commit_file(wt, "work.txt", "only here\n", "unpushed")
    git(wt, "update-ref", "refs/remotes/origin/bak/feat", "HEAD")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a3b_a_remote_whose_name_holds_a_slash_is_never_a_proof(tmp_path: Path) -> None:
    """N10b: a remote named `up/stream` shares refs/remotes/up/* with any remote `up`; its
    namespace proves nothing, even with a real URL and nothing else configured."""
    repo, wt = _wt(tmp_path)
    git(repo, "remote", "remove", "origin")
    git(repo, "config", "remote.up/stream.url", "https://example.invalid/r.git")
    git(repo, "config", "remote.up/stream.fetch", "+refs/heads/*:refs/remotes/up/stream/*")
    commit_file(wt, "work.txt", "only here\n", "unpushed")
    git(wt, "update-ref", "refs/remotes/up/stream/feat", "HEAD")
    _refused(_verdict(make_world(tmp_path), wt, repo), "W4")


def test_review_a3b_the_host_name_alone_marks_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """N14: with COMPUTERNAME unset, `socket.gethostname()` is the only local name left."""
    import socket

    monkeypatch.delenv("COMPUTERNAME", raising=False)
    assert aa.is_real_remote_url(f"https://{socket.gethostname()}/o/r.git") is False


def test_review_a3b_computername_alone_marks_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aa.socket, "gethostname", lambda: "some-other-box")
    monkeypatch.setenv("COMPUTERNAME", "BUILDBOX7")
    assert aa.is_real_remote_url("https://buildbox7/o/r.git") is False
    assert aa.is_real_remote_url("https://buildbox8/o/r.git") is True


# --- the fourth (data-safety) review, A4 ----------------------------------------------------------


def _external_worktree(repo: Path, where: Path) -> Path:
    """A linked worktree of `repo` OUTSIDE the artifact, holding staged-only work."""
    git(repo, "worktree", "add", "-q", "--detach", str(where))
    (where / "staged.txt").write_text("only copy of staged work", encoding="utf-8")
    git(where, "add", "staged.txt")
    (where / "staged.txt").write_text("working copy differs", encoding="utf-8")
    return where


def test_review_a4_scratch_backing_a_worktree_in_an_excluded_project_is_excluded(
    tmp_path: Path,
) -> None:
    """F1: the scratch clone's `.git/worktrees/wt` holds the index and HEAD of a worktree in
    `zzexclproj`; vaulting the scratch broke that worktree (`fatal: not a git repository`)."""
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    session = make_session(world, age_days=20)
    clone = _clone_into(session)
    ext = _external_worktree(clone, tmp_path / "zzexclproj" / "wt")
    _age_tree(session, world, 20)
    ran_in: list[Path] = []

    def recording(args: object, cwd: Path) -> aa.GitResult:
        ran_in.append(Path(cwd))
        return aa.run_git(args, cwd)  # type: ignore[arg-type]

    world.env.run_git = recording
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered
    assert aa.report_lines([v]) == []
    assert ran_in == []  # decided from paths alone, before any git
    assert git(ext, "status", "--porcelain").returncode == 0


def test_review_a4_scratch_backing_any_outside_worktree_is_refused(tmp_path: Path) -> None:
    """F1/P1b: the same with a worktree in an ordinary folder: refused, not hidden."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    clone = _clone_into(session)
    _external_worktree(clone, tmp_path / "otherwt")
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert "linked worktrees" in v.failed_detail and not v.excluded


def test_review_a4_a_prunable_outside_worktree_still_refuses(tmp_path: Path) -> None:
    """The admin entry alone holds the staged index; git lists it as prunable, the filesystem
    check still sees it."""
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    clone = _clone_into(session)
    ext = _external_worktree(clone, tmp_path / "offline" / "wt")
    shutil.rmtree(ext.parent)  # e.g. an unplugged drive
    _age_tree(session, world, 20)
    _refused(aa.evaluate_scratch(world.env, session), "S7")


def test_review_a4_a_clone_whose_nested_repo_has_an_outside_worktree_is_refused(
    tmp_path: Path,
) -> None:
    """F1/P5: rule C8 proved the nested repository clean and pushed, but not worktree-free."""
    clone = _clone(tmp_path, "proj-verify3")
    (clone / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    git(clone, "add", ".gitignore")
    git(clone, "commit", "-q", "-m", "ignore")
    mark_pushed(clone, "main")
    nested = make_repo(clone / ".venv" / "src", "pkg")
    _external_worktree(nested, tmp_path / "pkg-wt")
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    _refused(v, "C8")
    assert "linked worktrees" in v.failed_detail


def test_review_a4_a_clone_backing_a_worktree_in_an_excluded_project_is_excluded(
    tmp_path: Path,
) -> None:
    clone = _clone(tmp_path)
    _external_worktree(clone, tmp_path / "zzexclproj" / "wt")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    v = aa.evaluate_clone(world.env, clone)
    assert v.excluded and not v.offered


def _lfs_object(gitdir: Path) -> Path:
    oid = "a" * 64
    obj = gitdir / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"the only copy of a large model file")
    return obj


def test_review_a4_a_clone_holding_lfs_objects_is_refused(tmp_path: Path) -> None:
    """F2: C4 proves the commits (and so the pointer files) are pushed, not the LFS content."""
    clone = _clone(tmp_path)
    _lfs_object(clone / ".git")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_review_a4_an_empty_lfs_store_does_not_refuse(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    (clone / ".git" / "lfs" / "objects" / "aa").mkdir(parents=True)  # e.g. after `lfs prune`
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    assert v.offered, (v.failed_rule, v.failed_detail)


def test_review_a4_a_nested_repo_holding_lfs_objects_keeps_the_session(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = make_session(world, age_days=20)
    clone = _clone_into(session)
    _lfs_object(clone / ".git")
    _age_tree(session, world, 20)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S7")
    assert "LFS" in v.failed_detail


def test_review_a4_an_unreadable_worktrees_entry_counts_as_worktrees(tmp_path: Path) -> None:
    """Fail closed: `.git/worktrees` that cannot be listed (here a file) may hide a worktree.
    (Through the evaluators the exclusion check, which fails closed on the same error, hides the
    artifact first; this pins the helper on its own.)"""
    gitdir = tmp_path / "repo" / ".git"
    gitdir.mkdir(parents=True)
    assert aa._has_linked_worktrees(gitdir) is False
    (gitdir / "worktrees").mkdir()
    assert aa._has_linked_worktrees(gitdir) is False  # empty: git removed the last one
    (gitdir / "worktrees").rmdir()
    (gitdir / "worktrees").write_text("not a directory", encoding="utf-8")
    assert aa._has_linked_worktrees(gitdir) is True


def test_review_a4_an_unreadable_worktrees_entry_counts_as_excluded(tmp_path: Path) -> None:
    """Fail closed in the exclusion check too: a worktree list that cannot be read may name a
    worktree inside an excluded project, so the artifact is hidden, not merely refused."""
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    gitdir = tmp_path / "repo" / ".git"
    gitdir.mkdir(parents=True)
    assert aa._gitdir_excluded(world.env, gitdir) is False
    (gitdir / "worktrees").write_text("not a directory", encoding="utf-8")
    assert aa._gitdir_excluded(world.env, gitdir) is True


# --- the fifth (data-safety) review, A5 -----------------------------------------------------------


def _clone_with_local_history(tmp_path: Path) -> tuple[Path, str]:
    """`proj-verify1`: main is pushed; branch `old` is an orphan commit that exists ONLY here."""
    clone = _clone(tmp_path)
    git(clone, "checkout", "-q", "--orphan", "old")
    commit_file(clone, "ancient.txt", "only copy of this history\n", "imported history")
    only = git(clone, "rev-parse", "HEAD").stdout.strip()
    git(clone, "checkout", "-q", "-f", "main")
    return clone, only


@pytest.mark.parametrize("advice", [True, False])
def test_review_a5_a_graft_file_does_not_hide_an_only_copy_commit(
    tmp_path: Path, advice: bool
) -> None:
    """F1: `info/grafts` "A T" made T look reachable from origin/main; with the deprecation
    advice silenced nothing on stderr refused it either, and C4 offered the only copy."""
    clone, only = _clone_with_local_history(tmp_path)
    main = git(clone, "rev-parse", "main").stdout.strip()
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "grafts").write_text(f"{main} {only}\n", encoding="ascii")
    if not advice:
        git(clone, "config", "advice.graftFileDeprecated", "false")
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    assert not v.offered
    assert v.failed_rule in ("C4", "W2"), (v.failed_rule, v.failed_detail)
    if not advice:
        assert v.failed_rule == "C4" and only[:12] in v.failed_detail


def test_review_a5_a_replace_ref_does_not_hide_an_only_copy_commit(tmp_path: Path) -> None:
    clone, only = _clone_with_local_history(tmp_path)
    git(clone, "replace", "--graft", "main", only)
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_review_a5_a_clone_holding_git_annex_objects_is_refused(tmp_path: Path) -> None:
    """F2: the same class as Git LFS: content outside the commits, not proven pushed."""
    clone = _clone(tmp_path)
    obj = clone / ".git" / "annex" / "objects" / "ab" / "cd" / "SHA256E-s5--x" / "SHA256E-s5--x"
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"only")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_review_a5_the_proof_never_runs_the_configured_gpg_program(tmp_path: Path) -> None:
    """F3(ii): `log.showSignature=true` made `log -1` in the proof run `gpg.program`."""
    marker = tmp_path / "outside" / "gpg-marker.txt"
    marker.parent.mkdir()
    clone = _clone(tmp_path)
    tree = git(clone, "rev-parse", "HEAD^{tree}").stdout.strip()
    parent = git(clone, "rev-parse", "HEAD").stdout.strip()
    body = (
        f"tree {tree}\nparent {parent}\nauthor t <t@x> 1700000000 +0000\n"
        "committer t <t@x> 1700000000 +0000\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n"
        " AAAA\n -----END PGP SIGNATURE-----\n\nsigned\n"
    )
    raw = tmp_path / "commit.txt"
    raw.write_bytes(body.encode())
    sha = git(clone, "hash-object", "-t", "commit", "-w", str(raw)).stdout.strip()
    git(clone, "update-ref", "refs/heads/main", sha)
    git(clone, "update-ref", "refs/remotes/origin/main", sha)
    script = tmp_path / "fakegpg.sh"
    script.write_text(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\nexit 1\n', encoding="utf-8")
    git(clone, "config", "log.showSignature", "true")
    git(clone, "config", "gpg.program", script.as_posix())
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    assert v.offered, (v.failed_rule, v.failed_detail)
    assert not marker.exists()


def test_review_a5_a_clone_whose_remote_names_an_excluded_project_is_hidden(
    tmp_path: Path,
) -> None:
    """F5: `_verify-pr12` cloned from `.../zzexclproj.git`: the folder does not name it."""
    clone = make_repo(tmp_path, "_verify-pr12")
    git(clone, "remote", "set-url", "origin", "https://github.com/u/zzexclproj.git")
    world = make_world(tmp_path, exclusions=("zzexclproj",), run=_no_git)
    v = aa.evaluate_clone(world.env, clone)
    assert v.excluded and not v.offered
    assert aa.report_lines([v]) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://h/u/zzexclproj.git",
        "git@h:u/zzexclproj",
        "ssh://h/zzexclproj.git/",
        "D:\\r\\zzexclproj",
    ],
)
def test_review_a5_the_repository_name_of_a_remote_url(url: str) -> None:
    assert aa._url_repo_name(url) == "zzexclproj"


def test_review_a5_an_excluded_name_in_the_status_listing_is_hidden(tmp_path: Path) -> None:
    """F4: W2 refused first and printed `?? zzexclproj/secret_notes.txt` in the report."""
    repo, wt = _wt(tmp_path)
    (wt / "zzexclproj").mkdir()
    (wt / "zzexclproj" / "secret_notes.txt").write_text("x", encoding="utf-8")
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    v = _verdict(world, wt, repo)
    assert v.excluded and not v.offered
    assert aa.report_lines([v]) == []


def test_review_a5_an_ordinary_untracked_file_is_still_reported(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    (wt / "notes.txt").write_text("x", encoding="utf-8")
    v = _verdict(make_world(tmp_path, exclusions=("zzexclproj",)), wt, repo)
    _refused(v, "W2")
    assert not v.excluded and "notes.txt" in v.failed_detail


def test_review_a5_an_inherited_git_dir_does_not_redirect_the_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_DIR from a hook or a shell pointed every proof at another, fully pushed repository."""
    clone = _clone(tmp_path)
    commit_file(clone, "work.txt", "only here\n", "unpushed")
    other = make_repo(tmp_path / "elsewhere", "pushed")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "C4")


def test_review_a5_git_refuses_to_run_when_the_no_graft_dir_exists(tmp_path: Path) -> None:
    """The graft file named to git must be missing; if its directory exists, fail closed."""
    import tempfile

    (Path(tempfile.gettempdir()) / "reclaim-no-graft-file-0f4c2b7e").mkdir()
    result = aa.run_git(("--version",), tmp_path)
    assert result.rc == -1 and "must not" in result.err


def test_review_a5_a_replace_ref_onto_a_pushed_commit_does_not_hide_the_only_copy(
    tmp_path: Path,
) -> None:
    """`git replace T A`: the replace ref points at the PUSHED commit A, so no unpushed tip is
    left to refuse, and with replacements honoured the local-only T reads as A."""
    clone, only = _clone_with_local_history(tmp_path)
    git(clone, "replace", only, "main")
    v = aa.evaluate_clone(make_world(tmp_path).env, clone)
    _refused(v, "C4")
    assert only[:12] in v.failed_detail


# --- the sixth (data-safety) review, A6 -----------------------------------------------------------


def _stripping_repo(tmp_path: Path, attrs_in_info: bool = True) -> Path:
    """`nbstripout --install`-style: a clean filter that drops everything after line 1."""
    repo = make_repo(tmp_path, "proj")
    git(repo, "config", "filter.strip.clean", "sed -n 1p")
    git(repo, "config", "filter.strip.smudge", "cat")
    git(repo, "config", "filter.strip.required", "true")
    if attrs_in_info:
        attrs = repo / ".git" / "info" / "attributes"
        attrs.parent.mkdir(parents=True, exist_ok=True)
        attrs.write_text("*.ipynb filter=strip\n", encoding="utf-8")
    commit_file(repo, "analysis.ipynb", "{cells}\n", "notebook")
    mark_pushed(repo, "main")
    return repo


def test_review_a6_a_clean_filter_that_drops_content_keeps_the_worktree(tmp_path: Path) -> None:
    """F1: run the notebook, commit, push. The commit holds the stripped notebook, `status` is
    empty, and the outputs exist only in the working file."""
    repo = _stripping_repo(tmp_path)
    wt = add_worktree(repo, "proj-wt-nb", "nb")
    notebook = wt / "analysis.ipynb"
    notebook.write_text("{cells v2}\nOUTPUT: hours of GPU results\n", encoding="utf-8")
    git(wt, "add", "analysis.ipynb")
    git(wt, "commit", "-q", "-m", "v2")
    mark_pushed(wt, "nb")
    assert git(wt, "status", "--porcelain", "-uall").stdout == ""  # the trap
    assert "OUTPUT" not in git(wt, "cat-file", "-p", "HEAD:analysis.ipynb").stdout
    v = _verdict(make_world(tmp_path), wt, repo)
    _refused(v, "W2")
    assert "clean filter 'strip'" in v.failed_detail


def test_review_a6_a_clean_filter_in_a_clone_keeps_the_clone(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    git(clone, "config", "filter.strip.clean", "sed -n 1p")
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text("*.md filter=strip\n", encoding="utf-8")
    _refused(aa.evaluate_clone(make_world(tmp_path).env, clone), "W2")


def test_review_a6_a_filter_driver_no_tracked_file_uses_does_not_refuse(tmp_path: Path) -> None:
    """A globally installed filter (e.g. `nbstripout --global`) in a repo with no notebooks."""
    repo, wt = _wt(tmp_path)
    git(repo, "config", "filter.strip.clean", "sed -n 1p")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.ipynb filter=strip\n", encoding="utf-8")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)


def test_review_a6_git_lfs_as_the_filter_does_not_refuse(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")
    git(repo, "config", "filter.lfs.process", "git-lfs filter-process")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.md filter=lfs\n", encoding="utf-8")
    # Called directly: git-lfs is not installed here, so `status` itself would show the file
    # modified once it is marked `filter=lfs` (a real LFS checkout holds the pointer).
    aa._prove_no_hiding_filter(make_world(tmp_path).env, wt)  # does not raise


def test_review_a6_a_filter_named_lfs_that_is_not_git_lfs_refuses(tmp_path: Path) -> None:
    """The exemption follows the program, not the driver's name."""
    repo, wt = _wt(tmp_path)
    git(repo, "config", "filter.lfs.clean", "sed -n 1p")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.md filter=lfs\n", encoding="utf-8")
    # Called directly: through the evaluator `status` may already see the file modified.
    with pytest.raises(aa._Refusal, match="clean filter 'lfs'"):
        aa._prove_no_hiding_filter(make_world(tmp_path).env, wt)


def test_review_a6_a_skip_worktree_path_naming_an_excluded_project_is_hidden(
    tmp_path: Path,
) -> None:
    """F2: a skip-worktree flag (sparse checkout sets one on every path outside the cone) on a
    path naming an excluded project: the W2 detail printed it. The file is not on disk, so only
    the path itself can be matched."""
    repo, wt = _wt(tmp_path)
    commit_file(wt, "content/zzexclproj/notes.md", "x\n", "notes")
    push_branch(wt, "feat")
    git(wt, "update-index", "--skip-worktree", "content/zzexclproj/notes.md")
    shutil.rmtree(wt / "content")  # as sparse checkout leaves it
    v = _verdict(make_world(tmp_path, exclusions=("zzexclproj",)), wt, repo)
    assert v.excluded and not v.offered
    assert aa.report_lines([v]) == []


def test_review_a6_an_ordinary_skip_worktree_flag_is_still_reported(tmp_path: Path) -> None:
    repo, wt = _wt(tmp_path)
    git(wt, "update-index", "--skip-worktree", "README.md")
    v = _verdict(make_world(tmp_path, exclusions=("zzexclproj",)), wt, repo)
    _refused(v, "W2")
    assert not v.excluded and "hidden change flag" in v.failed_detail


def test_review_a6_a_scratch_junction_inside_an_excluded_copy_is_hidden(tmp_path: Path) -> None:
    """F3: S8 refused first and printed `...\\zzexclproj\\node_modules\\pkg` in the report."""
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    session = make_session(world, age_days=30)
    target = tmp_path / "store" / "pkg"
    target.mkdir(parents=True)
    modules = session / "zzexclproj" / "node_modules"
    modules.mkdir(parents=True)
    make_junction(modules / "pkg", target)
    _age_tree(session, world, 30)
    v = aa.evaluate_scratch(world.env, session)
    assert v.excluded and not v.offered
    assert aa.report_lines([v]) == []


def test_review_a6_a_scratch_junction_elsewhere_is_still_reported(tmp_path: Path) -> None:
    world = make_world(tmp_path, exclusions=("zzexclproj",))
    session = make_session(world, age_days=30)
    target = tmp_path / "store" / "pkg"
    target.mkdir(parents=True)
    make_junction(session / "pkg", target)
    _age_tree(session, world, 30)
    v = aa.evaluate_scratch(world.env, session)
    _refused(v, "S8")
    assert not v.excluded


def test_review_a6_status_show_untracked_files_no_does_not_refuse_everything(
    tmp_path: Path,
) -> None:
    """O1: `--ignored=matching` with a configured `status.showUntrackedFiles=no` is a fatal
    error, so every worktree and clone was refused at W8."""
    repo, wt = _wt(tmp_path)
    git(repo, "config", "status.showUntrackedFiles", "no")
    v = _verdict(make_world(tmp_path), wt, repo)
    assert v.offered, (v.failed_rule, v.failed_detail)


def test_review_a6_the_filter_check_reads_every_chunk_of_tracked_files(tmp_path: Path) -> None:
    """`check-attr` runs on 100 paths at a time: a filtered file past the first chunk counts."""
    repo, wt = _wt(tmp_path)
    for i in range(150):
        (wt / f"f{i:03d}.txt").write_text("x\n", encoding="utf-8")
    (wt / "zz_last.ipynb").write_text("{cells}\n", encoding="utf-8")
    git(wt, "add", ".")
    git(wt, "commit", "-q", "-m", "many files")
    push_branch(wt, "feat")
    git(repo, "config", "filter.strip.clean", "sed -n 1p")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.ipynb filter=strip\n", encoding="utf-8")
    with pytest.raises(aa._Refusal, match=r"zz_last.ipynb"):
        aa._prove_no_hiding_filter(make_world(tmp_path).env, wt)


def test_review_a6_an_exclusion_walk_that_fails_hides_the_refusal(tmp_path: Path) -> None:
    """Fail closed: if the subtree exclusion walk cannot run, the refusal detail may name an
    excluded project, so it is hidden."""
    repo, wt = _wt(tmp_path)
    (wt / "notes.txt").write_text("x", encoding="utf-8")
    world = make_world(tmp_path)

    def failing(path: Path, is_dir: bool) -> str | None:
        raise OSError("walk failed")

    world.env.exclusion_match = failing
    v = _verdict(world, wt, repo)
    assert v.excluded and not v.offered
