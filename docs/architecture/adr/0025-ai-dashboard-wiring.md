# 0025. Wiring the AI layer into the dashboard (recommend-only)

## Context

`src/reclaim/ai/` (Features 1a Track A/B, 1b, 2, 3's feedback store, the clutter ranker) has
been built, safety-gated (`evals/test_ai_safety_gate.py`), and independently measured (ADR-0011
through ADR-0022) since Stage 1 — but, deliberately, never wired to any CLI or dashboard surface
(ADR-0011: "today's AI layer has no dashboard/CLI wiring whatsoever, the strongest possible
safety posture at this stage of the build"). `reclaim.ai.presentation` (plain-language
`AICluster` -> UI-copy translation) already exists, tested, unwired, for exactly this moment —
its own module docstring names the gap this ADR closes. Meanwhile the launch-ux pass
(`feat/launch-ux`) shipped a one-click-clean flow and a "How this works" modal that already
promises the user "AI only ever suggests, never deletes" — a promise with no surface behind it
yet.

ADR-0011's own Consequences section named the exact shape this wiring must take: "narrow (not
delete) `test_cli_and_api_service_never_import_reclaim_ai_today` to assert the wiring is
read-only and never flows into `apply_batch`'s arguments." That is this ADR's central
constraint.

Three questions need deciding before writing code: (1) when does AI analysis run, (2) how are
results cached across dashboard views/polls, (3) what happens on a core-only (`pip install
reclaim`, no `[ai]` extra) install — the majority of first installs, per ADR-0024's bundle-size
decision to ship the installer core-only by default.

## Decision

### 1. Explicit, opt-in "Analyze with AI" — never automatic

AI analysis is a separate, user-triggered action (`POST /api/ai/analyze`), never run as part of
`POST /api/scan`. Every AI pipeline here is measurably more expensive than the deterministic
scan (pHash over every image, MinHash+embedding over every document, OCR over screenshot
candidates, optionally a CLIP embedding pass) — running it unasked on every scan would silently
turn a multi-second scan into a multi-minute background job the user never asked for, exactly
the "presumptuous" failure mode the task brief calls out. It follows the scan endpoint's own
established pattern exactly: `POST` starts a background task and returns immediately (`202`,
mirroring `/api/scan`'s `202` — except when AI extras aren't installed, see decision 3, which
returns `200` since nothing was accepted for background work); `GET /api/ai/status` polls
progress the same way `GET /api/scan/status` already does; the frontend reuses the identical
poll-until-terminal pattern already implemented in `app.js::pollScanStatus`, not a new one.

