from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

# Feature: generic clutter-likelihood ranker (ADR-0021). Synthetic-but-realistic FILE-RECORD
# fixtures -- metadata only (path, ext, size, mtime/ctime, path-class, category, cluster
# stats, cloud flag), matching feedback_store.FeatureVector's field set minus
# sibling_decision_context (that field requires REAL decision history, which doesn't exist
# for this synthetic labeling task -- no atime anywhere, same discipline as Feature 3).
#
# These are path STRINGS, not real files on disk. The ranker's target ("generic
# clutter-likelihood": is this the KIND of thing usually safe-to-suggest, not "would GG
# delete this") is assessable from path/extension/metadata PATTERNS alone -- a .tmp file
# under AppData\Local\Temp needs no file content to judge, which is exactly why this is a
# "knowable, not personal" property per GG's framing, and why LLM judges reasoning from
# metadata alone (never real file content, never real user data) is a legitimate way to
# label it.
#
# Records are generated in BATCHES (one batch = one simulated review-queue session, 20
# records each) -- the grouping unit LightGBM's LambdaMART ranking loss needs, and the unit
# the eval's grouped train/eval split (ADR-0021) is done on: an entire batch lands in train
# OR eval, never split across, so the model can never learn batch-specific quirks in train
# and "cheat" on the same batch at eval time.

_SEED = 42
_RECORDS_PER_BATCH = 15
_NUM_BATCHES = 8  # 120 records total -- sized against measured local-LLM CPU-only
# throughput (ADR-0021: an unrelated process holds most of this machine's 8GB GPU, so
# labeling runs CPU-only; measured ~9.6-32.3s/call warm across the 3 judge models, making
# 120 x 3 = 360 total judge calls a ~1.75-hour background run rather than a multi-hour one
# at the originally-planned 300 records -- still a statistically meaningful sample for
# Fleiss' kappa, which doesn't require large N to be informative.
_BATCH_START_EPOCH = 1_700_000_000.0  # each batch gets a distinct synthetic "generated_at"
_BATCH_SPACING_SECONDS = 7 * 86400.0  # one week apart -- gives a real, orderable time axis
# alongside the grouped-split axis, so a future re-measurement could compare grouped-split
# against a time-split without regenerating fixtures.


@dataclass(frozen=True, slots=True)
class RankerClusterStats:
    cluster_size: int
    position_in_cluster: int | None
    raw_score: float
    score_kind: str
    is_recommended_keep: bool


@dataclass(frozen=True, slots=True)
class RankerFileRecord:
    record_id: str
    batch_id: str
    batch_generated_at: float
    path: str
    ext: str
    size_bytes: int
    mtime: float
    ctime: float
    path_class: str
    category: str
    cluster_stats: RankerClusterStats
    cloud_sync_flag: bool
    # Internal-only: which archetype pool this record was generated from. NEVER passed to
    # the LLM judges (they only ever see the fields above) and NEVER used as a training
    # label -- exists solely so this file's own fixture-sanity tests can confirm the
    # generator produces a genuinely mixed distribution across clutter/important/ambiguous
    # archetypes, not a trivially-separable toy set. Only the LLM CONSENSUS becomes the
    # training label (per GG's explicit "disagreement means the property isn't cleanly
    # knowable, forcing a label reintroduces fabrication" instruction).
    archetype: str
    archetype_group: str  # "clutter" | "important" | "ambiguous"


@dataclass(frozen=True, slots=True)
class _ArchetypeSpec:
    path: str
    ext: str
    size_bytes: int
    age_days: int
    path_class: str
    category: str
    cluster_factory: Callable[[random.Random], RankerClusterStats]


def _no_cluster(rng: random.Random) -> RankerClusterStats:
    return RankerClusterStats(
        cluster_size=1,
        position_in_cluster=None,
        raw_score=0.0,
        score_kind="none",
        is_recommended_keep=False,
    )


def _in_cluster(rng: random.Random, *, size: int, is_keep: bool) -> RankerClusterStats:
    return RankerClusterStats(
        cluster_size=size,
        position_in_cluster=rng.randint(0, size - 1),
        raw_score=round(rng.uniform(0.85, 1.0), 4),
        score_kind="hamming_distance" if rng.random() < 0.5 else "minhash_jaccard_distance",
        is_recommended_keep=is_keep,
    )


