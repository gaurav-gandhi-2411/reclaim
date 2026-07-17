from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute defaults for production; SafetyValidator tests substitute a fixture-relative
# list so real C:\Windows etc. are never touched during development (spec: never scan or
# modify the real disk during development).
DEFAULT_PROTECTED_ROOTS: tuple[str, ...] = (
    "C:/Windows",
    "C:/Windows/*",
    "C:/Program Files",
    "C:/Program Files/*",
    "C:/Program Files (x86)",
    "C:/Program Files (x86)/*",
    "C:/ProgramData",
    "C:/ProgramData/*",
    "*/AppData/Local/Programs/*",
    "*/AppData/Local/Microsoft/WindowsApps/*",
)

# Leading "*/" patterns are user-profile-relative and match under any root, so tests
# don't need to override these even when protected_roots is substituted for fixtures.
DEFAULT_DOCKER_WSL_ROOTS: tuple[str, ...] = (
    "*/AppData/Local/Docker/*",
    "*/AppData/Local/Packages/*WSL*/*",
    "*/ProgramData/Docker/*",
    "//wsl$/*",
)

DEFAULT_PROTECTED_EXTENSIONS: tuple[str, ...] = (
    ".kdbx",
    ".ppk",
    ".pem",
    ".key",
    ".pfx",
    ".crt",
    ".gpg",
)

DEFAULT_DATABASE_EXTENSIONS: tuple[str, ...] = (".db", ".sqlite", ".mdf")
DEFAULT_VM_EXTENSIONS: tuple[str, ...] = (".vhdx", ".vmdk", ".qcow2")

DEFAULT_FINANCE_TOKENS: tuple[str, ...] = (
    "tax",
    "1099",
    "w2",
    "w-2",
    "invoice",
    "receipt",
    "statement",
    "contract",
    "agreement",
    "legal",
    "irs",
)


def _win_path(env_var: str, fallback: str) -> str:
    """Resolves a Windows env var to a posix-form path string, falling back to a literal
    default when the env var is unset (e.g. a CI runner or dev session missing a profile
    var) so the Stage 3 category defaults are still deterministic in that case."""
    value = os.environ.get(env_var)
    return Path(value).as_posix() if value else fallback


def _default_package_cache_paths() -> list[str]:
    """Real default Windows locations for package download caches. Global caches, so —
    unlike dev-artifact directories — these need no project-manifest-adjacency check.

    Model-weight caches (HuggingFace hub, torch hub, Ollama) are deliberately NOT here — see
    `_default_model_cache_paths` / ADR-0003. Re-acquiring a package cache costs a `pip install`;
    re-acquiring a 100+GB model checkpoint, or one that's gated/private/fine-tuned and may not
    be re-downloadable at all, is a different order of recovery cost and gets its own category
    with vaulted (never direct-delete) retention.
    """
    local_appdata = _win_path("LOCALAPPDATA", "C:/Users/Default/AppData/Local")
    appdata = _win_path("APPDATA", "C:/Users/Default/AppData/Roaming")
    userprofile = _win_path("USERPROFILE", "C:/Users/Default")
    return [
        f"{local_appdata}/pip/Cache",
        f"{appdata}/npm-cache",
        f"{local_appdata}/uv/cache",
        # Judgment call: only the user-profile conda pkgs cache is covered by default (the
        # alternative "<conda-install>/pkgs" location can't be derived without querying a
        # running conda installation) — a user can add it via [categories.package_caches.paths]
        # in config.toml if their conda install lives elsewhere.
        f"{userprofile}/.conda/pkgs",
        # Global caches, not project-adjacent dev artifacts (see the [categories] docstring on
        # `.gradle`/`.m2` in the spec for why these two live here, not in dev-artifact detection).
        f"{userprofile}/.m2/repository",
        f"{userprofile}/.gradle/caches",
    ]