A second call to `POST /api/ai/analyze` while one is already running returns `409` (identical to
`POST /api/scan`'s already-running guard) — no queueing, no silent no-op, matching the existing
single-in-flight-job precedent for this single-user, single-process tool (`AppState`'s own
docstring).

### 2. Caching: in-memory, keyed to a scan-generation counter, invalidated on next scan

`AppState` gains a plain `int` counter, `scan_generation`, incremented once per successfully
*completed* scan (`service.run_scan`'s success branch). `AIAnalysisStatus` (a new dataclass,
mirroring `ScanStatus`'s exact shape/locking pattern) records which `scan_generation` an analysis
pass covered; the last completed analysis's `list[AICluster]` lives on `AppState.ai_clusters`.
`GET /api/ai/status` and `GET /api/ai/suggestions` both compute `stale = (analysis's
scan_generation != current scan_generation)` — a `stale: true` flag tells the dashboard "a newer
scan completed since this analysis; re-run to refresh," without forcing a recompute on every
page load (the whole point of caching at all) and without ever serving genuinely wrong data
silently (the flag is explicit, not inferred by the frontend).

**Chosen: plain in-memory `AppState` field, not a persisted JSON file.** Consistent with every
other piece of this process's session state (`ScanStatus`, `csrf_token`, the mode log excepted
since that one is deliberately external/durable — see ADR-0023) — `AppState`'s own docstring
already establishes "single-process, in-memory, one instance per `create_app()` call" as the
right simplification for a single-user, localhost-only tool, and persisting AI results would be
the ONLY piece of this dashboard's session state that survives a restart, an inconsistency with
no clear benefit (a restarted server has no running scan, no running analysis, and re-running
"Analyze with AI" is a single click). **Disclosed consequence: a server restart loses the cached
analysis** — the dashboard's AI tab returns to "idle," and the user re-clicks "Analyze with AI."
This is judged an acceptable, honestly-stated cost, not a defect requiring persistence machinery
for a v1 wiring pass.

### 3. Degraded mode: cheap probe, typed response, never a 500

`reclaim.api.ai_orchestration.ai_extra_available()` checks `importlib.util.find_spec("imagehash")`
— a **probe**, not an import: `find_spec` resolves whether a module *could* be imported without
executing its top-level code, so this check costs nothing at startup and pulls in zero heavy
dependencies. `imagehash` is the single representative probe because the `ai` extra is one
unified `pip install reclaim[ai]` install — there is no supported partial install, so if the most
fundamental dependency (Track A's pHash prefilter) is missing, every other AI dependency is too.

Both `POST /api/ai/analyze` and `GET /api/ai/status`/`GET /api/ai/suggestions` check this first,
before touching `AppState.ai_status` at all, and return a typed `status: "unavailable"` response
with a friendly `unavailable_reason` ("AI features need the optional AI component — install
with: `pip install reclaim[ai]`") — never a `500`, never a raised `AIExtraNotInstalledError`
reaching the API boundary. This is a genuinely common path today: ADR-0024 ships the public
installer core-only by default, so most first installs will see this state the first time they
open the AI Suggestions tab, not as an edge case.

Beyond that front door, every individual pipeline inside `ai_orchestration.run_ai_analysis` is
independently wrapped: an `AIExtraNotInstalledError` (or any other exception — a pipeline's own
bug must never sink every other pipeline's real results) from one pipeline is caught, recorded
as a `(track, reason)` skip, logged, and the analysis continues with the remaining pipelines —
so a partial-extras environment (hypothetically) or one pipeline's transient failure (e.g. a
first-run CLIP-weight download failing for lack of network) degrades to "fewer suggestions,
honestly labeled why," never an aborted analysis or a 500.

### 4. Which pipelines run, on what inputs, and the caps

`ai_orchestration.classify_scan_files` splits the current scan's `full_inventory(under=root)`
into images (a curated extension set: jpg/jpeg/png/gif/bmp/webp/tif/tiff/heic/heif) and documents
(`reclaim.ai.document_text.is_supported_document` — txt/md/docx/pdf, the existing Feature-1b
list, not a second one). Every cap below is enforced and **counted, then reported** in
`AIAnalysisStatusOut.files_capped`/`files_considered` — never a silent truncation:

| bound | value | reason |
|---|---|---|
| max image file size | 25 MB | one oversized still photo must not dominate a background pass's wall-clock time |
| max document file size | 15 MB | same reasoning, document side |
| max images for Track A (near-identical) | 1,500 | pHash+Hamming clustering is the cheapest pipeline here but still O(n²) pairwise within a cluster bucket |
| max residual images for Track B (semantic) | 300 | CLIP embedding is the single most expensive per-file operation in this module; Track B only ever sees Track A's residual anyway |
| max documents for near-dup + version-chain | 800 | extract_text + MinHash + sentence-embedding per file |

Pipelines wired, one call each into the already-built, already-tested `reclaim.ai` orchestration
functions — no new AI logic, no new thresholds re-measured here (every threshold is the
already-ADR'd, already-measured operating point, passed through unchanged):

- **Near-identical images (Track A):** `image_similarity.build_near_identical_clusters` over the
  capped image set, `max_hamming_distance=14` (ADR-0012/ADR-0015's measured value).
- **Semantic image grouping (Track B):** `semantic_image_grouping.build_semantic_image_clusters`
  over Track A's residual (images not already claimed by any Track A cluster), capped
  independently at 300. Browse-only by construction (`AITrack.SEMANTIC_IMAGE`); the dashboard
  never offers a delete action for this track at all (see decision 6).
- **Document near-dup (Feature 1b):** `document_similarity.build_near_dup_document_clusters`
  over the capped document set, `minhash_threshold=0.1`, `embedding_threshold=0.95` (ADR-0017's
  measured, safety-follow-up-corrected joint operating point).
- **Version chains (Feature 1b):** **not a second clustering pass at a different threshold.**
  Every near-dup document cluster from the call above whose members include at least one
  recognizable filename-version pattern (`version_chain.filename_version_rank(path) is not
  None` for any member) is re-presented as a `VERSION_CHAIN` cluster over the *same* member set,
  via `version_chain.build_version_chain_cluster` — reusing the real ordering and the real
  `version_signals_agree` safety gate, rather than inventing a second, independently-tuned
  "looser" embedding threshold whose false-positive behavior nothing has measured. A near-dup
  cluster with no filename-version signal in it stays a flat `NEAR_DUP_DOCUMENT` (single keeper,
  no claimed order) exactly as `document_similarity.py` produced it. This is a deliberate scope
  simplification over "cluster twice at two thresholds" — see Alternatives.
- **Screenshot bursts (Feature 2):** `screenshot_review.build_screenshot_burst_clusters`, but
  only over images whose **filename** matches a screenshot-naming heuristic
  (`screen\s*shot|screenshot|scrnli|screen[_-]?capture|snip`, case-insensitive) — a cheap
  prefilter before the OCR-bearing pipeline runs, not a claim of exhaustiveness. **Disclosed
  limitation:** a renamed screenshot (e.g. "vacation_photo_23.png") is invisible to this
  prefilter and will never enter the screenshot-burst pipeline; it may still be caught by Track
  A if it's visually near-identical to another capture.
- **Clutter-ranker ordering:** applied once, at the end, across every cluster from every track
  above — never its own `AICluster`/`AITrack.RANKED_CLUTTER` entries (that track/shape is for
  singleton per-file ranking, not for ordering an already-clustered suggestions list). One
  representative member per cluster (the recommended keeper if one exists, else the first
  member) is scored via the real, already-trained `data/ai_models/clutter_ranker.txt` LightGBM
  model (checked into the repo — ADR-0021), using a `FeatureVector` built the same way
  `feedback_store.record_feedback_decision` builds one, except `sibling_decision_context` is
  always `(0, 0, 0)` — no accept/reject/keep history exists for AI-suggestion decisions in this
  dashboard yet (Feature 3's feedback logging isn't wired to the dashboard by this ADR; a
  documented future step, not silently faked here). If the model file is missing or `lightgbm`
  isn't installed, ranking is skipped (recorded as a `tracks_skipped` entry) and a deterministic
  fallback order is used instead: deletion-suggestions before browse-only, largest total cluster
  size first within each group — never a random or input-order artifact.

### 5. New endpoints

`POST /api/ai/analyze`, `GET /api/ai/status`, `GET /api/ai/suggestions` — added to
`reclaim/api/routes.py` following the exact existing patterns (`get_state`, `BackgroundTasks`,
the same lock-guarded `AppState` mutation shape as `/api/scan`). `GET /api/ai/suggestions` calls
`reclaim.ai.presentation.present_cluster` per cached `AICluster` and returns only the
presentation-layer `ClusterPresentation` fields (plus a member list for side-by-side display,
and each raw-file's size — no `AICluster`/`AIClusterMember` object ever crosses the Pydantic
response boundary; `AISuggestionOut` is a hand-mapped shape, not a `model_validate` pass-through
of the dataclass).

### 6. Acting on a suggestion: through the EXISTING `/api/apply`, safety-validated independently

The dashboard never adds a second apply path. Selecting specific AI-suggested files and clicking
apply sends those exact paths to the *existing* `POST /api/apply` (`ApplyRequest.paths`) —
byte-for-byte the same request shape the Review Queue's checkbox flow already sends.

This surfaced a real gap: `apply_selection` today only ever *narrows* the deterministic
`_all_candidates()` set by the requested `paths` — a path that was never flagged by any rule
detector (an ordinary photo or document, which is the overwhelmingly common case for an AI
suggestion) would silently match nothing and be dropped, making "select an AI suggestion and
apply" a silent no-op. `apply_selection` is extended: any requested path **not** already present
in the deterministic candidate set is independently re-validated, right there, through the exact
same `SafetyValidator.evaluate()` every deterministic candidate already goes through (a fresh
`FileRecord` via `reclaim.scanner.build_record_for_path`, not trusted caller metadata) and, only
if `Verdict.ELIGIBLE`, joins the batch as a `Candidate` with `category_group="user_selected"`,
**`tier=Tier.B` unconditionally** (never A — this path was never auto-quarantine-eligible) and
`retention_days=30` (a real vault-restore window if applied in power mode with `method="vault"`;
irrelevant in safe mode, which forces `recycle_bin` regardless — see below). A path that fails
that fresh safety check (inside a protected root, a git repo, etc.) is silently excluded from
the batch, exactly the same "BLOCKED means excluded, not erroring the whole request" posture
`reclaim.ai.safety.filter_paths_through_safety_validator` and `detectors.generate_candidates`
already use.

Deliberately named `"user_selected"`, not an `"ai_"`-prefixed group: this is a general
"apply an explicitly-named path outside the auto-detected candidate set" capability — the AI
Suggestions view is its first caller, not its only conceivable one — and keeping it out of the
`AI_CATEGORY_GROUP_PREFIX` namespace (reserved by ADR-0011 for what `reclaim.ai` itself
produces) avoids any appearance of blurring that reservation, even though the specific
structural test it protects (`test_ai_category_group_prefix_is_never_emitted_by_the_
deterministic_detectors`) only ever greps `reclaim/detectors.py`, which this code never touches.

From here, every existing guarantee applies completely unchanged: `apply_batch` still refuses
anything but `method="recycle_bin"` in `Mode.SAFE`; a batch containing this new candidate shape
still gets `SafetyValidator`-BLOCKED refusal as defense-in-depth (`SafetyInvariantError`) if
anything slipped past the fresh check; the Recycle-Bin/vault/direct-delete decision is still
made by `_effective_method_and_retention_days`, which has no idea (and needs no idea) that a
given `Candidate` originated from an AI suggestion rather than a rule detector.

**`reclaim.ai` itself is untouched by this decision** — it never imports `reclaim.executor` or
`send2trash` (unchanged, `evals/test_ai_safety_gate.py`'s AST scan still enforced), and no
`AICluster`/`AIClusterMember` object is ever constructed by, or passed into, `apply_batch`. The
bridge from "user clicked an AI-suggested path" to "safety-validated `Candidate`" lives entirely
in `reclaim/api/service.py`, which already imports neither `reclaim.executor`'s internals beyond
its existing public `apply_batch` call, nor anything AI-specific for this particular function.

**Per-item explicit confirmation**, unchanged from the existing Review Queue flow: the frontend
never auto-selects an AI suggestion's members; the user checks each file individually (mirroring
`renderCandidateCard`'s existing checkbox pattern) before Preview/Confirm, and `SEMANTIC_IMAGE`
clusters render with **no checkbox, no apply action, no selection affordance at all** — browse
text only (see decision 7).

### 7. Frontend: a new, clearly separate "AI Suggestions" tab

A fourth `<nav>` tab, `AI Suggestions`, alongside Overview/Storage Treemap/Review Queue/
Quarantine — never merged into the Review Queue's existing candidate list, so the "these are
deterministic, rule-based, always-safe" framing of Quick Clean/Review Queue is never diluted by
AI-recommended entries appearing in the same list. Loading/empty/error states follow the
existing `renderState`/`rc-state-panel` pattern exactly (rule 15a); a fourth explicit
**unavailable** state (distinct styling, an inline `pip install reclaim[ai]` instruction, no
retry button — nothing to retry until the extra is installed) is added specifically for decision
3's degraded-mode response, the one state this dashboard didn't previously need.

Per track: image near-dups and screenshot bursts show a side-by-side, text-only member table
(filenames/sizes — reusing `renderClusterTable`'s exact `textContent`-only path-rendering
discipline, never `innerHTML`, for every raw filesystem path) with the recommended keep
highlighted and its `_quality_reason`/`_screenshot_content_note` copy shown; version chains show
an ordered list (newest marked, or the "we can't tell which is newest" copy with no checkboxes
when signals disagree); semantic groups render with the `BROWSE_ONLY_NOTE` and — structurally,
not just by convention — the render function for that track never creates a checkbox or an
apply button at all, mirroring the same "structural, not conventional" discipline the AI safety
gate itself is built on. The clutter-ranker's ordering is applied silently (the list is just
*in* that order); `RANKED_CLUTTER_LIST_LABEL` is shown once, above the whole list, not
per-cluster. `technical_detail` (`presentation.py`'s one place a raw number appears) renders
inside a collapsed `<details>` per cluster, labeled with its unit exactly as `presentation.py`
already produces it — never a percentage, matching the existing "no invented confidence" rule.

No thumbnail-serving endpoint is added in this pass — every member is described in text (name,
size, and dimensions where the schema carries them) rather than rendering an actual image
preview. Judged out of scope for this wiring pass: a path-validated thumbnail endpoint (serving
only files that are current, real cluster members, never an arbitrary path) is a legitimate,
separable future enhancement, not a requirement to make suggestions reviewable today.

## Consequences

- **A server restart loses the cached AI analysis** (decision 2) — re-running "Analyze with AI"
  is one click, judged an acceptable v1 cost over persistence machinery this single-session tool
  doesn't otherwise have anywhere else.
- **Version-chain detection piggybacks on the near-dup threshold rather than an independently
  measured "looser" one** (decision 4) — a real document family whose members' embedding
  similarity happens to sit between "clearly the same document" (0.95) and "unrelated" would
  never be offered as a version chain at all, only as a stricter near-dup or nothing. This is a
  disclosed, deliberate scope decision (see Alternatives) to avoid shipping a second,
  unmeasured operating point; a future ADR could measure a dedicated "candidate family" threshold
  if this proves too narrow in practice.
- **The screenshot-burst filename prefilter is a heuristic, not exhaustive** (decision 4) — a
  renamed screenshot is invisible to it. Documented, not silently narrowed.
- **`test_cli_and_api_service_never_import_reclaim_ai_today` (evals/test_ai_safety_gate.py) is
  narrowed, not deleted**, per ADR-0011's own Consequences instruction: it now asserts (a)
  `cli.py` still never imports `reclaim.ai` (CLI wiring remains out of scope), and (b)
  `api/service.py`/`api/routes.py` DO import `reclaim.ai` now (expected), but a fresh,
  additional static/runtime check proves the read-only boundary instead — every function in
  `reclaim.ai` that `service.py`/`ai_orchestration.py` calls returns `AICluster`/presentation
  data only, and `apply_batch`'s only candidate source anywhere in `service.py` remains
  `_all_candidates()` (deterministic) plus the new, independently-safety-validated
  `_build_user_selected_candidate` path — never a `reclaim.ai` type.
- **No thumbnail/image-preview endpoint** — every suggestion is reviewed as text (filenames,
  sizes, the technical detail). A legitimate future enhancement, not required for this pass to
  be useful or safe.
- **Clutter-ranker `sibling_decision_context` is always zero** in this wiring — Feature 3's real
  feedback-driven personalization is a documented future step, not fabricated here.

## Alternatives considered

- **Auto-run AI analysis as part of every scan.** Rejected — the task brief's own framing
  ("running them unasked... would be slow and presumptuous") and the real, measured cost
  difference between a deterministic scan (seconds) and the AI pipelines (potentially minutes,
  plus a first-run model download for Track B) make this the wrong default; explicit opt-in
  costs the user one extra click and never surprises them with an unexpected multi-minute wait.
- **Persist the AI cache to `data/ai_cache/analysis.json`.** Considered for parity with
  `data/quarantine/manifest.jsonl`'s durability — rejected for v1: every other piece of this
  process's *session* state (as opposed to durable records like the quarantine manifest or the
  mode log) is already in-memory-only, and a persisted AI cache would need its own
  staleness/invalidation-on-restart story that duplicates the `scan_generation` mechanism this
  ADR already needs for the in-process case. Revisit if real usage shows restarts are frequent
  enough to make re-analysis genuinely annoying.
- **Cluster documents at a second, independently-measured "looser" threshold specifically for
  version-chain candidacy.** Rejected for this pass: no dataset or eval currently backs a
  version-chain-candidacy threshold distinct from the near-dup one, and shipping an unmeasured
  number as if it were calibrated would repeat exactly the mistake ADR-0016 exists to prevent.
  Reusing the near-dup cluster's own member set (decision 4) ships version-chain ordering today
  without inventing an unmeasured number; a dedicated ADR can revisit if this proves too narrow.
- **Give AI-suggested candidates their own `"ai_"`-prefixed category_group** (e.g.
  `"ai_suggested"`). Rejected in favor of `"user_selected"` — see decision 6's naming rationale.
- **Build a full thumbnail-serving endpoint in this pass.** Rejected as out of scope: the task
  explicitly allows text-only side-by-side comparison, and a path-validated image-serving
  endpoint is real, separable surface area (path traversal risk, cache-control, MIME sniffing)
  better shipped as its own reviewed, tested change.

## Test coverage

`tests/test_api_ai.py` (endpoint status/analyze/suggestions transitions, the degraded-mode
`AIExtraNotInstalledError`-simulated path via the same `block_ai_extra_imports`-style
import-blocking `tests/test_ai_optional_extra.py` already established, cap/skip reporting),
`tests/test_ai_orchestration.py` (unit-level: classification, caps, the near-dup/version-chain
split rule, clutter-ranker fallback ordering), `tests/test_api_ai_apply_safety.py` (the critical
proof: an AI-suggestion-shaped path submitted through the real `/api/apply` still gets
`Mode.SAFE`'s recycle-bin/Tier-B treatment, and a `BLOCKED` path is silently excluded, never
force-applied), `evals/test_ai_safety_gate.py`'s narrowed
`test_cli_and_api_service_never_import_reclaim_ai_today` plus new regression coverage for the
"apply_batch's only candidate sources are deterministic or freshly-safety-validated" invariant,
`tests/frontend/xss.test.mjs` gains coverage for the new AI-suggestion render path's
path-rendering discipline.
