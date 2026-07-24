from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from reclaim.mode import current_mode
from reclaim.models import SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS, Mode

logger = structlog.get_logger(__name__)

# ADR-0027 (schema versioning): the config shape as of introducing `schema_version` — bump
# whenever a top-level or category field is added/removed/changed in a way that would otherwise
# be invisible to a reader of an older release.
CONFIG_SCHEMA_VERSION = 1

# ADR-0027: every config/category class below sets `extra="ignore"` (never `"allow"`) —
# config.toml is parsed into `Config` and consulted in-memory (`load_effective_config`'s
# `model_copy` calls layer `mode`/`categories` overrides on top), but the app never writes a
# `Config`/category config back to config.toml, so there is no read-modify-write cycle for an
# unrecognized key to be lost from the way there is for `executor.QuarantineManifestEntry`.
# An unrecognized top-level/category/category-field key is tolerated (logged, not raised) ONLY
# when config.toml's own `schema_version` genuinely claims to be newer than
# `CONFIG_SCHEMA_VERSION` — see `_check_unknown_config_keys`. Without that claim, an unrecognized
# key still raises `UnknownConfigKeyError`, same as `extra="forbid"` did before this ADR — a
# security requirement `evals/test_ai_safety_gate.py` depends on (a hand-edited config.toml must
# never be able to smuggle an AI-related category/field into the deterministic pipeline just
# because it's unrecognized), not merely a stylistic preference.

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
        f"{local_appdata}/Yarn/Cache",
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
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

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
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_package_cache_paths)
    # ADR-0001: `None` -> direct permanent delete on apply (no vault, no send2trash); an int ->
    # vault + manifest + restore, with `purge` eligible once that many days have passed.
    # Package caches redownload deterministically at negligible cost, so they default to `None`.
    # Model-weight caches are NOT covered here — see `ModelCachesConfig` / ADR-0003.
    retention_days: int | None = None
    # ADR-0003 addendum: package-manager caches (pip/npm/uv/yarn/conda/.m2/.gradle) are exempt
    # from `safety.direct_delete_size_guard_bytes` regardless of size — the guard exists to
    # protect expensive-to-recover items, and re-fetching public package artifacts on the next
    # build/install is the cheapest possible recovery, not an expensive one. Without this
    # exemption a large pip/uv/gradle cache gets vaulted (no immediate disk-free gain) purely
    # because of its size, even though its actual recovery cost never justified that caution.
    size_guard_exempt: bool = True


class ModelCachesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

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
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    enabled: bool = False
    cache_paths: list[str] = Field(default_factory=_default_browser_and_thumbnail_cache_paths)
    temp_roots: list[str] = Field(default_factory=_default_temp_roots)
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore.
    # Browser/temp caches regenerate automatically, so they default to `None`.
    retention_days: int | None = None


class CrashDumpsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    enabled: bool = False
    paths: list[str] = Field(default_factory=_default_crash_dump_paths)
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore.
    # Crash dumps/WER reports are diagnostic-only, so they default to `None`.
    retention_days: int | None = None


class OldInstallersConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    # Spec: review-queue by default, auto-quarantine only if the user explicitly opts in.
    enabled: bool = False
    max_age_days: int = 90
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. Installers aren't deterministically rebuildable
    # (may need a re-download), so they default to the 30-day vaulted retention.
    retention_days: int | None = 30


class LargeLogsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    enabled: bool = False
    min_size_bytes: int = 50 * 1024 * 1024
    stale_days: int = 30
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. Logs have no rebuild command, so they default to
    # the 30-day vaulted retention.
    retention_days: int | None = 30


class DevArtifactsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    # Conservative default: the node_modules-in-clean-repo exemption requires this to be
    # explicitly turned on (spec: "category explicitly enabled"). Stage 3 also uses this flag
    # to decide Tier A vs Tier B for every dev-artifact candidate, not just the git exemption.
    enabled: bool = False
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore.
    # Dev artifacts (node_modules, .venv, target/, ...) are rebuildable from an adjacent
    # manifest, so they default to `None`.
    retention_days: int | None = None


class ArchivePairsConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    enabled: bool = False
    # ADR-0001: `None` -> direct permanent delete on apply; an int -> vault + manifest + restore,
    # `purge`-eligible after that many days. The archive itself isn't reconstructable from
    # anything else, so it defaults to the 30-day vaulted retention.
    retention_days: int | None = 30


