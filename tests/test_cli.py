from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reclaim.cli import _build_parser, _run_serve, main
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode


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


# --- serve: hard loopback-only bind gate ------------------------------------------------------


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
