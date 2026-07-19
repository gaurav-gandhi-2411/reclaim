from __future__ import annotations

import ast
from pathlib import Path

import pytest

from reclaim.ai.models import AI_CATEGORY_GROUP_PREFIX, AICluster, AIClusterMember, AITrack
from reclaim.ai.review_queue import AIReviewQueue
from reclaim.config import Config, load_config
from reclaim.executor import SafetyInvariantError, apply_batch
from reclaim.models import Candidate, Verdict
from reclaim.safety import SafetyValidator

# §7.5: "the single most important test in the AI layer." Every test here proves a
# structural (not conventional) guarantee: AI-layer output can NEVER reach the auto-delete
# path and NEVER appears in the deterministic Tier-A set, under any config — including
# adversarial. This file runs BEFORE any real model (pHash, CLIP, ...) is wired in, against
# scaffolding/fabricated data only — that's deliberate (spec §8 build order item 1): the
# architectural boundary is proven first, independent of any specific feature's correctness.

_AI_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim" / "ai"


# --- 1. Static: no file under reclaim.ai imports the executor or send2trash -------------------


def _imported_module_names(source: str) -> set[str]:
    """Every dotted module name a Python source file's imports could plausibly reference,
    covering `import x.y`, `from x.y import z`, AND `from x import y` (which — for
    `x="reclaim"`, `y="executor"` — imports the exact same module as `import reclaim.executor`
    but wouldn't be caught by looking only at `node.module`; this form was found missing in
    an earlier verifier pass and is deliberately covered here)."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_imported_module_names_catches_the_from_reclaim_import_executor_form() -> None:
    """Regression test for the exact gap an earlier verifier pass found: `from reclaim
    import executor` imports the identical module as `import reclaim.executor`, but naively
    reading only `ImportFrom.module` would miss it (it'd see just "reclaim")."""
    names = _imported_module_names("from reclaim import executor\n")
    assert "reclaim.executor" in names


def test_ai_package_never_imports_the_executor_or_send2trash() -> None:
    """The structural half of the recommend-only guarantee: this is re-checked against
    every .py file under src/reclaim/ai/ on every CI run, so a future PR that adds so much
    as one `import reclaim.executor` anywhere in the AI layer fails immediately here,
    before it can matter."""
    py_files = sorted(_AI_PACKAGE_ROOT.rglob("*.py"))
    assert py_files, f"expected to find .py files under {_AI_PACKAGE_ROOT}, found none"

    violations: dict[str, set[str]] = {}
    forbidden = {"reclaim.executor", "send2trash"}
    for py_file in py_files:
        imported = _imported_module_names(py_file.read_text(encoding="utf-8"))
        hit = {name for name in imported if name in forbidden or name.startswith("send2trash.")}
        if hit:
            violations[str(py_file.relative_to(_AI_PACKAGE_ROOT.parent.parent.parent))] = hit

    assert violations == {}, (
        f"reclaim.ai must never import the auto-delete executor or send2trash: {violations}"
    )


# --- 2. Runtime: an AI cluster member cannot be smuggled into apply_batch ---------------------


def test_ai_cluster_member_fed_to_apply_batch_fails_loudly_before_any_disk_io(
    tmp_path: Path,
) -> None:
    """Even if a caller ignored every type hint and handed `apply_batch` an
    `AIClusterMember` instead of a `Candidate`, it must fail immediately (AttributeError,
    since `AIClusterMember` has none of `Candidate`'s required fields) — never silently
    proceed to move or delete anything. Asserts zero filesystem mutation as the real proof,
    not just that an exception type was raised."""
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"not a real jpeg, just fixture bytes")
    fake_ai_member = AIClusterMember(path=target, size_bytes=target.stat().st_size)

    safety = SafetyValidator(Config())
    with pytest.raises(AttributeError):
        apply_batch(
            [fake_ai_member],  # type: ignore[list-item] -- the whole point of this test
            safety=safety,
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
        )

    assert target.exists()
    assert target.read_bytes() == b"not a real jpeg, just fixture bytes"
    assert not (tmp_path / "manifest.jsonl").exists()
    assert not (tmp_path / "vault").exists()


# --- 3. Adversarial config: no config surface exists to route AI into Tier A ------------------


