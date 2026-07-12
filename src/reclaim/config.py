from __future__ import annotations

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


class CategoriesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="forbid")

    # Conservative default: the node_modules-in-clean-repo exemption requires this to be
    # explicitly turned on (spec: "category explicitly enabled").
    dev_artifacts: bool = False


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
