from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim import cli, dedup, regenerable
from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.config import (
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    DuplicatesConfig,
    ExclusionsConfig,
    LargeLogsConfig,
    SafetyConfig,
    exclusion_patterns,
    load_config,
)
from reclaim.dedup import generate_duplicate_candidates
from reclaim.detectors import generate_candidates
from reclaim.executor import SafetyInvariantError, apply_batch
from reclaim.index import ScanIndex
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode
from reclaim.models import Candidate, Mode, Tier, Verdict
from reclaim.regenerable import CommandResult, RegenerableEnv
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# ADR-0037: the product honours a user exclusion list on EVERY delete path. These tests build a
# fake `ml-projects` with the three owner-declared names (owner config, never a product default),
# each with a worktree, a venv, caches, data, and TEMP scratch directories named after the
# project, plus a non-excluded sibling that MUST still be cleaned -- so a fixture that merely
# fails to produce candidates cannot make a test pass.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

NAMES = ("fr-en-transformer", "shipdoc-extract", "intent-router")
DAY = 86400.0
OLD = time.time() - 60 * DAY


def _write(path: Path, content: bytes, *, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def _project_tree(base: Path, label: str) -> None:
    """One project-shaped tree: manifest + node_modules (a dev-artifact directory candidate), an
    old log (a file candidate), a __pycache__, and a data file."""
    _write(base / "package.json", b"{}")
    _write(base / "node_modules" / "pkg" / "index.js", b"x" * 300)
    _write(base / "pyproject.toml", b"[project]\n")
    _write(base / ".venv" / "Lib" / "site.py", b"y" * 300)
    _write(base / "logs" / "build.log", (label.encode() + b"-log") * 400)
    _write(base / "src" / "__pycache__" / "m.pyc", b"z" * 300)
    _write(base / "data" / "train.bin", (label.encode() + b"-data") * 500)


@dataclass
class World:
    home: Path
    ml: Path
    temp: Path
    excluded_roots: list[Path] = field(default_factory=list)
    sibling: Path = Path()
    # content -> (excluded shallow copy, non-excluded deeper copy): the keeper-selection trap.
    keeper_content: bytes = b""
    keeper_outside: Path = Path()
    solo_outside: Path = Path()
    plain_temp: Path = Path()


def world_excl_solo(w: World) -> Path:
    return w.ml / NAMES[0] / "solo.bin"


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    ml = home / "ml-projects"
    temp = home / "AppData" / "Local" / "Temp"
    w = World(home=home, ml=ml, temp=temp)
    w.keeper_content = b"keeper-bytes-" * 600
    for name in NAMES:
        proj = ml / name
        _project_tree(proj, name)
        _write(proj / "keeper.bin", w.keeper_content)  # shallowest copy -> would be the KEEPER
        _project_tree(ml / f"{name}-wt-feature", f"{name}-wt")  # sibling-dir worktree
        _project_tree(ml / "envs" / name, f"{name}-env")  # envs/ layout
        _write(temp / "claude" / f"C--Users-gaura-ml-projects-{name}" / "s.bin", b"s" * 900)
        _write(temp / f"scratch-{name}-run" / "t.bin", b"t" * 900)
        w.excluded_roots += [proj, ml / f"{name}-wt-feature", ml / "envs" / name]
    # Same content as the keepers above, deeper, in a NON-excluded project.
    w.sibling = ml / "other-project"
    _project_tree(w.sibling, "other")
    w.keeper_outside = _write(w.sibling / "deep" / "er" / "keeper.bin", w.keeper_content)
    # Exactly TWO copies: one shallow inside an excluded project (the keeper by heuristic), one
    # deeper outside. With only `deny` the excluded keeper is never evaluated, so the outside copy
    # would be proposed for deletion relative to it.
    _write(world_excl_solo(w), b"solo-keeper-" * 500)
    w.solo_outside = _write(w.sibling / "deep" / "er" / "solo.bin", b"solo-keeper-" * 500)
    # Two identical files, both outside every excluded tree: a real duplicate that must remain.
    _write(w.sibling / "data" / "copy_a.bin", b"real-dup-" * 700)
    _write(w.sibling / "data" / "copy_b.bin", b"real-dup-" * 700)
    # Siblings in TEMP that are not excluded and must still be cleaned.
    w.plain_temp = _write(temp / "plain-old-dir" / "x.bin", b"p" * 900)
    # Excluded temp content must not be protected just by luck of age: fresh enough ones are
    # skipped for age anyway, so everything above is 60 days old on purpose.
    return w


def _config(*, names: tuple[str, ...] = NAMES, deny: tuple[str, ...] = ()) -> Config:
    return Config(
        safety=SafetyConfig(deny=list(deny)),
        exclusions=ExclusionsConfig(project_names=list(names)),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30),
            large_logs=LargeLogsConfig(enabled=True, min_size_bytes=1_000, stale_days=30),
            duplicates=DuplicatesConfig(enabled=True, min_reclaim_bytes=0, retention_days=30),
        ),
        mode=Mode.POWER,
    )


