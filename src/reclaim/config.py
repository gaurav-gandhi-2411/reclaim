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
    """Real default Windows locations for package/model download caches. Global caches, so —
    unlike dev-artifact directories — these need no project-manifest-adjacency check."""
    local_appdata = _win_path("LOCALAPPDATA", "C:/Users/Default/AppData/Local")
    appdata = _win_path("APPDATA", "C:/Users/Default/AppData/Roaming")
    userprofile = _win_path("USERPROFILE", "C:/Users/Default")
    return [
        f"{local_appdata}/pip/Cache",
        f"{appdata}/npm-cache",
        f"{local_appdata}/uv/cache",
        f"{userprofile}/.cache/huggingface/hub",
        f"{userprofile}/.cache/torch/hub",
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


class PackageCachesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_package_cache_paths)


class TempAndBrowserCachesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    cache_paths: list[str] = Field(default_factory=_default_browser_and_thumbnail_cache_paths)
    temp_roots: list[str] = Field(default_factory=_default_temp_roots)


class CrashDumpsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_crash_dump_paths)


class OldInstallersConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    # Spec: review-queue by default, auto-quarantine only if the user explicitly opts in.
    enabled: bool = False
    max_age_days: int = 90


class LargeLogsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    enabled: bool = False
    min_size_bytes: int = 50 * 1024 * 1024
    stale_days: int = 30


class CategoriesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    # Conservative default: the node_modules-in-clean-repo exemption requires this to be
    # explicitly turned on (spec: "category explicitly enabled"). Stage 3 also uses this flag
    # to decide Tier A vs Tier B for every dev-artifact candidate, not just the git exemption.
    dev_artifacts: bool = False
    package_caches: PackageCachesConfig = Field(default_factory=PackageCachesConfig)
    temp_and_browser_caches: TempAndBrowserCachesConfig = Field(
        default_factory=TempAndBrowserCachesConfig
    )
    crash_dumps: CrashDumpsConfig = Field(default_factory=CrashDumpsConfig)
    old_installers: OldInstallersConfig = Field(default_factory=OldInstallersConfig)
    archive_pairs: bool = False
    large_logs: LargeLogsConfig = Field(default_factory=LargeLogsConfig)
    # Spec lists exact duplicates under "auto-quarantine eligible" (Tier-A-capable, gated like
    # every other category) but the Decision Policy's Tier B example also names "duplicate
    # clusters" — resolved the same way as every other category: Tier-A-capable, default off,
    # so by default duplicates land in Tier B exactly as that example describes.
    duplicates: bool = False


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
