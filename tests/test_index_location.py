from __future__ import annotations

from pathlib import Path

import pytest

import reclaim.cli as cli_module
from reclaim.approvals import channel_file_path
from reclaim.cli import _build_parser, main
from reclaim.config import load_config
from reclaim.index_location import INDEX_FILE_NAME, default_index_path, resolve_index_db

# Every subcommand that takes `--db`, with the `_run_*` function `main` dispatches to and whether
# the subcommand also has its own `--config` option. ADR-0041: all of them must land on the same
# index for the same config.toml.
_DB_COMMANDS: list[tuple[list[str], str, bool]] = [
    (["scan", "SCANROOT"], "_run_scan", False),
    (["index-prune"], "_run_index_prune", False),
    (["reconcile", "C:\\"], "_run_reconcile", False),
    (["apply", "SCANROOT"], "_run_apply", True),
    (["undo", "batch1"], "_run_undo", True),
    (["purge"], "_run_purge", True),
    (["serve"], "_run_serve", True),
    (["dashboard"], "_run_dashboard", True),
    (["mcp-serve"], "_run_mcp_serve", True),
]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reclaim.cli.DEFAULT_LOG_PATH", tmp_path / "reclaim.log")
    monkeypatch.chdir(tmp_path)


def _write_config(path: Path, index_dir: Path) -> Path:
    # TOML literal string ('...') so Windows backslashes are not read as escapes.
    path.write_text(f"[storage]\nindex_dir = '{index_dir}'\n", encoding="utf-8")
    return path


def test_no_storage_section_keeps_the_legacy_default(tmp_path: Path) -> None:
    assert resolve_index_db(None, tmp_path / "absent.toml") == default_index_path()
    assert load_config(None).storage.index_dir == ""


def test_index_dir_redirects_the_index(tmp_path: Path) -> None:
    target = tmp_path / "bigdisk" / "reclaim"
    cfg = _write_config(tmp_path / "config.toml", target)
    assert resolve_index_db(None, cfg) == target / INDEX_FILE_NAME


def test_explicit_db_wins_over_index_dir(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "config.toml", tmp_path / "bigdisk")
    explicit = tmp_path / "mine.sqlite3"
    assert resolve_index_db(explicit, cfg) == explicit


@pytest.mark.parametrize("bad", ["relative/dir", "..\\up", "C:\\a\\..\\b"])
def test_relative_or_dotdot_index_dir_is_rejected(tmp_path: Path, bad: str) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[storage]\nindex_dir = '{bad}'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="index_dir"):
        resolve_index_db(None, cfg)


def test_unreadable_config_raises_instead_of_falling_back_to_the_legacy_index(
    tmp_path: Path,
) -> None:
    """Fail closed: a quiet fallback would let one entry point use a second, empty index."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage\nindex_dir = ", encoding="utf-8")
    with pytest.raises(Exception, match=r"(?i)toml|invalid|expected"):
        resolve_index_db(None, cfg)


@pytest.mark.parametrize(("argv", "runner", "has_config"), _DB_COMMANDS)
def test_every_db_command_resolves_the_same_index(
    argv: list[str],
    runner: str,
    has_config: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "bigdisk"
    cfg = _write_config(tmp_path / "config.toml", target)  # cwd == tmp_path: the default config
    seen: list[Path] = []

    def fake(args: object, **_kw: object) -> int:
        seen.append(args.db)  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(cli_module, runner, fake)
    full = [a if a != "SCANROOT" else str(tmp_path) for a in argv]
    if has_config:
        full += ["--config", str(cfg)]
    assert main(full) == 0
    assert seen == [target / INDEX_FILE_NAME]
    # The approval channel is "next to the index": it must follow the index to the same place.
    assert channel_file_path(seen[0]).parent == target


@pytest.mark.parametrize(("argv", "runner", "has_config"), _DB_COMMANDS)
def test_every_db_command_honours_an_explicit_db(
    argv: list[str],
    runner: str,
    has_config: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _write_config(tmp_path / "config.toml", tmp_path / "bigdisk")
    explicit = tmp_path / "elsewhere" / "x.sqlite3"
    seen: list[Path] = []
    monkeypatch.setattr(cli_module, runner, lambda a, **_k: seen.append(a.db) or 0)
    full = [a if a != "SCANROOT" else str(tmp_path) for a in argv] + ["--db", str(explicit)]
    if has_config:
        full += ["--config", str(cfg)]
    assert main(full) == 0
    assert seen == [explicit]


def test_commands_that_never_open_the_index_have_no_db_option() -> None:
    """check-disk-space and auto-clean (so both scheduled tasks) walk the filesystem and never
    touch the scan index. If one ever gains a `--db`, it must be resolved by `main` like the rest
    -- this test fails to force that decision."""
    parser = _build_parser()
    for argv in (["check-disk-space"], ["auto-clean"]):
        assert not hasattr(parser.parse_args(argv), "db"), argv


def test_unreadable_config_for_a_command_without_config_handling_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "config.toml").write_text("[storage\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "_run_scan", lambda *_a, **_k: pytest.fail("must not scan"))
    assert main(["scan", str(tmp_path)]) == 2
    assert "cannot resolve the index location" in capsys.readouterr().err


def test_vault_default_is_not_moved_by_index_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0041: only the index and channel file move; the vault stays on the files' volume."""
    cfg = _write_config(tmp_path / "config.toml", tmp_path / "bigdisk")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli_module, "_run_serve", lambda a, **_k: captured.update(vault=a.vault_dir) or 0
    )
    assert main(["serve", "--config", str(cfg)]) == 0
    assert "bigdisk" not in str(captured["vault"])