_FIRST_NAMES = ("alex", "sam", "jordan", "priya", "chen", "maya", "leo", "nina", "omar", "ines")
_PROJECT_NAMES = (
    "widget-app",
    "data-pipeline",
    "portfolio-site",
    "invoice-tool",
    "photo-backup",
    "budget-tracker",
    "recipe-app",
    "client-portal",
    "ml-experiment",
    "game-prototype",
)


# --- Clutter archetypes: usually safe-to-suggest kinds -----------------------------------


def _clutter_dev_artifact(rng: random.Random) -> _ArchetypeSpec:
    project = rng.choice(_PROJECT_NAMES)
    kind = rng.choice(["node_modules", "__pycache__", "target", "dist", "build"])
    depth = rng.choice(["", "/src", "/lib/deep/nested"])
    ext = rng.choice([".pyc", ".o", ".class", ""])
    name = f"module_{rng.randint(1, 999)}{ext}"
    cluster_size = rng.randint(3, 40)
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/projects/{project}/{kind}{depth}/{name}",
        ext=ext,
        size_bytes=rng.randint(500, 500_000),
        age_days=rng.randint(5, 400),
        path_class="other",
        category="dev_artifacts",
        cluster_factory=lambda r: _in_cluster(r, size=cluster_size, is_keep=False),
    )


def _clutter_package_cache(rng: random.Random) -> _ArchetypeSpec:
    cache_kind = rng.choice(["pip", "npm", "nuget", "yarn"])
    name = f"pkg_{rng.randint(1, 9999)}-{rng.randint(1, 20)}.{rng.randint(0, 9)}.tar"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/AppData/Local/{cache_kind}-cache/{name}",
        ext=".tar",
        size_bytes=rng.randint(10_000, 20_000_000),
        age_days=rng.randint(10, 600),
        path_class="other",
        category="package_caches",
        cluster_factory=_no_cluster,
    )


def _clutter_temp_or_browser_cache(rng: random.Random) -> _ArchetypeSpec:
    kind = rng.choice(["tmp", "browser_cache", "thumbnail_cache"])
    ext = rng.choice([".tmp", ".dat", ".cache", ""])
    name = f"{kind}_{rng.randint(1, 99999)}{ext}"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/AppData/Local/Temp/{name}",
        ext=ext,
        size_bytes=rng.randint(100, 5_000_000),
        age_days=rng.randint(1, 200),
        path_class="temp",
        category="temp_and_browser_caches",
        cluster_factory=_no_cluster,
    )


def _clutter_stale_installer(rng: random.Random) -> _ArchetypeSpec:
    app = rng.choice(["Setup", "Installer", "App_Update", "Driver_Pack"])
    ext = rng.choice([".exe", ".msi"])
    version = f"v{rng.randint(1, 9)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Downloads/{app}_{version}{ext}",
        ext=ext,
        size_bytes=rng.randint(5_000_000, 300_000_000),
        age_days=rng.randint(200, 900),
        path_class="downloads",
        category="old_installers",
        cluster_factory=_no_cluster,
    )


def _clutter_old_export(rng: random.Random) -> _ArchetypeSpec:
    kind = rng.choice(["export", "backup", "report_final", "data_dump"])
    ext = rng.choice([".csv", ".zip", ".bak"])
    suffix = rng.choice(["", "(1)", "(2)", "_old", "_FINAL"])
    year = rng.randint(15, 20)
    cluster_size = rng.randint(2, 5)
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Downloads/{kind}_20{year}{suffix}{ext}",
        ext=ext,
        size_bytes=rng.randint(1_000, 50_000_000),
        age_days=rng.randint(300, 1200),
        path_class="downloads",
        category="archive_pairs" if ext == ".zip" else "duplicates",
        cluster_factory=lambda r: _in_cluster(r, size=cluster_size, is_keep=False),
    )


