"""HARD GATE: agent-artifacts cleanup offers NOTHING that is not provably finished.

A population of real git repositories / session directories with a known-correct verdict each
(docs/specs/agent-artifacts.md section 8). The gate is 0 wrong offers; recall on the genuinely
finished ones is reported and must also be 100% on this population (a refusal for a finished item
is a regression too, just a harmless one). The first failed rule is checked per case so a refusal
for the wrong reason cannot mask a missing check.

Registered in scripts/verify.py's `_SAFETY_GATE_FILES`."""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

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


@dataclass
class Case:
    name: str
    kind: str  # worktree | clone | scratch
    offered: bool
    rule: str  # expected first failed rule ("" when offered)
    build: Callable[[Path], tuple[Path, Path | None, dict[str, object]]]
    # -> (artifact path, repo path for worktrees, make_world kwargs)


def _wt(tmp: Path, name: str = "proj") -> tuple[Path, Path]:
    repo = make_repo(tmp, name)
    return repo, add_worktree(repo, f"{name}-wt", "feat")


def _wt_case(mutate: Callable[[Path, Path, Path], dict[str, object] | None]):  # type: ignore[no-untyped-def]
    def build(tmp: Path) -> tuple[Path, Path | None, dict[str, object]]:
        repo, wt = _wt(tmp)
        extra = _as_dict(mutate(tmp, repo, wt))
        return wt, repo, extra

    return build


def _as_dict(value: object) -> dict[str, object]:
    """A mutator returns world kwargs (a dict) or something irrelevant (e.g. write_text's int)."""
    return value if isinstance(value, dict) else {}


def _gitdir(wt: Path) -> Path:
    return Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())


def _hidden(flag: str) -> Callable[[Path, Path, Path], None]:
    def mutate(_t: Path, _r: Path, wt: Path) -> None:
        git(wt, "update-index", flag, "README.md")
        (wt / "README.md").write_text("hidden", encoding="utf-8")

    return mutate


def _ignored(name: str) -> Callable[[Path, Path, Path], None]:
    def mutate(_t: Path, _r: Path, wt: Path) -> None:
        top = name.split("/", maxsplit=1)[0]
        commit_file(wt, ".gitignore", f"{top}\n", "ignore")
        push_branch(wt, "feat")
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text("x", encoding="utf-8")

    return mutate


def _regenerable(_t: Path, _r: Path, wt: Path) -> None:
    commit_file(wt, ".gitignore", "node_modules/\n", "ignore")
    push_branch(wt, "feat")
    (wt / "node_modules" / "p").mkdir(parents=True)
    (wt / "node_modules" / "p" / "i.js").write_text("x", encoding="utf-8")


def _junction(tmp: Path, _r: Path, wt: Path) -> None:
    commit_file(wt, ".gitignore", "node_modules/\n", "ignore")
    push_branch(wt, "feat")
    outside = tmp / "elsewhere"
    outside.mkdir()
    (wt / "node_modules").mkdir()
    make_junction(wt / "node_modules" / "l", outside)


def _submodule(tmp: Path, _r: Path, wt: Path) -> None:
    other = make_repo(tmp, "other")
    git(wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(other), "sub")
    git(wt, "commit", "-q", "-m", "submodule")
    push_branch(wt, "feat")


def _state_file(name: str) -> Callable[[Path, Path, Path], None]:
    def mutate(_t: Path, _r: Path, wt: Path) -> None:
        (_gitdir(wt) / name).write_text("0" * 40, encoding="utf-8")

    return mutate


def _detached_ahead(tmp: Path) -> tuple[Path, Path | None, dict[str, object]]:
    repo = make_repo(tmp, "proj")
    wt = add_worktree(repo, "proj-wt", None)
    commit_file(wt, "a.txt", "ahead", "ahead")
    return wt, repo, {}


def _clean_detached(tmp: Path) -> tuple[Path, Path | None, dict[str, object]]:
    repo = make_repo(tmp, "proj")
    return add_worktree(repo, "proj-wt", None), repo, {}


def _no_remote(_t: Path, repo: Path, _w: Path) -> None:
    git(repo, "remote", "remove", "origin")


def _local_remote(tmp: Path, repo: Path, _w: Path) -> None:
    git(repo, "remote", "set-url", "origin", (tmp / "proj-remote.git").as_posix())


def _stash(_t: Path, repo: Path, _w: Path) -> None:
    (repo / "README.md").write_text("stash me", encoding="utf-8")
    git(repo, "stash", "push", "-q")


def _lock(_t: Path, repo: Path, wt: Path) -> None:
    git(repo, "worktree", "lock", str(wt))


