from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

# Synthetic, deterministic (SEED-driven, house rule 40) checked-in fixture GENERATOR for
# Feature 1b's CI eval (spec §7.1: "CI fixtures (checked in, synthetic, deterministic)").
# Mirrors build_image_similarity_fixtures.py's role for Feature 1a exactly — the checked-in
# artifact is this generator, not committed text files, so re-running it always reproduces the
# same tree + ground truth.

_SEED = 42

_TOPIC_SENTENCES: tuple[tuple[str, ...], ...] = (
    (
        "The quarterly financial report shows steady revenue growth across all regions.",
        "Cloud services revenue increased by twelve percent compared to the prior quarter.",
        "Operating expenses remained flat while gross margin improved slightly.",
        "The board approved a new capital allocation plan for the coming fiscal year.",
        "Management expects continued momentum in the enterprise software segment.",
        "Customer retention rates held steady above ninety percent for the third year.",
    ),
    (
        "The hiking trail winds through dense forest before reaching the summit ridge.",
        "Early morning fog often blankets the lower valley until mid-morning.",
        "Hikers should carry sufficient water for the six-hour round trip.",
        "The final ascent involves a steep rocky section requiring careful footing.",
        "Wildlife sightings along this route commonly include deer and wild turkeys.",
        "The summit offers panoramic views of three neighboring mountain ranges.",
    ),
    (
        "The recipe calls for two cups of flour and a teaspoon of baking soda.",
        "Cream the butter and sugar together until the mixture turns pale and fluffy.",
        "Fold in the chocolate chips gently to avoid overworking the dough.",
        "Bake at three hundred fifty degrees for approximately twelve minutes.",
        "Allow the cookies to cool on the tray before transferring to a wire rack.",
        "Store in an airtight container to keep them fresh for up to a week.",
    ),
)

_DISTRACTOR_TOPICS: tuple[str, ...] = (
    "A brief history of maritime navigation techniques used before satellite positioning.",
    "An overview of common woodworking joints used in furniture construction.",
    "A summary of orchestral instrument families and their typical roles in a symphony.",
    "An explanation of basic soil chemistry relevant to home vegetable gardening.",
    "A description of traditional glassblowing techniques used in small workshops.",
)


@dataclass(frozen=True, slots=True)
class DocumentFixtureCase:
    id: str
    relative_path: str
    true_cluster_id: str
    is_best_quality: bool  # largest/most-recently-modified member — the expected keeper


def _mild_edit(sentences: list[str], rng: random.Random) -> list[str]:
    """A light touch: append one sentence, unchanged otherwise — the "barely edited re-save"
    case, expected to remain trivially near-dup under any reasonable MinHash threshold."""
    edited = list(sentences)
    edited.append("This section was added in a later revision for additional clarity.")
    return edited


def _moderate_edit(sentences: list[str], rng: random.Random) -> list[str]:
    """A more substantial edit: reorder two sentences, append two new ones — the "meaningfully
    revised but still the same document" case. Deliberately net-LARGER than `_mild_edit`'s
    single appended sentence (not net-shorter — an earlier version of this fixture dropped a
    sentence here, which made this variant SMALLER than the mild one and silently broke the
    "moderate = the expected keeper" ground truth, since document_keep_best.select_document_keep
    picks by size first: an ambiguous fixture doesn't test what it claims to)."""
    edited = list(sentences)
    if len(edited) > 1:
        i, j = rng.sample(range(len(edited)), 2)
        edited[i], edited[j] = edited[j], edited[i]
    edited.append("A new closing paragraph was written for this revision.")
    edited.append("The revision also incorporates feedback gathered from the review cycle.")
    return edited


def build_document_similarity_fixtures(
    root: Path, *, n_distractors: int = 5
) -> list[DocumentFixtureCase]:
    """Materializes a synthetic document tree under `root` with known ground truth. Each of
    the 3 hardcoded topics gets one base document, one mildly-edited variant, and one
    moderately-edited variant — deliberately not parameterized by `n_clusters` the way the
    image fixture is, since hand-authoring realistic topic sentences doesn't scale the same
    way procedural pixel patterns do; 3 topics x 3 variants already exercises every code path
    (clustering, keep-best, safety filtering) an eval needs. `n_distractors` remains
    adjustable since distractor topics are cheap to add.
    """
    cases: list[DocumentFixtureCase] = []
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic synthetic fixture data, not security

    for topic_index, topic_sentences in enumerate(_TOPIC_SENTENCES):
        cluster_id = f"topic_{topic_index:02d}"
        cluster_dir = root / cluster_id

        base_path = cluster_dir / "base.txt"
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_text(" ".join(topic_sentences), encoding="utf-8")
        cases.append(
            DocumentFixtureCase(
                f"{cluster_id}_base", str(base_path.relative_to(root)), cluster_id, False
            )
        )

        mild_path = cluster_dir / "base_v2.txt"
        mild_path.write_text(" ".join(_mild_edit(list(topic_sentences), rng)), encoding="utf-8")
        cases.append(
            DocumentFixtureCase(
                f"{cluster_id}_mild", str(mild_path.relative_to(root)), cluster_id, False
            )
        )

        moderate_path = cluster_dir / "base_final.txt"
        moderate_text = " ".join(_moderate_edit(list(topic_sentences), rng))
        moderate_path.write_text(moderate_text, encoding="utf-8")
        cases.append(
            DocumentFixtureCase(
                f"{cluster_id}_moderate", str(moderate_path.relative_to(root)), cluster_id, True
            )
        )

    distractor_dir = root / "distractors"
    for distractor_index, text in enumerate(_DISTRACTOR_TOPICS[:n_distractors]):
        distractor_id = f"distractor_{distractor_index:02d}"
        path = distractor_dir / f"{distractor_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        cases.append(
            DocumentFixtureCase(distractor_id, str(path.relative_to(root)), distractor_id, False)
        )

    return cases