class DuplicatesConfig(BaseModel):
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

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
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

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
    model_config = SettingsConfigDict(extra="ignore")  # ADR-0027: see module docstring above

    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    categories: CategoriesConfig = Field(default_factory=CategoriesConfig)
    # Stage 2: resolved by `load_config` from `reclaim.mode.current_mode()` (the mode-change
    # log), never read from config.toml directly — a hand-edited config file must never be the
    # thing that silently disables the safety boundary. Defaults to `Mode.SAFE` here too (not
    # just in `load_config`) so a bare `Config()` construction — every existing test, any future
    # caller that doesn't go through `load_config` — is the conservative default, never an
    # accidental power-mode config.
    mode: Mode = Mode.SAFE
    # ADR-0027: absent (pre-versioning) config.toml files validate with this defaulting to `1` —
    # the literal truth, since `1` is the version every existing field on this class belongs to.
    # A hand-edited config.toml is never expected to set this itself; it exists for a future
    # release's migration logic, not as a user-facing knob.
    schema_version: int = Field(default=CONFIG_SCHEMA_VERSION)


class UnknownConfigKeyError(ValueError):
    """Raised by `_check_unknown_config_keys` when `config.toml` has a key this version of the
    code doesn't recognize AND the file doesn't declare itself as coming from a newer schema
    version -- see that function's docstring for why this is a hard reject, not a warning, in
    that specific case."""


def _check_unknown_config_keys(data: dict[str, Any], *, allow_unknown: bool) -> None:
    """Scans `config.toml`'s raw top-level/category/category-field keys against what this
    version of the code recognizes.

    `allow_unknown=True` (the file's own declared `schema_version` is genuinely higher than
    `CONFIG_SCHEMA_VERSION` -- see `load_config`): logs and continues. ADR-0027's forward-compat
    goal only ever covers *this exact scenario* -- a real newer release of this same project
    adding a field, where the reader can tell from the version number that's what happened.

    `allow_unknown=False` (no such claim): RAISES `UnknownConfigKeyError` instead of merely
    logging. This is deliberately the same hard-reject `extra="forbid"` gave before ADR-0027 --
    `evals/test_ai_safety_gate.py`'s adversarial tests require exactly this (a hand-edited
    config.toml trying to smuggle in an `ai_`-named category or field must be rejected outright,
    never silently absorbed with only a log line no one is guaranteed to read) and a first
    version of this ADR broke that security boundary by tolerating ALL unknown keys
    unconditionally, version claim or not -- fixed here by scoping the tolerance to only the
    case it was actually meant for."""
    unknown_top = sorted(set(data) - set(Config.model_fields))
    categories_raw = data.get("categories")
    unknown_categories = (
        sorted(set(categories_raw) - set(CategoriesConfig.model_fields))
        if isinstance(categories_raw, dict)
        else []
    )
    unknown_fields_by_category: dict[str, list[str]] = {}
    if isinstance(categories_raw, dict):
        for category_name, category_data in categories_raw.items():
            field = CategoriesConfig.model_fields.get(category_name)
            if field is None or not isinstance(category_data, dict):
                continue
            category_model = field.annotation
            if not (isinstance(category_model, type) and issubclass(category_model, BaseModel)):
                continue
            unknown_fields = sorted(set(category_data) - set(category_model.model_fields))
            if unknown_fields:
                unknown_fields_by_category[category_name] = unknown_fields

    if not (unknown_top or unknown_categories or unknown_fields_by_category):
        return

    if allow_unknown:
        if unknown_top:
            logger.warning("config.unknown_keys_ignored", scope="top_level", keys=unknown_top)
        if unknown_categories:
            logger.warning(
                "config.unknown_keys_ignored", scope="categories", keys=unknown_categories
            )
        for category_name, unknown_fields in unknown_fields_by_category.items():
            logger.warning(
                "config.unknown_keys_ignored",
                scope=f"categories.{category_name}",
                keys=unknown_fields,
            )
        return

    all_unknown = [
        *unknown_top,
        *unknown_categories,
        *(field for fields in unknown_fields_by_category.values() for field in fields),
    ]
    raise UnknownConfigKeyError(
        f"config.toml has unrecognized key(s) {all_unknown} and does not declare a "
        f"schema_version newer than {CONFIG_SCHEMA_VERSION} -- refusing to load rather than "
        "silently ignore an unrecognized key with no forward-compat justification for it "
        "(extra='ignore' only ever applies once a genuinely newer schema_version is declared)."
    )