def _with_world(**kw: object) -> Callable[[Path, Path, Path], dict[str, object]]:
    return lambda _t, _r, _w: dict(kw)


def _live_cwd(_t: Path, _r: Path, wt: Path) -> dict[str, object]:
    return {"procs": fake_scan([ProcessSnapshot(1, "node.exe", str(wt), "node")])}


def _blocked(_t: Path, _r: Path, _w: Path) -> dict[str, object]:
    return {"procs": fake_scan(blocked=[(2, "x.exe", "denied")])}


def _clone_case(mutate: Callable[[Path], dict[str, object] | None]):  # type: ignore[no-untyped-def]
    def build(tmp: Path) -> tuple[Path, Path | None, dict[str, object]]:
        clone = make_repo(tmp, "proj-verify1")
        extra = _as_dict(mutate(clone))
        return clone, None, extra

    return build


def _clone_side_branch(clone: Path) -> None:
    git(clone, "checkout", "-q", "-b", "side")
    commit_file(clone, "s.txt", "side", "side")
    git(clone, "checkout", "-q", "main")


def _clone_stash(clone: Path) -> None:
    (clone / "README.md").write_text("stash", encoding="utf-8")
    git(clone, "stash", "push", "-q")


def _scratch_case(
    age: float, mutate: Callable[[World, Path], dict[str, object] | None] | None = None
):  # type: ignore[no-untyped-def]
    def build(tmp: Path) -> tuple[Path, Path | None, dict[str, object]]:
        world = make_world(tmp)
        session = make_session(world, age_days=age)
        extra = _as_dict(mutate(world, session) if mutate else None)
        return session, None, extra

    return build


def _s_transcript(world: World, session: Path) -> None:
    folder = world.projects / session.parent.name
    folder.mkdir(parents=True)
    (folder / f"{session.name}.jsonl").write_text("{}", encoding="utf-8")


def _s_nested_unpushed(world: World, session: Path) -> None:
    repo = session / "c"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_file(repo, "a", "a", "a")
    git(repo, "remote", "add", "origin", "https://example.invalid/c.git")
    mark_pushed(repo, "main")
    commit_file(repo, "b", "b", "unpushed")
    stamp = world.env.now - 30 * 86400
    for dirpath, dirnames, filenames in os.walk(session):
        for n in [*dirnames, *filenames]:
            os.utime(Path(dirpath) / n, (stamp, stamp))
        os.utime(dirpath, (stamp, stamp))


def _s_junction(world: World, session: Path) -> None:
    out = world.root / "keep"
    out.mkdir()
    make_junction(session / "scratchpad" / "l", out)


def _s_live(world: World, session: Path) -> dict[str, object]:
    return {
        "procs": fake_scan(
            [ProcessSnapshot(3, "claude.exe", "C:" + chr(92), f"--s {session.name}")]
        )
    }


def _s_excluded(world: World, session: Path) -> dict[str, object]:
    return {"exclusions": (session.parent.name.split("-")[-1],)}


def _s_non_uuid(world: World, _s: Path) -> None:
    return None