def _default_model_cache_paths() -> list[str]:
    """Real default Windows locations for ML model-weight caches (ADR-0003). Split out from
    `_default_package_cache_paths` because these hold the multi-GB-to-100+GB artifacts whose
    recovery cost (bandwidth, time, and sometimes gated/private auth that may no longer be
    available) is nothing like a package manager's re-download."""
    userprofile = _win_path("USERPROFILE", "C:/Users/Default")
    return [
        f"{userprofile}/.cache/huggingface/hub",
        f"{userprofile}/.cache/torch/hub",
        f"{userprofile}/.ollama/models",
    ]


def _default_browser_and_thumbnail_cache_paths() -> list[str]:
    local_appdata = _win_path("LOCALAPPDATA", "C:/Users/Default/AppData/Local")
    return [
        f"{local_appdata}/Google/Chrome/User Data/*/Cache",
        f"{local_appdata}/Microsoft/Edge/User Data/*/Cache",
        f"{local_appdata}/Mozilla/Firefox/Profiles/*/cache2",
        f"{local_appdata}/Microsoft/Windows/Explorer/thumbcache_*.db",
    ]


def _default_temp_roots() -> list[str]:
    temp = _win_path("TEMP", "C:/Users/Default/AppData/Local/Temp")
    return [temp, "C:/Windows/Temp"]


def _default_crash_dump_paths() -> list[str]:
    local_appdata = _win_path("LOCALAPPDATA", "C:/Users/Default/AppData/Local")
    return [
        f"{local_appdata}/CrashDumps",
        "C:/ProgramData/Microsoft/Windows/WER",
    ]


class SafetyConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    deny: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)
    protected_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_ROOTS))
    docker_wsl_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_DOCKER_WSL_ROOTS))
    protected_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROTECTED_EXTENSIONS)
    )
    database_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DATABASE_EXTENSIONS)
    )
    vm_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_VM_EXTENSIONS))
    finance_tokens: list[str] = Field(default_factory=lambda: list(DEFAULT_FINANCE_TOKENS))
    # ADR-0003: recovery cost, not category, is what should gate permanent deletion. Any single
    # candidate at or above this size is forced from `retention_days=None` (direct-delete) to
    # vaulted, regardless of which category it belongs to — protects against a future
    # direct-delete category (or a misconfigured one) reaching an unboundedly expensive-to-
    # redo item. 1GB default: generous enough to leave small, genuinely-cheap-to-rebuild cache
    # files on the fast direct-delete path, strict enough to catch the model/large-archive case
    # that motivated this guard.
    direct_delete_size_guard_bytes: int = 1024 * 1024 * 1024
    # Retention window applied only when the guard above fires — independent of the category's
    # own `retention_days` (which is `None` by construction whenever this guard is reachable).
    direct_delete_size_guard_retention_days: int = 30


class PackageCachesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_package_cache_paths)
    # ADR-0001: `None` -> direct permanent delete on apply (no vault, no send2trash); an int ->
    # vault + manifest + restore, with `purge` eligible once that many days have passed.
    # Package caches redownload deterministically at negligible cost, so they default to `None`.
    # Model-weight caches are NOT covered here — see `ModelCachesConfig` / ADR-0003.
    retention_days: int | None = None


class ModelCachesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_model_cache_paths)
    # ADR-0003: individual model-weight files by extension, matched under the SAME `paths` roots
    # only (never a disk-wide sweep) — defense in depth for a cache layout the whole-directory
    # match above doesn't fully cover (e.g. a user-added custom root, or a hub layout variant).
    model_extensions: list[str] = Field(default_factory=lambda: [".safetensors", ".ckpt", ".bin"])
    # ADR-0003: unlike every other package/dev-artifact cache, model weights default to VAULTED
    # (30-day) retention, never `None`. Re-acquiring a model checkpoint can cost hours of
    # bandwidth for a multi-GB-to-100+GB file, and a gated/private/fine-tuned/manually-pushed
    # model may not be re-downloadable at all if the original access has lapsed — "rebuildable"
    # was being decided by path type, not real rebuild cost, and this corrects that.
    retention_days: int | None = 30


class TempAndBrowserCachesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    cache_paths: list[str] = Field(default_factory=_default_browser_and_thumbnail_cache_paths)
    temp_roots: list[str] = Field(default_factory=_default_temp_roots)
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore.
    # Browser/temp caches regenerate automatically, so they default to `None`.
    retention_days: int | None = None


class CrashDumpsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_crash_dump_paths)
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore.
    # Crash dumps/WER reports are diagnostic-only, so they default to `None`.
    retention_days: int | None = None


class OldInstallersConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    # Spec: review-queue by default, auto-quarantine only if the user explicitly opts in.
    enabled: bool = False
    max_age_days: int = 90
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. Installers aren't deterministically rebuildable
    # (may need a re-download), so they default to the 30-day vaulted retention.
    retention_days: int | None = 30


class LargeLogsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    min_size_bytes: int = 50 * 1024 * 1024
    stale_days: int = 30
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. Logs have no rebuild command, so they default to
    # the 30-day vaulted retention.
    retention_days: int | None = 30


class DevArtifactsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    # Conservative default: the node_modules-in-clean-repo exemption requires this to be
    # explicitly turned on (spec: "category explicitly enabled"). Stage 3 also uses this flag
    # to decide Tier A vs Tier B for every dev-artifact candidate, not just the git exemption.
    enabled: bool = False
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore.
    # Dev artifacts (node_modules, .venv, target/, ...) are rebuildable from an adjacent
    # manifest, so they default to `None`.
    retention_days: int | None = None


class ArchivePairsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. The archive itself isn't reconstructable from
    # anything else, so it defaults to the 30-day vaulted retention.
    retention_days: int | None = 30


class DuplicatesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. A duplicate's bytes only exist in the kept copy
    # elsewhere, so it defaults to the 30-day vaulted retention.
    retention_days: int | None = 30
    # Materiality gate (2026-07-17 real-disk finding): a size bucket is only ever hashed if its
    # *theoretical* best-case reclaim — (member_count - 1) * size, i.e. every non-kept member
    # turning out to be an exact duplicate — clears this floor. On one real `C:\`, 80% of files
    # shared a size with another file, but the collision list was dominated by empty/near-empty
    # files (333K zero-byte files, thousands of 2/4/17/41/83/110-byte files) whose full bucket
    # could never reclaim anything material even in the best case. Hashing them wasted I/O for
    # zero possible benefit. Default 1MB: generous enough not to skip any bucket with real
    # reclaim potential, strict enough to skip the tiny-file noise that dominated collision
    # counts without dominating collision bytes.
    min_reclaim_bytes: int = 1024 * 1024


class CategoriesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    dev_artifacts: DevArtifactsConfig = Field(default_factory=DevArtifactsConfig)
    package_caches: PackageCachesConfig = Field(default_factory=PackageCachesConfig)
    model_caches: ModelCachesConfig = Field(default_factory=ModelCachesConfig)
    temp_and_browser_caches: TempAndBrowserCachesConfig = Field(
        default_factory=TempAndBrowserCachesConfig
    )
    crash_dumps: CrashDumpsConfig = Field(default_factory=CrashDumpsConfig)
    old_installers: OldInstallersConfig = Field(default_factory=OldInstallersConfig)
    archive_pairs: ArchivePairsConfig = Field(default_factory=ArchivePairsConfig)
    large_logs: LargeLogsConfig = Field(default_factory=LargeLogsConfig)
    # Spec lists exact duplicates under "auto-quarantine eligible" (Tier-A-capable, gated like
    # every other category) but the Decision Policy's Tier B example also names "duplicate
    # clusters" — resolved the same way as every other category: Tier-A-capable, default off,
    # so by default duplicates land in Tier B exactly as that example describes.
    duplicates: DuplicatesConfig = Field(default_factory=DuplicatesConfig)


class Config(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid")

    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    categories: CategoriesConfig = Field(default_factory=CategoriesConfig)


def load_config(path: Path | None) -> Config:
    """Load config from a TOML file, or return built-in defaults if `path` is None/missing."""
    if path is None or not path.exists():
        return Config()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Config.model_validate(data)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide cached config, read once from ./config.toml if present."""
    default_path = Path("config.toml")
    return load_config(default_path if default_path.exists() else None)