def _clutter_large_log(rng: random.Random) -> _ArchetypeSpec:
    project = rng.choice(_PROJECT_NAMES)
    name = f"app_{rng.randint(1, 365):03d}.log"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/AppData/Local/{project}/logs/{name}",
        ext=".log",
        size_bytes=rng.randint(10_000_000, 800_000_000),
        age_days=rng.randint(30, 500),
        path_class="other",
        category="large_logs",
        cluster_factory=_no_cluster,
    )


_CLUTTER_GENERATORS: tuple[Callable[[random.Random], _ArchetypeSpec], ...] = (
    _clutter_dev_artifact,
    _clutter_package_cache,
    _clutter_temp_or_browser_cache,
    _clutter_stale_installer,
    _clutter_old_export,
    _clutter_large_log,
)


# --- Important archetypes: usually NOT safe-to-suggest kinds ------------------------------


def _important_document(rng: random.Random) -> _ArchetypeSpec:
    kind = rng.choice(["Resume", "Cover_Letter", "Meeting_Notes", "Thesis_Draft", "Contract"])
    ext = rng.choice([".pdf", ".docx"])
    name = f"{kind}_{rng.randint(1, 20)}{ext}"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Documents/{name}",
        ext=ext,
        size_bytes=rng.randint(20_000, 5_000_000),
        age_days=rng.randint(1, 800),
        path_class="documents",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


def _important_config_or_key(rng: random.Random) -> _ArchetypeSpec:
    relative_path, ext = rng.choice(
        [
            (".ssh/id_rsa", ""),
            (".aws/credentials", ""),
            (".env", ".env"),
            ("wallet.dat", ".dat"),
            ("id_ed25519.pem", ".pem"),
        ]
    )
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/{relative_path}",
        ext=ext,
        size_bytes=rng.randint(200, 8_000),
        age_days=rng.randint(1, 1000),
        path_class="other",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


def _important_source_code(rng: random.Random) -> _ArchetypeSpec:
    project = rng.choice(_PROJECT_NAMES)
    ext = rng.choice([".py", ".ts", ".rs", ".go"])
    name = f"module_{rng.randint(1, 50)}{ext}"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/projects/{project}/src/{name}",
        ext=ext,
        size_bytes=rng.randint(500, 60_000),
        age_days=rng.randint(0, 60),
        path_class="other",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


def _important_financial(rng: random.Random) -> _ArchetypeSpec:
    kind = rng.choice(["Tax_Return", "Invoice", "Bank_Statement", "Budget"])
    ext = rng.choice([".xlsx", ".pdf"])
    year = rng.randint(20, 25)
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Documents/Finance/{kind}_20{year}{ext}",
        ext=ext,
        size_bytes=rng.randint(20_000, 2_000_000),
        age_days=rng.randint(1, 700),
        path_class="documents",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


def _important_personal_media(rng: random.Random) -> _ArchetypeSpec:
    event = rng.choice(["Vacation", "Birthday", "Wedding", "Graduation"])
    ext = rng.choice([".jpg", ".mp4"])
    year = rng.randint(15, 25)
    name = f"IMG_{rng.randint(1000, 9999)}{ext}"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Pictures/{event}_20{year}/{name}",
        ext=ext,
        size_bytes=rng.randint(500_000, 40_000_000),
        age_days=rng.randint(10, 2000),
        path_class="other",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


_IMPORTANT_GENERATORS: tuple[Callable[[random.Random], _ArchetypeSpec], ...] = (
    _important_document,
    _important_config_or_key,
    _important_source_code,
    _important_financial,
    _important_personal_media,
)


# --- Ambiguous archetypes: genuinely uncertain from metadata alone ------------------------
# Deliberately included, not smoothed away -- realistic distributions have real ambiguity,
# and this is also what stress-tests the cross-LLM disagreement/exclusion mechanism
# (ADR-0021): these are the records most likely to produce genuine 3-judge disagreement.


def _ambiguous_unclear_zip(rng: random.Random) -> _ArchetypeSpec:
    name = rng.choice(["project_files", "misc", "stuff", "archive", "untitled"])
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Downloads/{name}_{rng.randint(1, 99)}.zip",
        ext=".zip",
        size_bytes=rng.randint(50_000, 100_000_000),
        age_days=rng.randint(30, 900),
        path_class="downloads",
        category="archive_pairs",
        cluster_factory=_no_cluster,
    )


