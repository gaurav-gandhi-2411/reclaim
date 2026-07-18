# 0011. Applied-AI layer: recommend-only architecture, optional extra, licenses

## Context

`reclaim-ai-features-spec.md` defines a new applied-AI layer (image/document similarity,
content tagging, a learning-to-rank clutter prioritizer) sitting beside the deterministic
engine built in Stages 1–7. The spec's non-negotiable §0 principles — recommend-only forever,
local/offline/zero-cost, CPU-first, two-stage compute, no manufactured confidence, near-
identical vs. semantic-similar as separate pipelines — need an architecture that enforces them
structurally, not by convention, before any feature or model is built. §7.5 names the
recommend-only guarantee "the single most important test in the AI layer."

Two additional constraints shape this decision: the core deterministic tool (already shipped,
security-audited, packaged for install — see the 2026-07-18 security-audit checkpoint) must
keep installing and running with zero new mandatory dependencies, and every AI candidate must
pass through the exact same `SafetyValidator` the deterministic engine uses, with no exemption.

## Decision

**One package, one distinct subpackage, one optional extra.** The AI layer lives at
`src/reclaim/ai/` (not a separate PyPI package) so `pip install reclaim[ai]` — a single named
extra — is the whole installation story, matching the spec's own phrasing exactly.

**Type-level separation, not just module-level.** `reclaim.ai.models.AICluster`/
`AIClusterMember` share zero field names with `reclaim.models.Candidate`. `reclaim.executor.
apply_batch` accesses `candidate.safety_verdict`/`.retention_days`/etc. unconditionally on
every item in its input; handing it an `AIClusterMember` instead raises `AttributeError`
immediately, before any filesystem call — a structural failure mode, not a documented
convention someone has to remember to honor.

**Import-graph separation, statically re-verified every CI run.** Nothing under
`src/reclaim/ai/` may import `reclaim.executor` or `send2trash`
(`evals/test_ai_safety_gate.py`'s AST-based scan, covering `import x.y`, `from x.y import z`,
and `from x import y` forms). `reclaim/cli.py` and `reclaim/api/{service,routes}.py` do not
import `reclaim.ai` at all yet — today's AI layer has no dashboard/CLI wiring whatsoever, the
strongest possible safety posture at this stage of the build.

**Shared, reused `SafetyValidator` — no reimplementation, no exemption.**
`reclaim.ai.safety.filter_paths_through_safety_validator` calls the exact same
`SafetyValidator.evaluate()` the deterministic engine calls, building a fresh `FileRecord` per
path rather than trusting caller-supplied metadata (the same "never trust a possibly-stale
record" discipline `executor._reverify_direct_delete_candidates` already applies before a real
delete). An AI candidate under a protected root, inside a git repo, etc. is dropped before it
can ever reach the `AIReviewQueue` — proven in `tests/test_ai_safety_reuse.py` against real
fixture files on disk, not just asserted in the abstract.

**Reserved namespace, checked structurally.** `AI_CATEGORY_GROUP_PREFIX = "ai_"` is reserved
for AI-layer output; `reclaim.detectors`'s source is grepped (not just run against a fixture
tree) to confirm it never emits that prefix, so the guarantee holds for every category the
deterministic engine could ever produce, not just the ones a particular fixture happens to
exercise.

**Config surface has no AI hook point, proven adversarially.** Every `pydantic` config model
in `reclaim.config` is `extra="forbid"`. `evals/test_ai_safety_gate.py` includes the exact
adversarial case named in the build brief: a hand-crafted `config.toml` that tries to inject an
`[categories.ai_near_duplicate]` section, and a subtler one that tries to add an AI-related
field to an *existing* category — both rejected at `load_config` time.

**Lazy, guarded optional imports.** `reclaim.ai._optional.require(module_name, feature=...)`
imports lazily, inside the function that actually needs it — never at module load time — and
raises `AIExtraNotInstalledError` (an `ImportError` subclass) with an actionable message
instead of letting a raw `ModuleNotFoundError` reach the user. `import reclaim.ai` and every
current submodule succeed with zero AI dependencies installed
(`tests/test_ai_optional_extra.py`, proven both by simulated import-blocking — durable
regardless of this environment's actual install state — and, when genuinely available, against
the real installed package).

## Consequences

- Adding AI-review wiring to the dashboard/CLI in a future feature will need
  `test_cli_and_api_service_never_import_reclaim_ai_today` narrowed (not deleted) to assert the
  wiring is read-only and never flows into `apply_batch`'s arguments — flagged in that test's
  own docstring so the requirement isn't lost.
- The AST-based import scan is a per-file static check, not a full transitive-closure analysis
  — a deliberately obscure re-export chain (module A imports `reclaim.executor`, module B
  under `reclaim.ai` imports only module A) could theoretically slip past it. Mitigated by
  defense in depth: even if such a chain existed, nothing calls it (no code path constructs a
  real `Candidate` from AI data), and the type-level `AttributeError` proof holds regardless of
  how the reference arrived. Documented as a known limitation, not treated as closed.
- Every future feature (1b, 1a Track B, 2, 3) adds its own dependencies to the same `ai` extra
  incrementally, each recorded with its license in the ADR that lands it — this ADR covers only
  what Feature 1a Track A needs (below).

## Licenses (Feature 1a Track A dependencies)

Verified via installed-package metadata (`importlib.metadata`), not assumed from memory —
command: `uv run python -c "import importlib.metadata as md; ..."` against
`uv.lock`-resolved versions.

| package | resolved version | license | redistribution |
|---|---|---|---|
| imagehash | 4.3.2 | BSD-2-Clause | permitted |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 | permitted |
| pillow | 12.3.0 | MIT-CMU | permitted |
| numpy (transitive) | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | permitted |
| scipy (transitive) | 1.18.0 | BSD License | permitted |
| pywavelets (transitive) | 1.9.0 | MIT AND BSD-3-Clause | permitted |

All permissive; no copyleft (GPL/AGPL) terms anywhere in the dependency closure. No model
weights are bundled by Feature 1a Track A (pHash/dHash are hash algorithms, not learned
weights); the classical keep-best scorer uses no pretrained model either — both facts make this
ADR's license table exhaustive for this feature, not a partial list pending a weights check.

## Alternatives considered

- **Separate PyPI package (`reclaim-ai`).** Rejected: the spec explicitly frames this as
  `pip install reclaim[ai]`, a single package with an optional extra, and a second package
  would double the release/versioning surface for no isolation benefit the subpackage
  boundary doesn't already provide.
- **Runtime feature-flag gate instead of type-level separation.** Rejected: a config flag or
  `if` check is convention, not structure — exactly the gap §7.5 calls out as unacceptable for
  the AI layer's recommend-only guarantee. Type/import-level separation fails at
  parse/attribute-access time, before any runtime logic can even run.

## Test coverage

`evals/test_ai_safety_gate.py` (13 cases — the §7.5 mandatory safety eval), `tests/
test_ai_safety_reuse.py` (4 cases), `tests/test_ai_optional_extra.py` (7 cases), `tests/
test_ai_eval_harness.py` (13 cases, BCubed/PR-curve arithmetic). Independent verifier pass
completed before this ADR was written; see PLAN.md's 2026-07-18 AI-layer checkpoint.