def apply_safe_mode_category_overrides(categories: CategoriesConfig) -> CategoriesConfig:
    """Forces every group in `SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS` off regardless of what was
    requested, leaving every other category's `enabled` flag untouched. Iterates the shared
    frozenset (rather than hardcoding the three names here too) so there is exactly one
    definition of "which categories are forced off in safe mode," matching this module's own
    `REBUILDABLE_CATEGORY_GROUPS` discipline in models.py.

    Safe mode restricts WHICH categories can ever produce a candidate at all;
    `executor.apply_batch`'s mode-aware routing separately restricts HOW anything from an
    enabled category can ever be deleted — two independent layers, neither a substitute for
    the other."""
    updates = {
        group: getattr(categories, group).model_copy(update={"enabled": False})
        for group in SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS
    }
    return categories.model_copy(update=updates)


def load_config(path: Path | None) -> Config:
    """Load config from a TOML file, or return built-in defaults if `path` is None/missing.

    Pure TOML parsing only — does NOT resolve the live mode or apply any safe-mode category
    override. Every existing caller (the huge majority of this test suite; any internal code
    that needs "exactly what config.toml says," no policy layered on top) keeps working
    unchanged. Real end-user entry points (the CLI, the dashboard) must call
    `load_effective_config` instead — see its docstring for why the two are kept separate.

    ADR-0027: an unrecognized top-level/category/category-field key only ever gets tolerated
    (logged, not raised) when config.toml's own `schema_version` genuinely claims to be newer
    than `CONFIG_SCHEMA_VERSION` — a real signal this came from a newer release of this project,
    not a typo or an adversarial hand-edit. Absent that claim, an unrecognized key raises
    `UnknownConfigKeyError` — the same hard-reject `extra="forbid"` gave before ADR-0027, and a
    requirement `evals/test_ai_safety_gate.py` depends on (see `_check_unknown_config_keys`).
    """
    if path is None or not path.exists():
        return Config()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    declared_schema_version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
    claims_newer_schema = (
        isinstance(declared_schema_version, int) and declared_schema_version > CONFIG_SCHEMA_VERSION
    )
    _check_unknown_config_keys(data, allow_unknown=claims_newer_schema)
    config = Config.model_validate(data)
    if config.schema_version > CONFIG_SCHEMA_VERSION:
        logger.warning(
            "config.newer_schema_version_detected",
            config_path=str(path),
            known_schema_version=CONFIG_SCHEMA_VERSION,
            encountered_schema_version=config.schema_version,
        )
    return config


def load_effective_config(path: Path | None, *, mode: Mode | None = None) -> Config:
    """`load_config(path)`, then layers Stage 2's safety-boundary policy on top: this is what
    the CLI and the dashboard call, never `load_config` directly, for anything that will
    actually drive candidate generation or apply/purge against a real disk.

    `mode` resolves from `reclaim.mode.current_mode()` (the mode-change log) when not given
    explicitly — the explicit override exists for tests and any caller that already knows the
    live mode without re-reading the log, never for silently overriding it in production.
    Whenever the resolved mode is SAFE, `apply_safe_mode_category_overrides` is applied to the
    returned config's categories, regardless of what config.toml itself requested — deliberately
    kept as a separate function from `load_config` (rather than baked into it) so the hundreds
    of existing tests/call sites exercising plain TOML-parsing behavior are never silently
    affected by a mode this function alone is responsible for resolving.
    """
    config = load_config(path)
    resolved_mode = mode if mode is not None else current_mode()
    categories = (
        apply_safe_mode_category_overrides(config.categories)
        if resolved_mode == Mode.SAFE
        else config.categories
    )
    return config.model_copy(update={"mode": resolved_mode, "categories": categories})


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide cached config, read once from ./config.toml if present."""
    default_path = Path("config.toml")
    return load_config(default_path if default_path.exists() else None)
