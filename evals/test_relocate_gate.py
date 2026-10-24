from __future__ import annotations

# ruff: noqa: PTH103, PTH116, PTH123, S311, S603, S607, SIM115
# Trees here go past 260 characters, so files are opened through extended-length strings
# (`long_path`) that pathlib would normalise away; icacls sets the ACL case's deny entry.
# S311: seeded test data, not secrets. SIM115: the open handle is the point (held by an ExitStack).
import ctypes
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

import reclaim.relocate as relocate
from reclaim.config import Config, ExclusionsConfig
from reclaim.relocate import Policy, RelocateError, Stage
from reclaim.safety import SafetyValidator
from reclaim.scanner import long_path

# Spec `docs/specs/relocate.md` / run spec Phase B: "Zero data loss is a hard gate." Every case
# in `evals/fixtures/relocate/` builds a tree, runs a move (or a move and a failure, a kill, a
# rollback, a delete), and then checks one invariant before its expected outcome: every byte
# of every original file is still readable at its original path. A case that loses or alters a
# file fails the gate whatever its outcome was.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="junctions and NTFS attributes")

FIXTURES = Path(__file__).parent / "fixtures" / "relocate"
CHILD = Path(__file__).parent / "fixtures" / "relocate_child.py"
CASES = sorted(FIXTURES.glob("*.json"))
_ATTRS = {"readonly": 0x1, "hidden": 0x2, "system": 0x4}


class Crash(BaseException):
    """A kill: not an Exception, so the move's own rollback never runs."""


def _x(path: Path | str) -> str:
    return long_path(Path(path))


def _content(case: str, rel: str, size: int) -> bytes:
    rng = random.Random(f"{case}:{rel}")  # seed: deterministic per case and file
    return rng.randbytes(size)


def _manifest(root: Path | str) -> dict[str, str]:
    out: dict[str, str] = {}
    stack = [("", _x(root))]
    while stack:
        rel_dir, current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                rel = rel_dir + "\\" + entry.name if rel_dir else entry.name
                if entry.is_junction() or entry.is_symlink():
                    continue  # a link is not data of this tree
                if entry.is_dir(follow_symlinks=False):
                    stack.append((rel, entry.path))
                elif not entry.is_symlink():
                    with open(entry.path, "rb") as fh:
                        out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


class Layout:
    def __init__(self, tmp: Path, spec: dict[str, Any]) -> None:
        self.c, self.d = tmp / "C", tmp / "D"
        self.c.mkdir()
        self.d.mkdir()
        name = spec.get("source_is", "data")
        self.source = self.c / name
        self.target = self.d / "reclaim-relocated" / name
        if spec.get("long"):
            self.source = Path(self.c, *(["s" * 60] * 5), name)
            self.target = Path(self.d, *(["t" * 60] * 5), name)
        self.journals = tmp / "journals"
        validator = SafetyValidator(
            Config(exclusions=ExclusionsConfig(project_names=["zzexclproj"]))
        )
        self.policy = Policy(
            exclusion_match=lambda p: validator.exclusion_match(p, is_dir=True),
            protected=lambda p: False,
        )


def _two_volumes(monkeypatch: pytest.MonkeyPatch, layout: Layout) -> None:
    """The test's tmp dir is one disk; everything under the layout's `D` reads as another."""
    d_key = relocate._key(layout.d)
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(d_key) else 1
    )


def _deny_read(path: Path, stack: ExitStack) -> None:
    subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(RD)"], check=True, capture_output=True)
    stack.callback(
        subprocess.run,
        ["icacls", str(path), "/remove:d", "*S-1-1-0"],
        check=True,
        capture_output=True,
    )


def _build(layout: Layout, spec: dict[str, Any], stack: ExitStack) -> None:
    case = spec["case"]
    for item in spec["files"]:
        rel = item["path"].replace("LONG", "\\".join(["l" * 60] * 2))
        path = Path(_x(layout.source)) / rel.replace("/", "\\")
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(_content(case, item["path"], item["size"]))
        bits = sum(_ATTRS[a] for a in item.get("attrs", []))
        if bits:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), bits)
    first = Path(_x(layout.source)) / spec["files"][0]["path"].replace("/", "\\")
    for extra in spec.get("extras", []):
        if extra == "junction":
            relocate._create_junction(str(layout.source / "link"), str(layout.c))
        elif extra == "hardlink":
            os.link(first, str(first) + ".link")
        elif extra == "open":
            stack.enter_context(open(first, "rb"))
        # "acl_deny" is applied by the test after the before-manifest is read.


