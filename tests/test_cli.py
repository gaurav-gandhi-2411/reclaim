from __future__ import annotations

import argparse
import os
from collections import namedtuple
from pathlib import Path

import pytest

import reclaim.reconciliation as reconciliation_module
from reclaim.cli import _VERSION, _build_parser, _run_serve, main
from reclaim.index import InaccessibleEntry, ScanIndex
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode
from reclaim.models import FileRecord

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


@pytest.fixture(autouse=True)
def _isolate_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `main(...)` call in this file goes through `cli.main`'s `configure_logging(...)`
    call, which defaults to the real app-wide `data/logs/reclaim.log` relative to cwd --
    redirect it into this test's own `tmp_path` so running this file never writes a log file
    into the actual repo working directory (same isolation goal as every other DEFAULT_* path
    override elsewhere in this file, just applied automatically since G25's log wiring runs
    unconditionally on every `main()` call, unlike the opt-in-per-test `--db`/`--manifest`
    overrides)."""
    monkeypatch.setattr("reclaim.cli.DEFAULT_LOG_PATH", tmp_path / "reclaim.log")


def test_apply_dry_run_skips_duplicates_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression test for the real-disk-run stall: `apply` (dry-run) must be usable without
    ever triggering the size/hash-based duplicate pipeline — that pass is what had zero output
    for as long as anyone watched a 3.1M-file run. Default (no --include-duplicates) must
    report fast and never mention the duplicate category."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 200)
    (root / "b.bin").write_bytes(b"x" * 200)  # exact duplicate of a.bin
    db = tmp_path / "index.sqlite3"
    missing_config = tmp_path / "config.toml"

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(["apply", str(root), "--db", str(db), "--config", str(missing_config)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "duplicate detection skipped" in out
    assert "exact_duplicate" not in out


def test_apply_dry_run_include_duplicates_runs_dedup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--include-duplicates` opts back into the hash-based pipeline; the byte-identical pair
    must then surface as an `exact_duplicate` candidate in the printed report.

    Files are 2MB (not a tiny size) so the pair clears the default materiality gate
    (`config.categories.duplicates.min_reclaim_bytes`, 1MB) — a duplicate pair below that
    floor is deliberately never hashed at all (see `test_index.py`'s materiality tests)."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    (root / "b.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    db = tmp_path / "index.sqlite3"
    missing_config = tmp_path / "config.toml"

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "apply",
            str(root),
            "--db",
            str(db),
            "--config",
            str(missing_config),
            "--include-duplicates",
            "--tier",
            "both",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "duplicate detection skipped" not in out
    assert "exact_duplicate" in out


def test_apply_report_shows_materiality_exclusion_alongside_real_duplicate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tiny duplicate pair (below the default 1MB materiality floor) must be reported as
    excluded rather than silently dropped, while a real 2MB duplicate pair in the same tree is
    still detected and reported normally."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "tiny_a.bin").write_bytes(b"t" * 100)
    (root / "tiny_b.bin").write_bytes(b"t" * 100)
    (root / "large_a.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    (root / "large_b.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    db = tmp_path / "index.sqlite3"
    missing_config = tmp_path / "config.toml"

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "apply",
            str(root),
            "--db",
            str(db),
            "--config",
            str(missing_config),
            "--include-duplicates",
            "--tier",
            "both",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "exact_duplicate" in out  # the real 2MB pair still surfaces
    assert "1 size bucket(s) excluded as immaterial" in out
    assert "theoretical best-case size 100 bytes" in out


def test_apply_include_categories_restricts_to_named_categories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--include-categories` narrows an already tier/root-filtered selection to just the named
    fine-grained categories — the staged-rollout mechanism for applying a reviewed subset of one
    enabled group (here: dev_artifacts) while deferring the rest of that same group to a later
    run. `dev_artifacts.enabled=True` makes BOTH node_modules and pycache Tier A candidates;
    `--include-categories dev_artifact_pycache` must apply only the pycache one."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "package.json").write_bytes(b"{}")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg.js").write_bytes(b"x" * 100)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "mod.pyc").write_bytes(b"y" * 100)
    db = tmp_path / "index.sqlite3"
    config_path = tmp_path / "config.toml"
    config_path.write_text("[categories.dev_artifacts]\nenabled = true\n", encoding="utf-8")
    # This test exercises dev_artifacts (forced off, and every candidate forced to Tier B, in
    # the Stage 2 default safe mode) -- explicit power-mode opt-in, isolated to this test's own
    # mode log, is what makes "dev_artifacts.enabled=true actually enables it" true again.
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "apply",
            str(root),
            "--db",
            str(db),
            "--config",
            str(config_path),
            "--include-categories",
            "dev_artifact_pycache",
            "--mode-log",
            str(mode_log),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "restricted selection to ['dev_artifact_pycache']" in out
    assert "1/2 tier/root-eligible candidate(s) kept" in out
    assert "dev_artifact_pycache: count=1" in out
    assert "dev_artifact_node_modules" not in out


# --- P0-5: inaccessible-path accounting + `reclaim reconcile` --------------------------------


def test_scan_prints_inaccessible_size_accounting_when_paths_are_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reclaim scan`'s CLI output surfaces the best-effort inaccessible-path size accounting
    (P0-5), not just the bare skip count -- and stays silent about it when nothing was skipped."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "readable.txt").write_text("ok", encoding="utf-8")
    blocked_dir = root / "blocked_dir"
    blocked_dir.mkdir()
    (blocked_dir / "inner.txt").write_text("hidden", encoding="utf-8")

    real_scandir = os.scandir

    def fake_scandir(path: object, *args: object, **kwargs: object) -> object:
        if "blocked_dir" in str(path):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", fake_scandir)
    db = tmp_path / "index.sqlite3"

    assert main(["scan", str(root), "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "inaccessible-path size accounting" in out
    assert "0 bytes known" in out
    assert "1 path(s) with no size estimate at all" in out


def test_scan_omits_inaccessible_accounting_line_when_nothing_was_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("ok", encoding="utf-8")
    db = tmp_path / "index.sqlite3"

    assert main(["scan", str(root), "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "inaccessible-path size accounting" not in out


def test_reconcile_errors_when_no_index_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_db = tmp_path / "does_not_exist.sqlite3"

    exit_code = main(["reconcile", "C:\\", "--db", str(missing_db)])

    assert exit_code == 1
    assert "no index found" in capsys.readouterr().err


def test_reconcile_rejects_a_non_volume_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "index.sqlite3"
    with ScanIndex(db):
        pass  # just needs to exist on disk for _run_reconcile's own existence check

    exit_code = main(["reconcile", str(tmp_path), "--db", str(db)])

    assert exit_code == 1
    assert "is not a drive root" in capsys.readouterr().err


def test_reconcile_prints_delta_bytes_and_pct_for_a_fully_scanned_volume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the CLI entry point: a volume with a real indexed file plus a
    persisted inaccessible entry reconciles against a (faked, for determinism) real disk-usage
    figure, and the residual-gap explanation is printed whenever any inaccessible path has no
    size estimate at all."""
    db = tmp_path / "index.sqlite3"
    volume = Path("C:/")
    with ScanIndex(db) as index:
        index.upsert_records(
            [
                FileRecord(
                    path=Path("C:/a.txt"),
                    is_dir=False,
                    size_bytes=1000,
                    attributes=0,
                    ext=".txt",
                    git_repo_root=None,
                    git_repo_clean=False,
                    mtime=1.0,
                    ctime=1.0,
                    dev=1,
                    ino=1,
                )
            ],
            scanned_at=1000.0,
        )
        index.replace_inaccessible_under_root(
            volume,
            [
                InaccessibleEntry(
                    path="C:/blocked",
                    error="denied",
                    size_estimate_bytes=None,
                    size_estimate_is_lower_bound=False,
                )
            ],
            scanned_at=1000.0,
        )

    monkeypatch.setattr(
        reconciliation_module.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(total=10_000, used=2000, free=8000),
    )

    exit_code = main(["reconcile", "C:\\", "--db", str(db)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "indexed_bytes=1000" in out
    assert "reported_total_bytes=1000" in out
    assert "delta_bytes=1000" in out
    assert "delta_pct=50.00%" in out
    assert "1 inaccessible path(s) have no size estimate at all" in out


# --- serve: hard loopback-only bind gate ------------------------------------------------------


def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--version` is the fast, no-op path used to isolate pure interpreter+import overhead
    from real subcommand work for cold-start measurement (see packaging/RELEASE_RUNBOOK.md) --
    it must short-circuit before argparse's `required=True` subparsers check (no subcommand
    needed) and never touch any real subsystem."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"reclaim {_VERSION}"


def test_serve_default_host_is_loopback() -> None:
    """The default `--host` (no flag passed at all) must be a real loopback IP — this tool
    moves and permanently deletes files on command from whatever hits its API."""
    args = _build_parser().parse_args(["serve"])
    assert args.host == "127.0.0.1"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "localhost", "example.com", "0000:0000::1"],
)
def test_serve_rejects_non_loopback_host_at_parse_time(host: str) -> None:
    """`--host` is hard-gated at argparse parse time, before any server code ever runs — a
    typo'd or malicious `0.0.0.0`/LAN address must never reach `uvicorn.run`. `localhost` is
    deliberately rejected too (not just 0.0.0.0): it's a DNS/hosts-file lookup, not a literal
    loopback IP, and a tampered hosts file could point it elsewhere."""
    with pytest.raises(SystemExit) as exc_info:
        _build_parser().parse_args(["serve", "--host", host])
    assert exc_info.value.code == 2


def test_serve_accepts_ipv6_loopback() -> None:
    args = _build_parser().parse_args(["serve", "--host", "::1"])
    assert args.host == "::1"


def test_run_serve_revalidates_host_even_when_argparse_is_bypassed() -> None:
    """Defense in depth: `_run_serve` re-checks its `args.host` itself, so a caller that builds
    an `argparse.Namespace` directly (bypassing the CLI's own `type=` gate entirely) still can't
    reach `uvicorn.run` with a non-loopback host."""
    args = argparse.Namespace(
        host="0.0.0.0",
        port=8420,
        db=Path("unused.sqlite3"),
        config=Path("unused-config.toml"),
        vault_dir=None,
        manifest_path=None,
    )
    with pytest.raises(argparse.ArgumentTypeError):
        _run_serve(args)


# --- No-elevation guard: every mutating command refuses to run elevated ------------------------


def test_apply_refuses_to_run_elevated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    db = tmp_path / "index.sqlite3"
    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    def _boom() -> None:
        from reclaim.elevation import ElevatedProcessError

        raise ElevatedProcessError("simulated: process is elevated")

    monkeypatch.setattr("reclaim.cli.assert_not_elevated", _boom)

    exit_code = main(["apply", str(root), "--db", str(db), "--apply"])
    assert exit_code == 1
    assert "simulated: process is elevated" in capsys.readouterr().err
    assert (root / "a.bin").exists()  # refused before touching anything


def test_undo_refuses_to_run_elevated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        from reclaim.elevation import ElevatedProcessError

        raise ElevatedProcessError("simulated: process is elevated")

    monkeypatch.setattr("reclaim.cli.assert_not_elevated", _boom)

    exit_code = main(["undo", "some-batch-id"])
    assert exit_code == 1
    assert "simulated: process is elevated" in capsys.readouterr().err


def test_purge_refuses_to_run_elevated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        from reclaim.elevation import ElevatedProcessError

        raise ElevatedProcessError("simulated: process is elevated")

    monkeypatch.setattr("reclaim.cli.assert_not_elevated", _boom)

    exit_code = main(["purge"])
    assert exit_code == 1
    assert "simulated: process is elevated" in capsys.readouterr().err


def test_serve_refuses_to_run_elevated(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        from reclaim.elevation import ElevatedProcessError

        raise ElevatedProcessError("simulated: process is elevated")

    monkeypatch.setattr("reclaim.cli.assert_not_elevated", _boom)

    exit_code = main(["serve"])
    assert exit_code == 1
    assert "simulated: process is elevated" in capsys.readouterr().err


# --- malformed config.toml is a friendly message, not a raw traceback — audit D16 ---------------


def test_apply_reports_clean_message_for_malformed_toml_syntax(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid TOML syntax (`tomllib.TOMLDecodeError`) must never surface as a raw traceback --
    it should print one actionable line naming the file and exit 1."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    db = tmp_path / "index.sqlite3"
    bad_config = tmp_path / "config.toml"
    bad_config.write_text("this is not [valid toml", encoding="utf-8")

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(["apply", str(root), "--db", str(db), "--config", str(bad_config)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "reclaim apply:" in err
    assert str(bad_config) in err


def test_apply_reports_clean_message_for_unknown_config_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A recognized-syntax TOML file with an unrecognized key raises `UnknownConfigKeyError`
    (a `ValueError` subclass) -- same friendly-message treatment as bad TOML syntax, not a raw
    traceback."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    db = tmp_path / "index.sqlite3"
    bad_config = tmp_path / "config.toml"
    bad_config.write_text("a_typo_or_attack_key = true\n", encoding="utf-8")

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(["apply", str(root), "--db", str(db), "--config", str(bad_config)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "reclaim apply:" in err
    assert str(bad_config) in err


def test_apply_reports_clean_message_for_pydantic_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A recognized key with an invalid value raises `pydantic.ValidationError` -- same
    friendly-message treatment, not a raw traceback."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    db = tmp_path / "index.sqlite3"
    bad_config = tmp_path / "config.toml"
    # schema_version is declared as an int everywhere in this codebase; a string fails
    # Config.model_validate with a real pydantic.ValidationError.
    bad_config.write_text('schema_version = "not-an-int"\n', encoding="utf-8")

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(["apply", str(root), "--db", str(db), "--config", str(bad_config)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "reclaim apply:" in err
    assert str(bad_config) in err


@pytest.mark.parametrize("command", ["serve", "undo", "purge"])
def test_other_config_consuming_commands_report_clean_message_too(
    command: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D16 wires the friendly-message helper into every CLI entry point that loads config.toml,
    not just `apply` -- `serve`/`undo`/`purge` each get the same treatment."""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    bad_config = tmp_path / "config.toml"
    bad_config.write_text("this is not [valid toml", encoding="utf-8")

    args = [command, "--config", str(bad_config)]
    if command == "undo":
        args.append("some-batch-id")
    else:
        # "serve"/"purge" both resolve the live mode before loading config; point at an
        # isolated, guaranteed-nonexistent mode log rather than depending on whatever
        # data/mode_log.jsonl happens (or doesn't) to exist relative to the test runner's cwd.
        args.extend(["--mode-log", str(tmp_path / "mode_log.jsonl")])

    exit_code = main(args)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert f"reclaim {command}:" in err
    assert str(bad_config) in err


# --- serve: clean messages for port-bind failures — audit F22 ----------------------------------


def test_serve_reports_clean_message_when_port_already_in_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind-in-use failure from `uvicorn.run` must never surface as a raw OSError
    traceback -- it should print one actionable line to stderr and exit 1."""
    import errno

    import uvicorn

    def _boom(app: object, **kw: object) -> None:
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(uvicorn, "run", _boom)

    db = tmp_path / "index.sqlite3"
    exit_code = main(["serve", "--db", str(db)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "port 8420 is already in use" in err
    assert "--port" in err


def test_serve_reports_clean_message_when_port_bind_denied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission-denied bind failure (e.g. a privileged port) is a distinct condition
    from port-in-use and must get its own clear message, not be conflated with EADDRINUSE."""
    import errno

    import uvicorn

    def _boom(app: object, **kw: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(uvicorn, "run", _boom)

    db = tmp_path / "index.sqlite3"
    exit_code = main(["serve", "--db", str(db)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "permission denied" in err
    assert "port 8420" in err


def test_serve_does_not_swallow_unrelated_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the two known bind-failure conditions get a friendly message -- any other
    OSError must still propagate so it isn't silently hidden from the user."""
    import errno

    import uvicorn

    def _boom(app: object, **kw: object) -> None:
        raise OSError(errno.EIO, "some other I/O error")

    monkeypatch.setattr(uvicorn, "run", _boom)

    db = tmp_path / "index.sqlite3"
    with pytest.raises(OSError, match="some other I/O error"):
        main(["serve", "--db", str(db)])


def test_scan_does_not_check_elevation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only `scan` touches nothing and must never be blocked by the elevation guard —
    only the mutating commands (apply/undo/purge/serve) check it."""

    def _boom() -> None:
        raise AssertionError("scan must never call assert_not_elevated")

    monkeypatch.setattr("reclaim.cli.assert_not_elevated", _boom)

    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    db = tmp_path / "index.sqlite3"
    assert main(["scan", str(root), "--db", str(db)]) == 0


# --- scan: progress output, Wave 1 finding #2 (2026-07-30 real-disk diagnosis) ------------------


def test_scan_prints_progress_heartbeat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw CLI `scan` command previously called `scan_tree` with no `on_progress` at all --
    a real 2.67M-file scan sat silent on the terminal for 7+ minutes, indistinguishable from a
    hang. Forces the heartbeat interval to 0 (matching `test_scanner.py`'s own convention) so
    this test doesn't depend on real wall-clock timing to see at least one heartbeat line."""
    import reclaim.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)

    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    (root / "b.bin").write_bytes(b"y" * 100)
    db = tmp_path / "index.sqlite3"

    assert main(["scan", str(root), "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "reclaim scan: scanning..." in out
    assert "entries visited" in out


# --- scan: clean message on disk-full during the index write — audit A5 ------------------------


def test_scan_reports_clean_message_when_disk_is_full(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk-full failure writing the index must never surface as an uncaught traceback --
    it should print one actionable line to stderr and exit 1, same pattern as F22's serve
    port-collision handling."""
    import errno

    from reclaim.index import ScanIndex as ScanIndexClass

    def fake_upsert(self: object, records: object, *, scanned_at: float) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ScanIndexClass, "upsert_records", fake_upsert)

    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 100)
    db = tmp_path / "index.sqlite3"

    exit_code = main(["scan", str(root), "--db", str(db)])

    assert exit_code == 1
    assert "disk is full" in capsys.readouterr().err


# --- dashboard: serve + auto-open browser -------------------------------------------------------


def test_dashboard_parses_with_the_same_defaults_as_serve() -> None:
    serve_args = _build_parser().parse_args(["serve"])
    dashboard_args = _build_parser().parse_args(["dashboard"])
    for attr in ("host", "port", "db", "config", "vault_dir", "manifest"):
        assert getattr(serve_args, attr) == getattr(dashboard_args, attr)


def test_dashboard_rejects_non_loopback_host_same_as_serve() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _build_parser().parse_args(["dashboard", "--host", "0.0.0.0"])
    assert exc_info.value.code == 2


def test_dashboard_opens_browser_and_delegates_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reclaim dashboard` must do exactly what `reclaim serve` does (same app, same bind
    guard) plus open the dashboard URL in the default browser — proven here by mocking
    `uvicorn.run` (never actually starts a server / blocks) and `webbrowser.open` (never
    actually launches a browser) and asserting both were called with the right arguments."""
    import threading
    import webbrowser

    import uvicorn

    run_calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: run_calls.append(kw))

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    class _ImmediateTimer:
        def __init__(
            self, interval: float, function: object, args: tuple[object, ...] = ()
        ) -> None:
            self._function = function
            self._args = args

        def start(self) -> None:
            self._function(*self._args)  # type: ignore[operator]

    monkeypatch.setattr(threading, "Timer", _ImmediateTimer)

    db = tmp_path / "index.sqlite3"
    exit_code = main(["dashboard", "--db", str(db)])

    assert exit_code == 0
    assert opened == ["http://127.0.0.1:8420"]
    assert run_calls == [{"host": "127.0.0.1", "port": 8420}]


def test_dashboard_refuses_to_run_elevated(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        from reclaim.elevation import ElevatedProcessError

        raise ElevatedProcessError("simulated: process is elevated")

    monkeypatch.setattr("reclaim.cli.assert_not_elevated", _boom)

    exit_code = main(["dashboard"])
    assert exit_code == 1
    assert "simulated: process is elevated" in capsys.readouterr().err
