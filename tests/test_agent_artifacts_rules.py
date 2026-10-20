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
    git(session, "init", "-q", "--bare", "-b", "main", str(bare))
    scratch_repo = session / "scratchpad" / "writer"
    scratch_repo.mkdir()
    git(scratch_repo, "init", "-q", "-b", "main")
    commit_file(scratch_repo, "a", "a", "a")
    git(scratch_repo, "push", "-q", str(bare), "main")
    shutil.rmtree(
        scratch_repo, onexc=lambda func, path, _exc: (Path(path).chmod(0o700), func(path))
    )  # now the bare repo holds the only copy
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