def _ambiguous_old_log(rng: random.Random) -> _ArchetypeSpec:
    name = f"diagnostics_{rng.randint(1, 999)}.log"
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Documents/{name}",
        ext=".log",
        size_bytes=rng.randint(5_000, 3_000_000),
        age_days=rng.randint(60, 900),
        path_class="documents",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


def _ambiguous_bak_file(rng: random.Random) -> _ArchetypeSpec:
    kind = rng.choice(["settings", "project", "document", "database"])
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Documents/{kind}_{rng.randint(1, 99)}.bak",
        ext=".bak",
        size_bytes=rng.randint(10_000, 20_000_000),
        age_days=rng.randint(60, 1000),
        path_class="documents",
        category="duplicates",
        cluster_factory=lambda r: _in_cluster(r, size=2, is_keep=False),
    )


def _ambiguous_large_download(rng: random.Random) -> _ArchetypeSpec:
    ext = rng.choice([".mp4", ".iso", ".zip"])
    return _ArchetypeSpec(
        path=f"C:/Users/{rng.choice(_FIRST_NAMES)}/Downloads/file_{rng.randint(1, 999)}{ext}",
        ext=ext,
        size_bytes=rng.randint(200_000_000, 4_000_000_000),
        age_days=rng.randint(1, 500),
        path_class="downloads",
        category="uncategorized",
        cluster_factory=_no_cluster,
    )


_AMBIGUOUS_GENERATORS: tuple[Callable[[random.Random], _ArchetypeSpec], ...] = (
    _ambiguous_unclear_zip,
    _ambiguous_old_log,
    _ambiguous_bak_file,
    _ambiguous_large_download,
)

# Archetype pools weighted toward realism: most files in a review queue are either clearly
# clutter or clearly not (that's WHY they got surfaced/not-surfaced by the deterministic
# detectors in the first place), with a meaningful but minority ambiguous tail -- not a
# uniform 1/3-1/3-1/3 split, which would overstate how often genuine ambiguity occurs.
_ARCHETYPE_GROUPS: tuple[tuple[tuple[Callable[[random.Random], _ArchetypeSpec], ...], str], ...] = (
    (_CLUTTER_GENERATORS, "clutter"),
    (_IMPORTANT_GENERATORS, "important"),
    (_AMBIGUOUS_GENERATORS, "ambiguous"),
)
_GROUP_SELECTION_WEIGHTS = (0.45, 0.40, 0.15)  # clutter, important, ambiguous


def build_ranker_fixture_batches() -> list[list[RankerFileRecord]]:
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic synthetic fixture data
    batches: list[list[RankerFileRecord]] = []
    record_counter = 0
    for batch_index in range(_NUM_BATCHES):
        batch_id = f"batch_{batch_index:03d}"
        batch_generated_at = _BATCH_START_EPOCH + batch_index * _BATCH_SPACING_SECONDS
        records: list[RankerFileRecord] = []
        for _ in range(_RECORDS_PER_BATCH):
            generators, group_name = rng.choices(
                _ARCHETYPE_GROUPS, weights=_GROUP_SELECTION_WEIGHTS, k=1
            )[0]
            generator = rng.choice(generators)
            spec = generator(rng)
            age_seconds = spec.age_days * 86400.0
            mtime = batch_generated_at - age_seconds
            ctime = mtime - rng.uniform(0, 3600.0 * 24 * 3)
            cluster_stats = spec.cluster_factory(rng)
            record_counter += 1
            records.append(
                RankerFileRecord(
                    record_id=f"{batch_id}_r{record_counter:05d}",
                    batch_id=batch_id,
                    batch_generated_at=batch_generated_at,
                    path=spec.path,
                    ext=spec.ext,
                    size_bytes=spec.size_bytes,
                    mtime=mtime,
                    ctime=ctime,
                    path_class=spec.path_class,
                    category=spec.category,
                    cluster_stats=cluster_stats,
                    cloud_sync_flag=rng.random() < 0.12,
                    archetype=generator.__name__,
                    archetype_group=group_name,
                )
            )
        batches.append(records)
    return batches


def build_all_ranker_records() -> list[RankerFileRecord]:
    return [record for batch in build_ranker_fixture_batches() for record in batch]
