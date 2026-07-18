from __future__ import annotations

import html
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import RequestResponseEndpoint

from reclaim.ai.labeling import (
    KEEP_REASON_OPTIONS,
    LabelCandidate,
    LabelingProgress,
    LabelStore,
    compute_progress,
    record_decision,
)
from reclaim.api.security import (
    LocalOriginPolicy,
    generate_csrf_token,
    local_origin_violation,
)

# Loopback-only local review UI for gold-set labeling — reuses `reclaim.api.security`
# wholesale (Host/Origin DNS-rebinding guard + per-session CSRF token) rather than
# reimplementing it; this tool writes real personal file paths to a local label store and,
# while it never deletes or moves anything, deserves the same local-API hardening the main
# dashboard has, not a lesser standard because it's "just a dev tool." Importing
# reclaim.api.security here is safe: that module has no dependency on reclaim.executor (see
# evals/test_ai_safety_gate.py's import-graph scan, which covers this file too — it lives
# under src/reclaim/ai/ like everything else in the AI layer).


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    decision: str
    keep_path: str | None = None
    keep_reasons: list[str] = []


def create_labeling_app(
    candidates: list[LabelCandidate], *, label_store_path: Path, host: str, port: int
) -> FastAPI:
    app = FastAPI(title="Reclaim — Gold-Set Labeling", version="0.1.0")
    store = LabelStore(label_store_path)
    csrf_token = generate_csrf_token()
    # `local_origin_violation` (reclaim.api.security) reads the token via
    # `request.app.state.reclaim.csrf_token` — matching that exact shape here (a minimal
    # namespace, not the full dashboard `AppState`, which carries many fields that make no
    # sense for this tool) is what lets this app reuse that already-audited module unmodified
    # rather than forking it.
    app.state.reclaim = SimpleNamespace(csrf_token=csrf_token)
    policy = LocalOriginPolicy(host=host, port=port)
    candidates_by_id = {candidate.cluster.cluster_id: candidate for candidate in candidates}

    @app.middleware("http")
    async def _local_origin_guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        violation = local_origin_violation(request, policy)
        if violation is not None:
            return JSONResponse(status_code=403, content={"detail": violation})
        return await call_next(request)

    @app.get("/image/{cluster_id}/{member_index}")
    def get_image(cluster_id: str, member_index: int) -> FileResponse:
        # Closed allowlist, not a general path parameter: only a path that is literally one
        # of THIS run's candidate cluster members can ever be served — there is no way to
        # request an arbitrary local file via this route, unlike a naive "serve this path"
        # endpoint would allow.
        candidate = candidates_by_id.get(cluster_id)
        if candidate is None or not (0 <= member_index < len(candidate.cluster.members)):
            raise HTTPException(status_code=404, detail="unknown cluster or member index")
        path = candidate.cluster.members[member_index].path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="file no longer exists on disk")
        return FileResponse(path)

    @app.post("/api/label")
    def post_label(payload: LabelRequest) -> dict[str, bool]:
        valid_decisions = ("confirmed_near_duplicates", "rejected_not_duplicates", "skipped")
        if payload.decision not in valid_decisions:
            raise HTTPException(status_code=400, detail=f"invalid decision: {payload.decision!r}")
        candidate = candidates_by_id.get(payload.cluster_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="unknown cluster_id")
        member_paths = {member.path.as_posix() for member in candidate.cluster.members}
        if payload.keep_path is not None and payload.keep_path not in member_paths:
            raise HTTPException(status_code=400, detail="keep_path is not a member of this cluster")
        invalid_reasons = set(payload.keep_reasons) - set(KEEP_REASON_OPTIONS)
        if invalid_reasons:
            raise HTTPException(status_code=400, detail=f"invalid keep_reasons: {invalid_reasons}")
        record_decision(
            store,
            candidate,
            decision=payload.decision,  # type: ignore[arg-type]
            keep_path=payload.keep_path,
            keep_reasons=tuple(payload.keep_reasons),
        )
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        already_labeled = store.labeled_cluster_ids()
        pending = [c for c in candidates if c.cluster.cluster_id not in already_labeled]
        progress = compute_progress(store)
        return HTMLResponse(
            _render_page(pending, csrf_token, total=len(candidates), progress=progress)
        )

    return app


