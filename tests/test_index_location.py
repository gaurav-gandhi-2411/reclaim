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
    (["scan", "SCANROOT"], "_run_scan", True),
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


def _storage_toml(index_dir: str) -> str:
    # TOML literal string ('...') so Windows backslashes are not read as escapes.
    return f"[storage]\nindex_dir = '{index_dir}'\n"


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
    monkeypatch.setattr(
        cli_module, "_run_reconcile", lambda *_a, **_k: pytest.fail("must not reconcile")
    )
    assert main(["reconcile", "C:\\"]) == 2
    assert "cannot resolve the index location" in capsys.readouterr().err


@pytest.mark.parametrize("config_text", ["[storage\n", "[storage]\nindex_dir = 5\n"])
def test_scan_with_an_unreadable_config_refuses_before_touching_any_index(
    config_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ADR-0041: scan has its own --config now (for exclusions), but a config whose [storage]
    cannot be read still must not fall back to the legacy index. Nothing is scanned, no index
    is opened, and no database file appears anywhere."""
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    default_db = tmp_path / "legacy" / "index.sqlite3"
    monkeypatch.setattr(cli_module, "_DEFAULT_DB_PATH", default_db)
    monkeypatch.setattr(cli_module, "scan_tree", lambda *_a, **_k: pytest.fail("must not scan"))
    monkeypatch.setattr(cli_module, "ScanIndex", lambda *_a, **_k: pytest.fail("must not open"))
    for extra in ([], ["--config", str(tmp_path / "config.toml")]):
        assert main(["scan", str(tmp_path), *extra]) != 0
    assert "cannot resolve the index location" in capsys.readouterr().err
    assert not default_db.exists() and not default_db.parent.exists()
    assert list(tmp_path.rglob("*.sqlite3")) == []


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


def test_explicit_config_beats_the_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teeth for the verifier's mutation (resolve ignoring --config): the default config.toml in
    the cwd says one dir, --config says another; config-bearing commands must follow --config."""
    _write_config(tmp_path / "config.toml", tmp_path / "from_default")
    other = _write_config(tmp_path / "other.toml", tmp_path / "from_flag")
    seen: list[Path] = []
    monkeypatch.setattr(cli_module, "_run_serve", lambda a, **_k: seen.append(a.db) or 0)
    assert main(["serve", "--config", str(other)]) == 0
    assert seen == [tmp_path / "from_flag" / INDEX_FILE_NAME]


def test_all_commands_agree_when_launched_from_a_different_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifier defect D1: a frozen install anchors its data/config on the exe directory, but
    `--config` used to default to the launch cwd, so `serve` and `scan` could pick different
    indexes. Simulate: install dir holds config.toml; cwd is somewhere else entirely."""
    install = tmp_path / "install"
    install.mkdir()
    target = tmp_path / "bigdisk"
    _write_config(install / "config.toml", target)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr("reclaim.index_location.data_root", lambda: install)
    monkeypatch.setattr(cli_module, "_DEFAULT_CONFIG_PATH", install / "config.toml")
    got: dict[str, Path] = {}
    for _argv, runner, _ in _DB_COMMANDS:
        monkeypatch.setattr(
            cli_module, runner, lambda a, _n=runner, **_k: got.__setitem__(_n, a.db) or 0
        )
    for argv, _runner, _has_cfg in _DB_COMMANDS:
        # the parser default must be rebuilt after the monkeypatch so --config defaults to install/
        full = [a if a != "SCANROOT" else str(elsewhere) for a in argv]
        assert main(full) == 0
    assert set(got.values()) == {target / INDEX_FILE_NAME}, got


def test_nonexistent_explicit_config_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "_run_serve", lambda *_a, **_k: pytest.fail("must not serve"))
    assert main(["serve", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_index_dir_naming_the_index_file_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_storage_toml(r"D:\x\reclaim_index.sqlite3"), encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        resolve_index_db(None, cfg)


def test_default_config_path_is_anchored_on_the_exe_dir_when_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teeth for the verifier's mutation (default reverted to a cwd-relative Path): exercises the
    real `default_config_path` / `data_root` wiring instead of patching the resulting constant."""
    from reclaim import app_paths
    from reclaim.index_location import default_config_path

    assert default_config_path() == Path("config.toml")  # dev/test: unchanged
    monkeypatch.setattr(app_paths, "compiled_exe_dir", lambda: tmp_path / "install")
    assert default_config_path() == tmp_path / "install" / "config.toml"
    assert default_index_path() == tmp_path / "install" / "data" / INDEX_FILE_NAME


def test_directory_given_as_config_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "adir").mkdir()
    monkeypatch.setattr(cli_module, "_run_serve", lambda *_a, **_k: pytest.fail("must not serve"))
    assert main(["serve", "--config", str(tmp_path / "adir")]) == 2
