# 0013. Gold-set labeling tool: architecture, and why it delivers a tool, not labels

## Context

The build brief's explicit autonomy boundary requires a gold-set labeling tool — local,
privacy-safe — so GG can label a few hundred real image near-dup clusters + keep-best choices
from his own disk, and forbids two things: fabricating a gold set, and blocking this build on
GG actually doing the labeling. ADR-0012's Hamming-distance threshold and the keep-best
scorer's weights are provisional specifically because no such real-world ground truth exists
yet; this tool is how it will.

## Decision

**Reuses the real Feature 1a pipeline for candidate discovery — not a separate
implementation.** `reclaim.ai.labeling.discover_label_candidates` calls
`image_similarity.build_near_identical_clusters` directly, at a looser default Hamming
distance (15, vs. ADR-0012's CI gate of 10) — deliberately over-inclusive, so GG reviews and
rejects borderline cases (informative negative labels) rather than only ever seeing clusters
the current threshold already accepts. What GG labels is genuinely what the shipped pipeline
would propose, not a hand-curated subset.

**Local-API hardening reused wholesale, not re-implemented at a lesser standard.**
`reclaim.ai.labeling_app` imports `reclaim.api.security` (`LocalOriginPolicy`,
`local_origin_violation`, `generate_csrf_token`) directly — the same Host/Origin DNS-rebinding
guard and per-session CSRF token the main dashboard uses. This tool never deletes or moves a
file, but it does write real personal file paths to a local label store, and "it's just a dev
tool" was rejected as a reason to hold it to a lesser security bar than the product surface it
feeds. `reclaim.api.security` has no dependency on `reclaim.executor`, so importing it from
`reclaim.ai` doesn't cross the Gate-2 boundary — confirmed by `evals/test_ai_safety_gate.py`'s
import-graph scan, which covers this file along with every other file under `src/reclaim/ai/`.

**Closed-allowlist image serving, not a general file-path parameter.** `GET
/image/{cluster_id}/{member_index}` only ever serves a path that is literally a member of
*this run's* candidate set — there is no query parameter or path segment that names an
arbitrary local file, unlike a naive "serve this path" endpoint would allow. Verified both in
`tests/test_ai_labeling_app.py` (unknown cluster/out-of-range index → 404; a traversal-style
URL → 404/422, never a served file) and live, in a real browser, against a path-traversal
attempt.

**No inline event handlers with interpolated data — data-attributes + delegated listeners
only.** An earlier draft of `labeling_app.py`'s HTML template used
`onclick="selectKeep('...', i, '...')"` with `html.escape()`-wrapped filenames interpolated
directly into the JS string literal — the exact double-context injection class already fixed
once this session in the main dashboard (`app.js::renderClusterTable`): HTML-escaping a quote
character does not protect a JS string literal inside an inline event-handler attribute,
because the browser HTML-decodes the attribute value *before* parsing it as JavaScript, so an
escaped quote reappears as a literal one and breaks out of the string. Caught and fixed before
any test was written against it — every filename/path now travels exclusively through
`data-*` attributes (safe: read via `.dataset`, never re-interpreted as code), with a single
delegated `document.addEventListener("click", ...)` handler reading those attributes. The fix
is documented inline in the template's own `<script>` comment so it can't be silently
reintroduced by a future edit that doesn't know the history.

**Plain server-rendered HTML + a page reload per batch of decisions, not a SPA.** Deliberately
simpler than the main dashboard: this is a manually-invoked, single-user, short-lived review
session, not a persistent product surface — a full fetch-based SPA with live DOM patching
would be over-engineering for a tool run once per labeling session. Each label submission does
use `fetch()` (needed to attach the CSRF header, which a plain HTML `<form>` POST cannot set),
removes the labeled card from the DOM immediately, and a page reload re-derives the
authoritative pending/labeled counts from the label store — verified live: label a cluster,
reload, confirm the count and the pending list both update correctly.

## Consequences

- This tool has NOT been run against GG's real photos as part of this build. It was verified
  against synthetic fixtures only (`evals/ai_fixtures/build_image_similarity_fixtures.py`,
  reused to generate a throwaway smoke-test directory) — both via automated tests
  (`tests/test_ai_labeling.py`, `tests/test_ai_labeling_app.py`) and a live, real-browser
  session (navigate → view clusters → select a keeper → confirm → reload and verify
  persistence → reject a different cluster → confirm the empty state → verify the Host/CSRF
  guards reject a live spoofed request). Real gold-set labeling is an explicit, separate
  follow-up requiring GG to run `scripts/ai_label_tool.py` against a real directory himself.
- `data/ai_labels/` is gitignored — labels contain real, personal file paths and must never be
  committed.
- Once GG has labeled enough clusters, the real PR curve over that gold set (using the exact
  same `eval_harness.precision_recall_curve`/`select_operating_point` functions ADR-0012 already
  exercises on synthetic data) becomes the input to a follow-up ADR that can finally drop the
  word "provisional."

## Alternatives considered

- **A CLI-only (terminal image preview) labeling flow.** Rejected: visual quality comparison
  (is this one blurrier? which crop looks better?) genuinely benefits from real image
  rendering, which a terminal can't provide without external viewer integration that would be
  more complex than a minimal local web page.
- **Full SPA matching the main dashboard's architecture.** Rejected as over-engineering for a
  manually-invoked, single-user, short-lived tool — see Decision above.
- **Skip the Host/CSRF guard since "nothing here deletes files."** Rejected — the tool writes
  real personal file paths to disk from whatever page happens to be able to reach it locally;
  reusing already-audited code costs almost nothing and there was no reason to accept a lower
  bar.

## Test coverage

`tests/test_ai_labeling.py` (6 cases: LabelStore round-trip, append-only fold, candidate
discovery reusing the real pipeline with safety filtering). `tests/test_ai_labeling_app.py`
(10 cases: index rendering, closed-allowlist image serving including a traversal attempt,
label validation, CSRF/Host guard rejection). Live browser verification (this session,
chrome-devtools): full click-through-to-persistence flow on synthetic images, plus a live
`curl` proof that a spoofed Host header and a missing CSRF token are both rejected by the
running server, not just by the test suite.