def _run(layout: Layout, spec: dict[str, Any]) -> tuple[str, str]:
    free = spec.get("free", 10**15)

    def move(**kwargs: Any) -> relocate.Outcome:
        return relocate.execute_move(
            layout.source,
            layout.target,
            layout.policy,
            layout.journals,
            free_bytes=lambda _p: free,
            **kwargs,
        )

    def journal() -> Path:
        (found,) = layout.journals.glob("relocate-*.json")
        return found

    action = spec["action"]
    try:
        if action.startswith("crash:"):
            point = action.split(":", 1)[1]

            def die(name: str) -> None:
                if name == point:
                    raise Crash(name)

            with pytest.raises(Crash):
                move(checkpoint=die)
            return relocate.recover(journal()).journal.stage, ""
        outcome = move()
        if action == "move":
            return outcome.journal.stage, ""
        if action == "move_then_rollback":
            return relocate.rollback(journal()).journal.stage, ""
        if action == "delete_then_rollback":
            relocate.delete_old_copy(journal(), confirmed=True, free_bytes=lambda _p: free)
            return relocate.rollback(journal()).journal.stage, ""
        if action == "corrupt_then_delete":
            victim = layout.target / spec["files"][0]["path"]
            st = os.stat(_x(victim))
            data = bytearray(victim.read_bytes())
            data[len(data) // 2] ^= 0x01
            victim.write_bytes(bytes(data))
            os.utime(_x(victim), ns=(st.st_atime_ns, st.st_mtime_ns))
            try:
                relocate.delete_old_copy(journal(), confirmed=True)
            except RelocateError as exc:
                # The corrupted copy is what the junction serves: restore the old copy so the
                # invariant below checks that nothing was lost, which is the point of refusing.
                relocate.rollback(journal())
                return "delete_refused", str(exc)
            return "deleted", ""
    except RelocateError as exc:
        if "rolled back" in str(exc):
            return "failed_rolled_back", str(exc)
        return "refused", str(exc)
    raise AssertionError(f"unknown action {action}")


@pytest.mark.parametrize("case_path", CASES, ids=[p.stem for p in CASES])
def test_relocate_never_loses_a_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_path: Path
) -> None:
    spec = json.loads(case_path.read_text(encoding="utf-8"))
    layout = Layout(tmp_path, spec)
    _two_volumes(monkeypatch, layout)
    with ExitStack() as stack:
        _build(layout, spec, stack)
        before = _manifest(layout.source)
        if "acl_deny" in spec.get("extras", []):
            _deny_read(layout.source / "locked.bin", stack)
        outcome, message = _run(layout, spec)
    # Zero data loss, whatever the outcome: every original file, byte for byte, at its path.
    after = _manifest(layout.source)
    lost = {rel for rel, digest in before.items() if after.get(rel) != digest}
    assert not lost, f"{spec['case']}: files lost or changed: {sorted(lost)}"
    assert outcome == spec["expect"], (spec["case"], outcome, message)
    if "reason" in spec:
        assert spec["reason"] in message, (spec["case"], message)
    if outcome in {Stage.ROLLED_BACK, "refused", "failed_rolled_back", "delete_refused"}:
        assert not relocate._is_reparse(str(layout.source)), "the original is still a junction"
    if outcome in {"refused", "failed_rolled_back"}:
        # Nothing swapped: no old copy is left behind. (A rollback after the swap keeps it
        # unchanged on purpose, ADR-0044 addendum 5.)
        assert not os.path.lexists(_x(str(layout.source) + relocate.MOVED_SUFFIX))
    if outcome == Stage.DONE:
        assert relocate._is_reparse(str(layout.source))
        assert _manifest(layout.target) == before


def test_a_real_kill_mid_copy_recovers_the_original(tmp_path: Path) -> None:
    """The process really dies (TerminateProcess), not a simulated exception."""
    layout = Layout(tmp_path, {"case": "kill"})
    for i in range(40):
        path = layout.source / f"f{i:02d}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_content("kill", path.name, 50_000))
    before = _manifest(layout.source)
    ready = tmp_path / "ready"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(relocate.__file__).parents[1]), *sys.path])
    child = subprocess.Popen(
        [
            sys.executable,
            str(CHILD),
            str(layout.source),
            str(layout.target),
            str(layout.journals),
            str(layout.d),
            str(ready),
        ],
        env=env,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert child.poll() is None, "the child exited before reaching the copy"
            assert time.monotonic() < deadline, "the child never reached the copy"
            time.sleep(0.05)
        child.kill()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    (journal,) = layout.journals.glob("relocate-*.json")
    assert relocate.Journal.load(journal).stage == Stage.COPYING
    assert os.path.lexists(_x(layout.target)), "the kill landed before the copy began"

    outcome = relocate.recover(journal)

    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(layout.source) == before
    assert not os.path.lexists(_x(layout.target))
