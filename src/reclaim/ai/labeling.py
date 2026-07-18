from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reclaim.ai.image_similarity import build_near_identical_clusters
from reclaim.ai.models import AICluster
from reclaim.safety import SafetyValidator

# Gold-set labeling tool (spec §7.1 / the explicit autonomy-boundary instruction: "build a
# gold-set labeling tool ... so GG can label a few hundred real image/doc pairs + keep-best
# choices from his own disk. Do NOT fabricate a gold set"). This module is the tool's
# non-UI core: candidate discovery (reusing the real Feature 1a pipeline, not a separate
# reimplementation) and the append-only local label store. `labeling_app.py` wraps this in a
# loopback-only FastAPI review UI; `scripts/ai_label_tool.py` is the CLI launcher.
#
# Nothing here has been run against a real gold set — this delivers the tool, not labels.

LabelDecisionKind = Literal["confirmed_near_duplicates", "rejected_not_duplicates", "skipped"]


@dataclass(frozen=True, slots=True)
class LabelDecision:
    """One human labeling decision for one candidate cluster — the ground truth Feature 1a's
    operating point will eventually be selected from (ADR-0012's PROVISIONAL threshold stops
    being provisional once enough of these exist and a new ADR records the real PR curve)."""

    cluster_id: str
    decision: LabelDecisionKind
    # Which member GG identifies as the one to keep — only meaningful for
    # "confirmed_near_duplicates"; None for a rejected or skipped cluster.
    keep_path: str | None
    member_paths: tuple[str, ...]
    labeled_at: float


def discover_label_candidates(
    root: Path, *, safety: SafetyValidator, max_hamming_distance: int = 15
) -> list[AICluster]:
    """Proposes candidate clusters for GG to review — reuses the exact Feature 1a pipeline
    (`image_similarity.build_near_identical_clusters`), not a separate implementation, so
    what GG labels is genuinely what the shipped feature would propose. `max_hamming_distance`
    defaults looser than ADR-0012's CI gate (10) — deliberately: a labeling tool should show
    borderline/over-inclusive candidates for GG to REJECT (informative negative labels), not
    only the cases the current threshold already accepts confidently.
    """
    image_paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp")
    ]
    return build_near_identical_clusters(
        image_paths, safety=safety, max_hamming_distance=max_hamming_distance
    )


class LabelStore:
    """Append-only JSONL label log — same event-log pattern as
    `executor.QuarantineManifestEntry` (append, never rewrite in place). Lives wherever the
    caller points it; `scripts/ai_label_tool.py` defaults to `data/ai_labels/gold_labels.jsonl`,
    which `.gitignore` excludes — these paths are real, personal filesystem paths from GG's
    own disk and must never be committed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, decision: LabelDecision) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "cluster_id": decision.cluster_id,
                        "decision": decision.decision,
                        "keep_path": decision.keep_path,
                        "member_paths": list(decision.member_paths),
                        "labeled_at": decision.labeled_at,
                    }
                )
            )
            fh.write("\n")

    def read_all(self) -> list[LabelDecision]:
        if not self._path.exists():
            return []
        decisions: list[LabelDecision] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                decisions.append(
                    LabelDecision(
                        cluster_id=data["cluster_id"],
                        decision=data["decision"],
                        keep_path=data["keep_path"],
                        member_paths=tuple(data["member_paths"]),
                        labeled_at=data["labeled_at"],
                    )
                )
        return decisions

    def labeled_cluster_ids(self) -> set[str]:
        """Folds to the LATEST decision per cluster_id (same "last line wins" event-log fold
        the deterministic engine's manifest reader uses) — re-launching the tool skips
        already-labeled clusters rather than re-asking, but a cluster can still be
        re-labeled by deliberately labeling it again (the new decision simply appends)."""
        return {decision.cluster_id for decision in self.read_all()}


def record_decision(
    store: LabelStore,
    cluster: AICluster,
    *,
    decision: LabelDecisionKind,
    keep_path: str | None,
    now: float | None = None,
) -> None:
    store.append(
        LabelDecision(
            cluster_id=cluster.cluster_id,
            decision=decision,
            keep_path=keep_path,
            member_paths=tuple(member.path.as_posix() for member in cluster.members),
            labeled_at=now if now is not None else time.time(),
        )
    )