CASES: list[Case] = [
    # --- worktrees that ARE finished
    Case("wt clean pushed", "worktree", True, "", _wt_case(lambda *_: None)),
    Case("wt detached at pushed commit", "worktree", True, "", _clean_detached),
    Case("wt stash only in repo", "worktree", True, "", _wt_case(_stash)),
    Case("wt regenerable ignored dirs", "worktree", True, "", _wt_case(_regenerable)),
    # --- worktrees that are NOT
    Case(
        "wt dirty tracked",
        "worktree",
        False,
        "W2",
        _wt_case(lambda _t, _r, w: (w / "README.md").write_text("d", encoding="utf-8")),
    ),
    Case(
        "wt untracked",
        "worktree",
        False,
        "W2",
        _wt_case(lambda _t, _r, w: (w / "n.txt").write_text("n", encoding="utf-8")),
    ),
    Case("wt skip-worktree edit", "worktree", False, "W2", _wt_case(_hidden("--skip-worktree"))),
    Case(
        "wt assume-unchanged edit", "worktree", False, "W2", _wt_case(_hidden("--assume-unchanged"))
    ),
    Case(
        "wt unpushed commit",
        "worktree",
        False,
        "W4",
        _wt_case(lambda _t, _r, w: commit_file(w, "l", "l", "l")),
    ),
    Case("wt detached ahead", "worktree", False, "W4", _detached_ahead),
    Case("wt no remote", "worktree", False, "W4", _wt_case(_no_remote)),
    Case("wt remote is a local path", "worktree", False, "W4", _wt_case(_local_remote)),
    Case("wt merge in progress", "worktree", False, "W2", _wt_case(_state_file("MERGE_HEAD"))),
    Case("wt bisect in progress", "worktree", False, "W2", _wt_case(_state_file("BISECT_LOG"))),
    Case("wt locked", "worktree", False, "W1", _wt_case(_lock)),
    Case("wt submodule", "worktree", False, "W2", _wt_case(_submodule)),
    Case("wt ignored .env", "worktree", False, "W8", _wt_case(_ignored(".env"))),
    Case("wt ignored build/", "worktree", False, "W8", _wt_case(_ignored("build/out.bin"))),
    Case("wt ignored database", "worktree", False, "W8", _wt_case(_ignored("app.db"))),
    Case("wt junction in node_modules", "worktree", False, "W8", _wt_case(_junction)),
    Case("wt excluded repo", "worktree", False, "W7", _wt_case(_with_world(exclusions=("proj",)))),
    Case("wt live cwd", "worktree", False, "W5", _wt_case(_live_cwd)),
    Case("wt unreadable same-user process", "worktree", False, "W5", _wt_case(_blocked)),
    # --- clones
    Case("clone clean pushed", "clone", True, "", _clone_case(lambda c: None)),
    Case("clone other branch unpushed", "clone", False, "C4", _clone_case(_clone_side_branch)),
    Case("clone stash", "clone", False, "C2", _clone_case(_clone_stash)),
    Case(
        "clone dirty",
        "clone",
        False,
        "W2",
        _clone_case(lambda c: (c / "x").write_text("x", encoding="utf-8")),
    ),
    Case(
        "clone linked worktree",
        "clone",
        False,
        "C6",
        _clone_case(lambda c: add_worktree(c, "proj-verify1-wt", "w")),
    ),
    # --- scratch
    Case("scratch idle 7.1 days", "scratch", True, "", _scratch_case(7.1)),
    Case("scratch idle 6.9 days", "scratch", False, "S2", _scratch_case(6.9)),
    Case("scratch fresh transcript", "scratch", False, "S3", _scratch_case(20, _s_transcript)),
    Case(
        "scratch nested clone unpushed",
        "scratch",
        False,
        "S7",
        _scratch_case(30, _s_nested_unpushed),
    ),
    Case("scratch junction inside", "scratch", False, "S8", _scratch_case(20, _s_junction)),
    Case("scratch live process", "scratch", False, "S4", _scratch_case(20, _s_live)),
    Case("scratch excluded slug", "scratch", False, "S5", _scratch_case(20, _s_excluded)),
]


def _run(case: Case, tmp: Path) -> aa.ArtifactVerdict:
    tmp.mkdir(parents=True)
    path, repo, extra = case.build(tmp)
    kwargs = {k: v for k, v in extra.items() if k in {"procs", "exclusions"}}
    world = make_world(tmp, **kwargs)  # type: ignore[arg-type]
    if case.kind == "worktree":
        assert repo is not None
        return aa.evaluate_worktree(world.env, path, repo)
    if case.kind == "clone":
        return aa.evaluate_clone(world.env, path)
    return aa.evaluate_scratch(world.env, path)


def test_zero_wrong_offers_and_full_recall_on_the_known_population(tmp_path: Path) -> None:
    wrong_offers: list[str] = []
    missed: list[str] = []
    wrong_reason: list[str] = []
    rows: list[str] = []
    for i, case in enumerate(CASES):
        verdict = _run(case, tmp_path / f"case{i:02d}-{uuid.uuid4().hex[:6]}")
        rows.append(
            f"{case.name:40} expected={'OFFER' if case.offered else 'KEEP '} "
            f"got={'OFFER' if verdict.offered else 'KEEP '} {verdict.failed_rule}"
        )
        if verdict.offered and not case.offered:
            wrong_offers.append(case.name)
        elif not verdict.offered and case.offered:
            missed.append(f"{case.name}: {verdict.failed_rule} {verdict.failed_detail}")
        elif not case.offered and verdict.failed_rule != case.rule:
            wrong_reason.append(f"{case.name}: expected {case.rule}, got {verdict.failed_rule}")
    positives = sum(c.offered for c in CASES)
    negatives = len(CASES) - positives
    print(  # noqa: T201 -- eval numbers; run with `pytest -s` to see them
        f"\n[agent-artifacts gate] cases={len(CASES)} finished={positives} unfinished={negatives} "
        f"wrong_offers={len(wrong_offers)} missed={len(missed)} wrong_reason={len(wrong_reason)}\n"
        + "\n".join(rows)
    )
    assert wrong_offers == [], f"UNSAFE: offered something not provably finished: {wrong_offers}"
    assert missed == [], f"recall regression: {missed}"
    assert wrong_reason == [], f"refused for the wrong reason: {wrong_reason}"
    assert time.time() > 0