def test_malicious_config_cannot_inject_an_ai_category_section(tmp_path: Path) -> None:
    """The adversarial case named explicitly in the build brief: a config.toml that tries to
    define an AI-named category and enable it for auto-quarantine. `Config`'s pydantic
    models are all `extra="forbid"` — there is no field named anything AI-related anywhere
    in the schema, so this must be rejected at load time, not silently ignored or (worse)
    silently accepted into some catch-all bucket."""
    malicious_config = tmp_path / "config.toml"
    malicious_config.write_text(
        '[categories.ai_near_duplicate]\nenabled = true\nauto_quarantine = true\ntier = "A"\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception, match=r"ai_near_duplicate|extra"):
        load_config(malicious_config)


def test_malicious_config_cannot_add_an_ai_tier_field_to_an_existing_category(
    tmp_path: Path,
) -> None:
    """A subtler adversarial attempt: not a whole new section, but trying to smuggle an
    AI-related override into a real, existing category (e.g. "make dev_artifacts also cover
    my AI near-dup output"). Still rejected — `DevArtifactsConfig` has no such field."""
    malicious_config = tmp_path / "config.toml"
    malicious_config.write_text(
        '[categories.dev_artifacts]\nenabled = true\nai_source = "near_identical_image"\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception, match=r"ai_source|extra"):
        load_config(malicious_config)


# --- 4. Namespace separation: the deterministic engine never emits the AI namespace ------------


def test_ai_category_group_prefix_is_never_emitted_by_the_deterministic_detectors() -> None:
    """`AI_CATEGORY_GROUP_PREFIX` ("ai_") is reserved for the AI layer alone. Grepping
    reclaim.detectors' source (rather than running it against a fixture tree) proves the
    reservation holds for every category the deterministic engine could EVER emit, not just
    the ones a particular fixture tree happens to exercise."""
    detectors_source = (_AI_PACKAGE_ROOT.parent / "detectors.py").read_text(encoding="utf-8")
    assert f'"{AI_CATEGORY_GROUP_PREFIX}' not in detectors_source, (
        "reclaim.detectors.py must never emit a category_group starting with "
        f"{AI_CATEGORY_GROUP_PREFIX!r} — that namespace is reserved for the AI layer"
    )


# --- 5. AICluster's own construction-time invariant (unit-level, but part of the gate) --------


def test_browse_only_track_cannot_carry_a_deletion_suggestion() -> None:
    member = AIClusterMember(path=Path("photo.jpg"), size_bytes=100, is_recommended_keep=True)
    with pytest.raises(ValueError, match="browse/ranking-only"):
        AICluster(
            cluster_id="c1",
            track=AITrack.SEMANTIC_IMAGE,  # browse-only track
            members=(member,),
            raw_score=0.92,
            score_kind="cosine_similarity",
            rationale="test",
        )


def test_semantic_image_track_never_suggests_deletion_even_with_a_high_similarity() -> None:
    """Track B (ADR-0022): a near-perfect cosine similarity (0.99) alone must never flip
    suggests_deletion True -- SEMANTIC_IMAGE is browse-tidiness only, never a deletion
    suggestion, regardless of how visually/semantically close a group's members are."""
    members = (
        AIClusterMember(path=Path("beach1.jpg"), size_bytes=100),
        AIClusterMember(path=Path("beach2.jpg"), size_bytes=100),
    )
    cluster = AICluster(
        cluster_id="semantic-1",
        track=AITrack.SEMANTIC_IMAGE,
        members=members,
        raw_score=0.01,  # a max_pairwise_distance of 0.01 -- i.e. 0.99 cosine similarity
        score_kind="max_pairwise_cosine_distance",
        rationale="test",
    )
    assert cluster.suggests_deletion is False


def test_near_identical_track_with_a_keeper_does_suggest_deletion() -> None:
    keep = AIClusterMember(path=Path("a.jpg"), size_bytes=100, is_recommended_keep=True)
    drop = AIClusterMember(path=Path("b.jpg"), size_bytes=100, is_recommended_keep=False)
    cluster = AICluster(
        cluster_id="c1",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(keep, drop),
        raw_score=2.0,
        score_kind="hamming_distance",
        rationale="test",
    )
    assert cluster.suggests_deletion is True


def test_near_identical_track_without_a_keeper_is_browse_only_not_a_suggestion() -> None:
    """A near-identical cluster the keep-best scorer hasn't scored yet must not be treated
    as a deletion suggestion just because its track is deletion-eligible."""
    unscored_a = AIClusterMember(path=Path("a.jpg"), size_bytes=100)
    unscored_b = AIClusterMember(path=Path("b.jpg"), size_bytes=100)
    cluster = AICluster(
        cluster_id="c1",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(unscored_a, unscored_b),
        raw_score=2.0,
        score_kind="hamming_distance",
        rationale="test",
    )
    assert cluster.suggests_deletion is False


def test_near_dup_document_track_with_a_keeper_does_suggest_deletion() -> None:
    """Feature 1b: NEAR_DUP_DOCUMENT joined the deletion-eligible set (spec §2: "deletion
    suggestions only for high-similarity near-dups") — same mechanism as near-identical
    images, proven the same way."""
    keep = AIClusterMember(path=Path("report_v2.docx"), size_bytes=100, is_recommended_keep=True)
    drop = AIClusterMember(path=Path("report_v2_copy.docx"), size_bytes=100)
    cluster = AICluster(
        cluster_id="doc-1",
        track=AITrack.NEAR_DUP_DOCUMENT,
        members=(keep, drop),
        raw_score=0.05,
        score_kind="minhash_jaccard_distance",
        rationale="test",
    )
    assert cluster.suggests_deletion is True


def test_version_chain_track_with_a_keeper_does_suggest_deletion() -> None:
    """Feature 1b: VERSION_CHAIN joined the deletion-eligible set (spec §2: "recommend keeping
    the latest, surface the older ones for review") — the latest version is the
    `is_recommended_keep` member, older versions carry a `position` but no keep flag."""
    v1 = AIClusterMember(path=Path("draft_v1.docx"), size_bytes=90, position=0)
    v2 = AIClusterMember(path=Path("draft_v2.docx"), size_bytes=95, position=1)
    final = AIClusterMember(
        path=Path("draft_final.docx"),
        size_bytes=100,
        is_recommended_keep=True,
        position=2,
    )
    chain = AICluster(
        cluster_id="chain-1",
        track=AITrack.VERSION_CHAIN,
        members=(v1, v2, final),
        raw_score=0.88,
        score_kind="content_similarity",
        rationale="test",
    )
    assert chain.suggests_deletion is True


def test_screenshot_burst_track_with_a_keeper_does_suggest_deletion() -> None:
    """Feature 2: SCREENSHOT_BURST joined the deletion-eligible set, but ONLY conditionally
    (screenshot_review.py's orchestration only ever sets a keeper when every member's OCR
    content tag is transient-UI) -- at the AICluster level tested here, the track itself is
    deletion-eligible the same mechanical way NEAR_IDENTICAL_IMAGE/NEAR_DUP_DOCUMENT/
    VERSION_CHAIN are; the content-tag gating is tested separately in
    tests/test_ai_screenshot_review.py."""
    keep = AIClusterMember(path=Path("shot2.png"), size_bytes=100, is_recommended_keep=True)
    drop = AIClusterMember(path=Path("shot1.png"), size_bytes=100)
    cluster = AICluster(
        cluster_id="burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=(keep, drop),
        raw_score=2.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test",
    )
    assert cluster.suggests_deletion is True


def test_screenshot_burst_track_without_a_keeper_is_browse_only_not_a_suggestion() -> None:
    """The mixed-content-tag case: a burst track cluster with no member marked as keeper
    (screenshot_review.py's conditional gate withheld it) must not be treated as a deletion
    suggestion just because the track itself is deletion-eligible."""
    unscored_a = AIClusterMember(path=Path("shot1.png"), size_bytes=100)
    unscored_b = AIClusterMember(path=Path("shot2.png"), size_bytes=100)
    cluster = AICluster(
        cluster_id="burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=(unscored_a, unscored_b),
        raw_score=2.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test",
    )
    assert cluster.suggests_deletion is False


def test_review_queue_partitions_deletion_suggestions_from_browse_only() -> None:
    keep = AIClusterMember(path=Path("a.jpg"), size_bytes=100, is_recommended_keep=True)
    drop = AIClusterMember(path=Path("b.jpg"), size_bytes=100)
    suggestion = AICluster(
        cluster_id="near-dup-1",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(keep, drop),
        raw_score=1.0,
        score_kind="hamming_distance",
        rationale="near-identical",
    )
    browse = AICluster(
        cluster_id="semantic-1",
        track=AITrack.SEMANTIC_IMAGE,
        members=(AIClusterMember(path=Path("c.jpg"), size_bytes=100),),
        raw_score=0.9,
        score_kind="cosine_similarity",
        rationale="same scene",
    )

    queue = AIReviewQueue()
    queue.add(suggestion)
    queue.add(browse)

    assert [c.cluster_id for c in queue.deletion_suggestions()] == ["near-dup-1"]
    assert [c.cluster_id for c in queue.browse_only()] == ["semantic-1"]


def test_review_queue_partitions_all_five_tracks_correctly() -> None:
    """Feature 1b's two deletion-eligible tracks (NEAR_DUP_DOCUMENT, VERSION_CHAIN) and
    Feature 2's SCREENSHOT_BURST all land in the same deletion_suggestions() view as
    NEAR_IDENTICAL_IMAGE, and a browse-only track stays out of it — proven at the
    AIReviewQueue level, not just AICluster.suggests_deletion in isolation, since the queue's
    partitioning is what a future UI would actually read."""
    image_dup = AICluster(
        cluster_id="image-1",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(
            AIClusterMember(path=Path("a.jpg"), size_bytes=100, is_recommended_keep=True),
            AIClusterMember(path=Path("b.jpg"), size_bytes=100),
        ),
        raw_score=1.0,
        score_kind="hamming_distance",
        rationale="near-identical",
    )
    document_dup = AICluster(
        cluster_id="document-1",
        track=AITrack.NEAR_DUP_DOCUMENT,
        members=(
            AIClusterMember(path=Path("r_v1.docx"), size_bytes=100, is_recommended_keep=True),
            AIClusterMember(path=Path("r_v2.docx"), size_bytes=100),
        ),
        raw_score=0.95,
        score_kind="min_pairwise_cosine_similarity_within_cluster",
        rationale="near-dup document",
    )
    chain = AICluster(
        cluster_id="chain-1",
        track=AITrack.VERSION_CHAIN,
        members=(
            AIClusterMember(path=Path("d_v1.docx"), size_bytes=90, position=0),
            AIClusterMember(
                path=Path("d_final.docx"), size_bytes=100, is_recommended_keep=True, position=1
            ),
        ),
        raw_score=0.9,
        score_kind="min_pairwise_content_similarity_within_chain",
        rationale="version chain",
    )
    burst = AICluster(
        cluster_id="burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=(
            AIClusterMember(path=Path("s1.png"), size_bytes=100),
            AIClusterMember(path=Path("s2.png"), size_bytes=100, is_recommended_keep=True),
        ),
        raw_score=2.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="screenshot burst",
    )
    browse = AICluster(
        cluster_id="semantic-1",
        track=AITrack.SEMANTIC_IMAGE,
        members=(AIClusterMember(path=Path("c.jpg"), size_bytes=100),),
        raw_score=0.9,
        score_kind="cosine_similarity",
        rationale="same scene",
    )

    queue = AIReviewQueue()
    for cluster in (image_dup, document_dup, chain, burst, browse):
        queue.add(cluster)

    assert {c.cluster_id for c in queue.deletion_suggestions()} == {
        "image-1",
        "document-1",
        "chain-1",
        "burst-1",
    }
    assert [c.cluster_id for c in queue.browse_only()] == ["semantic-1"]


# --- 6. Full pipeline sanity: apply_batch's real call sites never touch reclaim.ai -------------


def test_cli_and_api_service_never_import_reclaim_ai_today() -> None:
    """Today, the AI layer has no dashboard/CLI wiring at all — the strongest possible
    safety property at this stage of the build (spec §8 build order: harness + safety eval
    land before any feature, let alone any UI surface). If/when a future feature adds
    READ-ONLY AI-review wiring to the dashboard, this test must be narrowed (not deleted) to
    assert the wiring never flows into `apply_batch`'s `selected`/`candidates` argument —
    see this test's docstring at that point for the updated guarantee it should assert.
    """
    for source_file in (
        _AI_PACKAGE_ROOT.parent / "cli.py",
        _AI_PACKAGE_ROOT.parent / "api" / "service.py",
        _AI_PACKAGE_ROOT.parent / "api" / "routes.py",
    ):
        imported = _imported_module_names(source_file.read_text(encoding="utf-8"))
        hit = {name for name in imported if name == "reclaim.ai" or name.startswith("reclaim.ai.")}
        assert hit == set(), f"{source_file.name} must not import reclaim.ai yet: {hit}"


def test_apply_batch_signature_has_no_ai_layer_parameter() -> None:
    """`apply_batch`/`Candidate` accept no AI-related parameter or field to smuggle data
    through — a static confirmation alongside the AttributeError proof in test 2 above."""
    import inspect

    from reclaim.executor import apply_batch as apply_batch_fn

    params = inspect.signature(apply_batch_fn).parameters
    assert "ai_review_queue" not in params
    assert "ai_clusters" not in params
    candidate_fields = {f for f in Candidate.__dataclass_fields__}
    assert not any(f.startswith("ai_") for f in candidate_fields)


def test_safety_invariant_error_is_still_importable_for_ai_layer_defense_in_depth() -> None:
    """Sanity check that the executor's own defense-in-depth exception type is stable and
    importable — a future feature wiring AI-derived candidates through the deterministic
    pipeline (which would be a deliberate, reviewed architecture change, not this layer)
    would still hit this same BLOCKED-candidate refusal. Not a meaningful assertion on its
    own; documents the fallback that would still exist if the boundary above were ever
    breached by mistake."""
    assert issubclass(SafetyInvariantError, RuntimeError)
    assert Verdict.BLOCKED is not None