def _under_any(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _is_excluded_path(path: Path) -> bool:
    text = str(path).lower()
    return any(name in text for name in NAMES)


def _scan(world: World, tmp_path: Path) -> ScanIndex:
    index = ScanIndex(tmp_path / "index.sqlite3")
    index.__enter__()
    scan_tree(world.home, index)
    return index


@pytest.fixture
def index(world: World, tmp_path: Path) -> Iterator[ScanIndex]:
    idx = _scan(world, tmp_path)
    try:
        yield idx
    finally:
        idx.__exit__(None, None, None)


def _all_candidates(index: ScanIndex, config: Config) -> list[Candidate]:
    safety = SafetyValidator(config)
    return generate_candidates(index, config, safety) + generate_duplicate_candidates(
        index, config, safety
    )


def _snapshot(roots: list[Path]) -> dict[str, int]:
    out: dict[str, int] = {}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = Path(dirpath) / f
                out[str(p)] = p.stat().st_size
    return out


# --- config ----------------------------------------------------------------------------------


def test_default_config_has_no_exclusions() -> None:
    cfg = Config()
    assert cfg.exclusions.project_names == []
    assert exclusion_patterns(cfg) == []


def test_exclusion_names_are_globs_tokens_and_validated(tmp_path: Path) -> None:
    cfg = _config(names=("Foo-Bar",), deny=("*/x/*",))
    assert exclusion_patterns(cfg) == ["*/x/*", "*foo-bar*"]
    for bad in ("", "  ", "a/b", "a\\b", "a*", "a?", "[x]"):
        with pytest.raises(ValueError):
            ExclusionsConfig(project_names=[bad])
    toml = tmp_path / "c.toml"
    toml.write_text('[exclusions]\nproject_names = ["alpha"]\n', encoding="utf-8")
    assert load_config(toml).exclusions.project_names == ["alpha"]


# --- (a) generic pipeline: scan -> candidates -> apply_batch ----------------------------------


def test_negative_control_without_exclusions_excluded_trees_are_candidates(
    world: World, index: ScanIndex
) -> None:
    """Teeth for the fixture itself: with NO exclusions the same tree does yield candidates
    inside the (to-be-)excluded projects, including the keeper-trap duplicate."""
    candidates = _all_candidates(index, _config(names=()))
    inside = [c for c in candidates if _is_excluded_path(c.path)]
    assert any(c.path.name == "node_modules" for c in inside)
    assert any(c.path.name == "build.log" for c in inside)
    assert any(c.path == world.solo_outside for c in candidates), (
        "without exclusions the deeper copy is proposed relative to the excluded keeper"
    )


def test_candidates_never_include_excluded_projects_but_siblings_still_do(
    world: World, index: ScanIndex
) -> None:
    candidates = _all_candidates(index, _config())
    assert candidates, "fixture must yield candidates"
    assert not [c.path for c in candidates if _is_excluded_path(c.path)]
    paths = {c.path for c in candidates}
    assert world.sibling / "node_modules" in paths
    assert world.sibling / "logs" / "build.log" in paths
    # A real duplicate wholly outside the excluded trees is still found.
    assert {world.sibling / "data" / "copy_a.bin", world.sibling / "data" / "copy_b.bin"} & paths
    # Keeper trap: the excluded shallow copy is dropped, so the deeper copy has no counterpart
    # and is NOT proposed for deletion "relative to" a file in an excluded tree.
    assert world.keeper_outside not in paths
    assert world.solo_outside not in paths


def test_dedup_never_hashes_excluded_files(
    world: World, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashed: list[Path] = []
    real_partial = dedup._compute_partial_hash
    real_full = dedup._compute_full_hash

    def spy_partial(path: Path, size: int) -> str:
        hashed.append(path)
        return real_partial(path, size)

    def spy_full(path: Path) -> str:
        hashed.append(path)
        return real_full(path)

    monkeypatch.setattr(dedup, "_compute_partial_hash", spy_partial)
    monkeypatch.setattr(dedup, "_compute_full_hash", spy_full)
    cfg = _config()
    generate_duplicate_candidates(index, cfg, SafetyValidator(cfg))
    assert hashed, "the non-excluded duplicates must still be hashed"
    assert not [p for p in hashed if _is_excluded_path(p)]


@pytest.mark.parametrize("apply", [False, True])
def test_apply_batch_touches_nothing_in_excluded_trees(
    world: World, index: ScanIndex, tmp_path: Path, apply: bool
) -> None:
    cfg = _config()
    safety = SafetyValidator(cfg)
    candidates = _all_candidates(index, cfg)
    before = _snapshot(world.excluded_roots)
    report = apply_batch(
        candidates,
        safety=safety,
        apply=apply,
        method="vault",
        mode=Mode.POWER,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
    )
    assert not [i.path for i in report.items if _is_excluded_path(Path(i.path))]
    assert _snapshot(world.excluded_roots) == before
    if apply:
        # Siblings really were cleaned (the protection is not "nothing ran").
        assert not (world.sibling / "node_modules").exists()
        assert not (world.sibling / "logs" / "build.log").exists()


def test_apply_batch_refuses_a_candidate_that_slipped_past_candidate_generation(
    world: World, tmp_path: Path
) -> None:
    """Last line of defence: even a hand-built candidate carrying an ELIGIBLE verdict is refused
    when its path matches an exclusion -- and so is a directory merely CONTAINING one."""
    cfg = _config()
    safety = SafetyValidator(cfg)

    def cand(path: Path, *, is_dir: bool) -> Candidate:
        return Candidate(
            path=path,
            is_dir=is_dir,
            category="large_log",
            category_group="large_logs",
            size_bytes=1,
            tier=Tier.B,
            rationale="x",
            rebuild_instruction=None,
            safety_verdict=Verdict.ELIGIBLE,
            safety_reason_code="DEFAULT_ELIGIBLE",
            retention_days=30,
        )

    victim = world.ml / NAMES[0] / "logs" / "build.log"
    ancestor = world.temp / "claude"  # contains C--Users-...-fr-en-transformer
    for bad in (cand(victim, is_dir=False), cand(ancestor, is_dir=True)):
        with pytest.raises(SafetyInvariantError, match="user exclusion"):
            apply_batch(
                [bad],
                safety=safety,
                apply=True,
                method="vault",
                mode=Mode.POWER,
                vault_dir=tmp_path / "v",
                manifest_path=tmp_path / "m.jsonl",
            )
    assert victim.exists() and ancestor.exists()


def test_safety_blocks_directory_that_contains_an_excluded_tree(world: World) -> None:
    cfg = _config()
    safety = SafetyValidator(cfg)
    from reclaim.scanner import GitRepoCache, build_record_for_path

    rec = build_record_for_path(world.temp / "claude", GitRepoCache())
    assert rec is not None
    result = safety.evaluate(rec)
    assert result.verdict == Verdict.BLOCKED and result.reason_code == "USER_EXCLUSION"


# --- purge -------------------------------------------------------------------------------------


def test_purge_refuses_the_whole_run_when_an_eligible_entry_came_from_an_excluded_path(
    world: World, tmp_path: Path
) -> None:
    from reclaim.executor import QuarantineManifestEntry, append_manifest_entries
    from reclaim.purge import purge_expired

    vault = tmp_path / "vault"
    manifest = tmp_path / "manifest.jsonl"
    vault.mkdir()
    entries = []
    for label, original in (
        ("excluded", world.ml / NAMES[1] / "old.bin"),
        ("other", world.sibling / "old.bin"),
    ):
        vp = vault / f"{label}.bin"
        vp.write_bytes(b"v" * 10)
        entries.append(
            QuarantineManifestEntry(
                batch_id="b1",
                original_path=original,
                vault_path=vp,
                size_bytes=10,
                category="large_log",
                category_group="large_logs",
                rationale="x",
                tier=Tier.B,
                method="vault",
                is_dir=False,
                retention_days=30,
                retention_until=time.time() - 10,
                quarantined_at=time.time() - 100,
            )
        )
    append_manifest_entries(manifest, entries)
    cfg = _config()
    # Existing ADR-0001 semantics, now reached through [exclusions]: any eligible entry failing the
    # fresh safety re-check aborts the WHOLE purge run (deleting nothing) -- the excluded-origin
    # entry is never purged, and neither is its non-excluded neighbour, until the user restores
    # the entry or removes the exclusion.
    with pytest.raises(SafetyInvariantError, match="pre-purge safety re-check"):
        purge_expired(
            apply=True,
            manifest_path=manifest,
            vault_dir=vault,
            safety=SafetyValidator(cfg),
            mode=Mode.POWER,
        )
    assert (vault / "excluded.bin").exists()
    assert (vault / "other.bin").exists()
    # Negative control: without the exclusion the same run purges both.
    purge_expired(
        apply=True,
        manifest_path=manifest,
        vault_dir=vault,
        safety=SafetyValidator(_config(names=())),
        mode=Mode.POWER,
    )
    assert not (vault / "excluded.bin").exists() and not (vault / "other.bin").exists()


# --- (b) review queue / dashboard API ----------------------------------------------------------

_HOST, _PORT = "127.0.0.1", 8420


def _client(tmp_path: Path, config: Config) -> TestClient:
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "api_index.sqlite3",
        config=config,
        vault_dir=tmp_path / "api_vault",
        manifest_path=tmp_path / "api_manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run.json",
        log_path=tmp_path / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    return TestClient(
        app,
        base_url=f"http://{_HOST}:{_PORT}",
        headers={security.CSRF_HEADER_NAME: app.state.reclaim.csrf_token},
    )


def test_dashboard_review_queue_and_apply_exclude_projects(world: World, tmp_path: Path) -> None:
    client = _client(tmp_path, _config())
    assert client.post("/api/scan", json={"path": str(world.home)}).status_code == 202
    assert client.get("/api/scan/status").json()["status"] == "completed"
    assert client.post("/api/candidates/warm").status_code == 202

    queue = client.get("/api/candidates?tier=both").json()
    queue_paths = [c["path"] for c in queue["candidates"]]
    assert queue_paths, "review queue must not be empty (fixture teeth)"
    assert not [p for p in queue_paths if _is_excluded_path(Path(p))]
    assert any(world.sibling.as_posix().lower() in p.lower() for p in queue_paths)

    clusters = client.get("/api/duplicate-clusters/review").json()
    blob = json.dumps(clusters).lower()
    assert not any(name in blob for name in NAMES)

    # The user (or a stale UI / AI suggestion) explicitly names an excluded file: silently
    # excluded, never applied.
    victim = world.ml / NAMES[2] / "logs" / "build.log"
    resp = client.post(
        "/api/apply", json={"tier": "both", "paths": [str(victim)], "dry_run": False}
    )
    assert resp.status_code in (202, 400, 409), resp.text
    assert victim.exists()

    blanket = client.post("/api/apply", json={"tier": "both", "dry_run": False})
    assert blanket.status_code == 202, blanket.text
    status = client.get("/api/apply/status").json()
    assert status["status"] == "completed", status
    applied = [i["path"] for i in status["result"]["items"]]
    assert applied
    assert not [p for p in applied if _is_excluded_path(Path(p))]
    for root in world.excluded_roots:
        assert (root / "package.json").exists()
        assert (root / "node_modules").exists()
    assert not (world.sibling / "node_modules").exists()


# --- (d)/(e) regenerable tier + `reclaim auto-clean` -------------------------------------------


def _env(world: World, patterns: tuple[str, ...] = ()) -> RegenerableEnv:
    return RegenerableEnv(
        home=world.home,
        local_appdata=world.home / "AppData" / "Local",
        temp_roots=(world.temp,),
        crash_dump_roots=(),
        which=lambda _n: None,
        running_process_names=lambda: frozenset(),
        run_command=lambda *_a: CommandResult(0, "", ""),
        has_open_handle=lambda _p: False,
        disk_anchor=world.home,
        # Directories get "now" mtimes when the fixture creates them, and a directory's age is
        # its NEWEST content -- so the clock is moved a year ahead instead of back-dating dirs.
        now=lambda: time.time() + 365 * DAY,
        excluded_patterns=patterns,
    )


def _excluded_temp_files(world: World) -> list[Path]:
    out: list[Path] = []
    for name in NAMES:
        out.append(world.temp / "claude" / f"C--Users-gaura-ml-projects-{name}" / "s.bin")
        out.append(world.temp / f"scratch-{name}-run" / "t.bin")
    return out


def test_regenerable_negative_control_cleans_everything_without_exclusions(
    world: World, tmp_path: Path
) -> None:
    report = regenerable.run_regenerable_clean(_env(world), apply=True, audit_log_path=None)
    assert report.bytes_removed > 0
    assert not any(p.exists() for p in _excluded_temp_files(world)), (
        "without exclusions the scratch dirs ARE deleted -- the protection has teeth"
    )


def test_regenerable_skips_excluded_scratch_and_still_cleans_siblings(
    world: World, tmp_path: Path
) -> None:
    patterns = tuple(exclusion_patterns(_config()))
    report = regenerable.run_regenerable_clean(
        _env(world, patterns), apply=True, audit_log_path=None
    )
    assert all(p.exists() for p in _excluded_temp_files(world))
    assert not world.plain_temp.exists(), "non-excluded sibling temp dir must still be cleaned"
    excluded_entries = report.excluded
    assert any(f"scratch-{NAMES[0]}-run" in e and f"*{NAMES[0]}*" in e for e in excluded_entries)
    # `%TEMP%/claude` holds excluded subdirectories: skipped WHOLE, not partially deleted.
    assert (world.temp / "claude").is_dir()
    assert regenerable.count_excluded_applied(report.applied_paths, patterns) == 0
    assert report.applied_paths, "something was applied"


def test_regenerable_dry_run_reports_the_same_exclusions(world: World) -> None:
    patterns = tuple(exclusion_patterns(_config()))
    report = regenerable.run_regenerable_clean(
        _env(world, patterns), apply=False, audit_log_path=None
    )
    assert report.excluded and report.applied_paths == []


def test_regenerable_excluded_root_is_skipped_excluded(world: World) -> None:
    root = world.ml / NAMES[0] / "tmp"
    _write(root / "old" / "a.bin", b"a" * 100)
    env = _env(world, tuple(exclusion_patterns(_config())))
    env.temp_roots = (root,)
    report = regenerable.run_regenerable_clean(env, apply=True, audit_log_path=None)
    item = next(i for i in report.items if i.key == "temp0")
    assert item.status == "skipped_excluded"
    assert (root / "old" / "a.bin").exists()


def test_count_excluded_applied_detects_a_violation() -> None:
    patterns = ["*foo*"]
    assert regenerable.count_excluded_applied(["C:/a/foo-x/y", "C:/a/bar"], patterns) == 1
    assert regenerable.count_excluded_applied(["C:/a/bar"], patterns) == 0


@dataclass
class _Cli:
    config: Path
    audit: Path


@pytest.fixture
def cli_env(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Cli:
    monkeypatch.setattr(service, "regenerable_clean_env", lambda: _env(world))
    monkeypatch.setattr(regenerable, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cli, "DEFAULT_LOG_PATH", tmp_path / "reclaim.log")
    monkeypatch.setattr(cli, "assert_not_elevated", lambda: None)
    cfg = tmp_path / "config.toml"
    names = ", ".join(f'"{n}"' for n in NAMES)
    cfg.write_text(
        f"[autoclean]\nenabled = true\n\n[exclusions]\nproject_names = [{names}]\n",
        encoding="utf-8",
    )
    return _Cli(config=cfg, audit=tmp_path / "audit.jsonl")


def test_cli_auto_clean_scheduled_apply_honours_installed_config(
    world: World, cli_env: _Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        ["auto-clean", "--apply", "--scheduled", "--json", "--config", str(cli_env.config)]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert all(p.exists() for p in _excluded_temp_files(world))
    assert not world.plain_temp.exists()
    assert out["excluded_applied"] == 0
    assert any(NAMES[1] in e for e in out["excluded"])
    # The audit log records the exclusions too.
    audit = [json.loads(x) for x in cli_env.audit.read_text("utf-8").splitlines()]
    assert any(row.get("excluded") for row in audit if not row.get("summary"))


def test_cli_text_report_states_excluded_applied_zero(
    world: World, cli_env: _Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(cli_env.config)])
    assert "excluded_applied: 0" in capsys.readouterr().out


def test_cli_fails_loudly_if_an_excluded_path_was_applied(
    world: World,
    cli_env: _Cli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mechanical `applied ∩ excluded = ∅` check is itself tested: simulate a skip-logic
    failure (the regenerable tier ignoring exclusions) and the CLI must exit non-zero."""
    real = regenerable.run_regenerable_clean

    def ignoring_exclusions(env: RegenerableEnv | None = None, **kw: object) -> object:
        assert env is not None
        patterns = env.excluded_patterns
        env.excluded_patterns = ()  # the bug being simulated
        report = real(env, **kw)  # type: ignore[arg-type]
        env.excluded_patterns = patterns
        return report

    monkeypatch.setattr(regenerable, "run_regenerable_clean", ignoring_exclusions)
    rc = cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(cli_env.config)])
    assert rc == 1
    assert "INVARIANT VIOLATION" in capsys.readouterr().err


def test_dashboard_one_click_preview_uses_config_exclusions(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "regenerable_clean_env", lambda: _env(world))
    monkeypatch.setattr(regenerable, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    client = _client(tmp_path, _config())
    body = client.post("/api/clean/regenerable", json={"apply": False}).json()
    assert body["excluded"], body
    assert body["excluded_applied"] == 0
    assert all(p.exists() for p in _excluded_temp_files(world))


# --- what `[safety] deny` ALREADY covered before ADR-0037 (kept as regression tests) -----------


def test_deny_alone_already_blocks_generic_candidates_and_user_selected_paths(
    world: World, index: ScanIndex, tmp_path: Path
) -> None:
    """Probed against the pre-ADR-0037 code (same assertions held there): a plain `[safety] deny`
    glob keeps scan-driven candidates and an explicitly named path out of the pipeline. What it
    did NOT cover is pinned by the other tests in this file (hashing, keeper trap, containing
    directories, apply_batch's last line, the regenerable tier, purge)."""
    deny = tuple(f"*/ml-projects/{n}/*" for n in NAMES)
    cfg = _config(names=(), deny=deny)
    candidates = _all_candidates(index, cfg)
    assert candidates
    assert not [c.path for c in candidates if _under_any(c.path, [world.ml / n for n in NAMES])]

    client = _client(tmp_path, cfg)
    assert client.post("/api/scan", json={"path": str(world.home)}).status_code == 202
    victim = world.ml / NAMES[1] / "logs" / "build.log"
    resp = client.post("/api/apply", json={"tier": "B", "paths": [str(victim)], "dry_run": False})
    assert resp.status_code in (202, 400, 409), resp.text
    assert victim.exists()