def _render_progress_summary(progress: LabelingProgress) -> str:
    rows = "".join(
        f"<li>{stratum}: {progress.counts_by_stratum.get(stratum, 0)} "
        f"(target &ge; {progress.target_per_stratum_minimum})</li>"
        for stratum in ("near_duplicate", "boundary", "negative_control")
    )
    status = "✅ targets met" if progress.meets_targets else "targets not yet met"
    return f"""<div class="rc-progress">
      <p><strong>Progress:</strong> {progress.total_labeled} / {progress.target_total} total
      labeled — {status}</p>
      <ul>{rows}</ul>
      <p class="rc-status">A gold set of mostly confirmed positives can't locate a decision
      threshold — the boundary/negative_control minimums above matter as much as the total.</p>
    </div>"""


def _render_page(
    pending: list[LabelCandidate], csrf_token: str, *, total: int, progress: LabelingProgress
) -> str:
    cards = "\n".join(_render_cluster_card(candidate) for candidate in pending)
    remaining = len(pending)
    labeled = total - remaining
    empty_state = "<p>All candidate clusters have been labeled. Nothing pending.</p>"
    progress_html = _render_progress_summary(progress)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Reclaim — Gold-Set Labeling</title>
<style>
body {{
  font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee;
}}
.rc-cluster {{
  border: 1px solid #444; border-radius: 8px; padding: 1rem; margin-bottom: 1.5rem;
}}
.rc-cluster[data-stratum="boundary"] {{ border-left: 4px solid #e0a030; }}
.rc-cluster[data-stratum="negative_control"] {{ border-left: 4px solid #c0392b; }}
.rc-cluster[data-stratum="near_duplicate"] {{ border-left: 4px solid #4caf50; }}
.rc-members {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
.rc-member {{ text-align: center; }}
.rc-member img {{
  max-width: 220px; max-height: 220px; border: 2px solid #333; border-radius: 4px;
}}
.rc-member.selected img {{ border-color: #4caf50; }}
button {{ margin: 0.25rem; padding: 0.4rem 0.8rem; cursor: pointer; }}
.rc-status {{ font-size: 0.9rem; color: #9c9; }}
.rc-progress {{ border: 1px solid #333; border-radius: 8px; padding: 1rem; margin-bottom: 2rem; }}
.rc-reasons {{ margin: 0.5rem 0; }}
.rc-reason {{ margin-right: 1rem; }}
</style></head>
<body>
<h1>Gold-Set Labeling — {remaining} pending, {labeled} labeled, {total} total</h1>
<p>Nothing here leaves this machine. Labels are written to a local file only.</p>
{progress_html}
{cards if cards else empty_state}
<script>
// Filenames/paths are attacker-controllable input (this tool's whole job is walking a
// real disk — a file literally named `');alert(1);//` is real, reachable input) and are
// carried exclusively via data-* attributes (HTML-attribute-escaped, never
// re-interpreted as code) — never via an inline onclick="...('...')" handler, where
// HTML-escaping a quote character does NOT protect the embedded JS string literal (the
// browser HTML-decodes the attribute value before parsing it as JS, so an escaped quote
// reappears as a literal one). Every click is wired here via delegation instead, reading
// .dataset for its arguments.
const CSRF_TOKEN = {csrf_token!r};
let selectedKeep = {{}};
let selectedReasons = {{}};

function selectKeep(clusterId, memberIndex, path) {{
  selectedKeep[clusterId] = path;
  const selector = `[data-cluster="${{CSS.escape(clusterId)}}"] .rc-member`;
  document.querySelectorAll(selector).forEach((el, i) => {{
    el.classList.toggle("selected", i === memberIndex);
  }});
}}

function toggleReason(clusterId, reason, checked) {{
  const set = selectedReasons[clusterId] || new Set();
  if (checked) {{ set.add(reason); }} else {{ set.delete(reason); }}
  selectedReasons[clusterId] = set;
}}

async function submitLabel(clusterId, decision) {{
  const isConfirm = decision === "confirmed_near_duplicates";
  const keepPath = isConfirm ? (selectedKeep[clusterId] || null) : null;
  const reasons = isConfirm ? Array.from(selectedReasons[clusterId] || []) : [];
  const response = await fetch("/api/label", {{
    method: "POST",
    headers: {{
      "Content-Type": "application/json",
      "X-Reclaim-CSRF-Token": CSRF_TOKEN,
    }},
    body: JSON.stringify({{
      cluster_id: clusterId, decision: decision, keep_path: keepPath, keep_reasons: reasons,
    }}),
  }});
  if (response.ok) {{
    const selector = `[data-cluster="${{CSS.escape(clusterId)}}"]`;
    document.querySelector(selector).remove();
  }} else {{
    alert("Failed to save label: " + (await response.text()));
  }}
}}

document.addEventListener("click", (event) => {{
  const memberEl = event.target.closest('[data-role="select-keep"]');
  if (memberEl) {{
    selectKeep(
      memberEl.dataset.cluster, Number(memberEl.dataset.index), memberEl.dataset.path
    );
    return;
  }}
  const buttonEl = event.target.closest('[data-role="submit-label"]');
  if (buttonEl) {{
    submitLabel(buttonEl.dataset.cluster, buttonEl.dataset.decision);
  }}
}});

document.addEventListener("change", (event) => {{
  const reasonEl = event.target.closest('[data-role="reason-checkbox"]');
  if (reasonEl) {{
    toggleReason(reasonEl.dataset.cluster, reasonEl.dataset.reason, reasonEl.checked);
  }}
}});
</script>
</body></html>"""


def _render_member(cluster_id_attr: str, index: int, path: Path, size_bytes: int) -> str:
    path_attr = html.escape(path.as_posix(), quote=True)
    name = html.escape(path.name)
    return (
        f'<div class="rc-member" data-role="select-keep" data-cluster="{cluster_id_attr}" '
        f'data-index="{index}" data-path="{path_attr}">\n'
        f'  <img src="/image/{cluster_id_attr}/{index}" alt="candidate image" loading="lazy">\n'
        f"  <div>{name}</div>\n"
        f'  <div class="rc-status">{size_bytes:,} bytes</div>\n'
        f"</div>"
    )


def _render_reason_checkboxes(cluster_id_attr: str) -> str:
    boxes = "\n".join(
        f'<label class="rc-reason"><input type="checkbox" data-role="reason-checkbox" '
        f'data-cluster="{cluster_id_attr}" data-reason="{html.escape(reason, quote=True)}"> '
        f"{html.escape(reason)}</label>"
        for reason in KEEP_REASON_OPTIONS
    )
    return f'<div class="rc-reasons">Why is the selected image better? {boxes}</div>'


def _render_cluster_card(candidate: LabelCandidate) -> str:
    cluster = candidate.cluster
    cluster_id_attr = html.escape(cluster.cluster_id, quote=True)
    stratum_attr = html.escape(candidate.stratum, quote=True)
    members_html = "\n".join(
        _render_member(cluster_id_attr, i, member.path, member.size_bytes)
        for i, member in enumerate(cluster.members)
    )
    rationale = html.escape(cluster.rationale)
    score_kind = html.escape(cluster.score_kind)
    reason_checkboxes = _render_reason_checkboxes(cluster_id_attr)
    header = f"<strong>[{stratum_attr}]</strong> {rationale} — raw score: {cluster.raw_score:.1f}"
    return f"""<div class="rc-cluster" data-cluster="{cluster_id_attr}"
         data-stratum="{stratum_attr}">
      <p>{header} ({score_kind})</p>
      <p>Click an image below to mark it as the one to KEEP, then confirm.</p>
      <div class="rc-members">{members_html}</div>
      {reason_checkboxes}
      <button data-role="submit-label" data-cluster="{cluster_id_attr}"
              data-decision="confirmed_near_duplicates">Confirm — these are near-duplicates</button>
      <button data-role="submit-label" data-cluster="{cluster_id_attr}"
              data-decision="rejected_not_duplicates">Reject — not duplicates</button>
      <button data-role="submit-label" data-cluster="{cluster_id_attr}"
              data-decision="skipped">Skip for now</button>
    </div>"""
