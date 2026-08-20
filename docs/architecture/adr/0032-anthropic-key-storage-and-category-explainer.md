# 0032. Anthropic API key storage and the per-category LLM explainer (R2)

## Context

R2 ("per-category LLM explainer") was audited as entirely absent, but confirmed *clean slate*:
zero code anywhere in `src/reclaim/` reads `ANTHROPIC_API_KEY`, no key storage, no settings UI,
no prose-generation code (`docs/AUDIT-2026-08.md`, "R2" row). This is the first feature in this
codebase that makes an outbound call to a paid third-party API and the first that stores a
user-entered secret — both new surfaces, so this ADR exists per this project's own "new systems
land in an ADR before implementation" convention, even though the feature itself is scoped and
non-irreversible.

Three questions needed deciding: (1) how to store the user's Anthropic API key durably without
ever writing it in plaintext, (2) how to call the Anthropic API without ever risking this
project's standing "never read `ANTHROPIC_API_KEY` from the environment" invariant (the user is
on a Claude Max plan and this app must only ever use a key they explicitly entered in-app), (3)
what this module's input/output shape must look like to make "cannot influence a delete
decision" a structural fact, not a convention.

## Decision

### 1. Key storage: Windows DPAPI via `ctypes`, not a new dependency

`src/reclaim/anthropic_key_store.py` calls `CryptProtectData`/`CryptUnprotectData` directly via
`ctypes.windll.crypt32`, current-user scope, `CRYPTPROTECT_UI_FORBIDDEN` (never a blocking OS
prompt). This mirrors `elevation.py`'s existing precedent for touching a Windows-native API
directly rather than adding a dependency (`elevation.py` calls `shell32.IsUserAnAdmin` the same
way). The encrypted blob lives at `data/anthropic_key.bin` by default — the same `data/`
convention every other piece of durable state in this app already uses (`data/mode_log.jsonl`,
`data/first_run_state.json`, `data/logs/reclaim.log`).

DPAPI ties the ciphertext to the current Windows user account: only a process running as that
same OS user can ever decrypt it. No separate password or key-encryption-key to manage — the
user's own Windows login is the root of trust, which is the right fit for a single-user,
localhost-only desktop tool (the same framing `AppState`'s own docstring already uses for why an
in-memory, per-process state dataclass is sufficient here).

**Alternative considered: a third-party secrets library (`keyring`, `cryptography` + a
locally-generated key file).** Rejected — `keyring` would add a new dependency for something
`ctypes` + two Win32 calls already does natively (~150 lines, zero new dependency, matches the
existing `elevation.py` precedent exactly); a hand-rolled `cryptography`-based scheme would need
its own key-management story (where does the encryption key live? another file to protect?)
that DPAPI already solves by delegating to the OS's own per-user secret store.

### 2. Anthropic API access: raw `httpx`, never the `anthropic` SDK

`src/reclaim/ai/category_explainer.py` calls the Anthropic Messages API (`POST /v1/messages`)
and a lightweight key-validation call (`GET /v1/models`) directly over `httpx` — already a
project dependency (`reclaim.update_check`, the only other outbound HTTP call in this codebase)
— rather than adding the `anthropic` Python SDK as a new dependency.

This is a safety decision, not just a dependency-minimization one: the `anthropic` SDK's
`Anthropic()` client falls back to reading `ANTHROPIC_API_KEY` from the environment when no
`api_key` is passed explicitly. That fallback is exactly the failure mode this project's
key-handling requirement forbids — the user is on a Claude Max plan, and this app must never
silently pick up that unrelated credential even by accident (e.g. a future refactor that drops
the explicit `api_key=` kwarg somewhere). Calling the REST API directly means there is no
SDK-level fallback to guard against in the first place: every function in this module takes
`api_key` as an explicit argument, sourced by the caller from `anthropic_key_store.load_key`
(the DPAPI-decrypted, explicitly-entered-in-app key) — never from `os.environ`. Verified by
`grep -r ANTHROPIC_API_KEY src/reclaim/` returning zero matches after this change, same as
before it.

**Model ID uncertainty, disclosed rather than guessed as fact.** This implementation session did
not have access to a current reference for Anthropic's model catalog/pricing (the `claude-api`
skill referenced in the task brief was not present in this environment). `DEFAULT_MODEL =
"claude-3-5-haiku-20241022"` is a real, previously-documented Anthropic model id chosen for
being cheap and stable, but it is explicitly flagged in the module docstring as unverified for
"current cheapest/recommended model as of ship date" — both `explain_category` and
`validate_api_key` accept an explicit `model` override specifically so this can be corrected
without an API shape change once a real pricing/model reference is available. Per this project's
own "never invent APIs, flags, or numbers" discipline, this is disclosed as an open item, not
silently presented as verified.

**Alternative considered: adding the `anthropic` SDK dependency.** Rejected for the reasons
above (env-var fallback risk) and because this module's actual API surface needs (`POST
/v1/messages`, `GET /v1/models`, both trivial JSON-over-HTTP calls) don't need the SDK's
streaming/retry/typed-response machinery — `httpx` + a ~15-line JSON-parsing helper is the
"simplest solution that satisfies the constraints," matching update_check.py's existing shape
in this same codebase.

### 3. Structural guarantee: `CategoryDescriptor` in, `CategoryExplanation` out, nothing else

`explain_category`'s only input is `CategoryDescriptor` — `category_group`, `display_name`,
`file_count`, `total_size_bytes`, `tier`, `retention_days` — aggregate, category-level
statistics only. There is no `paths`/`sample_files`/`candidates` field on this dataclass, and
its only output, `CategoryExplanation`, carries `category_group`, `explanation` (plain prose),
and `cached` — nothing resembling a `Candidate`/`AICluster`, nothing that could be fed into
`apply_batch` even by mistake. This mirrors `reclaim.ai.presentation`'s existing discipline
(`ClusterPresentation` never carries a raw judgment the input cluster didn't already have) one
level further: this module never sees an individual path at all, so it cannot leak one into a
prompt or a response even accidentally. Enforced at three levels: (a) a type-level unit test
asserting the exact field sets of both dataclasses contain no path/candidate-shaped name, (b) a
signature-level test asserting `explain_category` accepts no path/candidate/selection kwarg, (c)
the same AST-level `evals/test_ai_safety_gate.py` scan every other `reclaim.ai` module is
checked by (this module lives under `src/reclaim/ai/`, so it's automatically covered by the
existing recursive scan — this ADR adds an explicit coverage-sanity test plus a negative test
proving the scan mechanism would actually catch a regression here, matching the negative-test
discipline other safety-critical tracks in this repo already use).

### 4. Caching: fingerprint the descriptor's own fields, never a scan id

`data/ai_explanations/<fingerprint>.json`, where `fingerprint` is a SHA-256 of
`category_group`/`file_count`/`total_size_bytes`/`tier`/`retention_days` (deliberately excluding
`display_name`, a cosmetic label with no independent variation). A re-scan that finds the exact
same category totals is a guaranteed cache hit (zero tokens spent, zero network calls — verified
by a mocked-client call-count assertion in `tests/test_ai_category_explainer.py`); a genuinely
changed category is a guaranteed cache miss. A cache hit needs no API key at all — the
cache-first check happens before the key-presence check in both `explain_category` and the
API-layer wiring, so a previously-generated explanation stays visible even if the key is later
removed from Settings.

### 5. Degrade gracefully everywhere; never a 500

Every new endpoint (`GET/POST/DELETE /api/settings/anthropic-key`, `POST
/api/settings/anthropic-key/test`, `GET /api/ai/category-explanation/{category_group}`) is
wrapped so a missing key, no scan data, a corrupted DPAPI blob (e.g. produced under a different
Windows account), or a real Anthropic API failure (network, auth, malformed response) all
degrade to a typed response — `AnthropicKeyStatusResponse`/`TestAnthropicKeyResponse`/
`CategoryExplanationResponse`'s `status`/`valid` fields — never a raised exception reaching the
route. This matches `reclaim.update_check.check_for_update`'s existing "best-effort, never
raises" posture, the only other best-effort outbound-network feature in this codebase.

### 6. Settings UI: a new "Settings" tab, standalone for now

No Settings/category-toggle surface existed anywhere in the dashboard at the time this branch
was cut (checked: no `feat/settings-tab`-shaped branch had landed on `main` or was ahead of it).
This adds a fifth `<nav>` tab with a password-masked key-entry field, Save, a "Test key" button
(calls the cheap `/test` endpoint before any real spend), and Remove. If a parallel Settings-tab
track (category toggles) lands separately, the two should be consolidated into one tab rather
than shipping two — flagged in the PR description for the merge to note.

## Consequences

- **`DEFAULT_MODEL`'s exact id is unverified** against Anthropic's current catalog/pricing (see
  decision 2) — a real, functioning risk until someone with catalog access confirms or updates
  it; the `model` override parameter exists specifically so this is a one-line fix, not an API
  change.
- **No `usd_cost` dollar-figure logging** — token counts (`tokens_in`/`tokens_out`) are logged
  per call, but no dollar conversion is computed, because no verified current pricing table was
  available this session (same root cause as the model-id caveat above). A future change can add
  the pricing constant once verified, following this project's existing "log `usd_cost` in the
  `finally:` block" convention for LLM calls.
- **A server restart does not lose the API key** (DPAPI blob is durable, unlike the rest of
  `AppState`'s in-memory session state) — this is a deliberate, disclosed asymmetry: a
  credential the user typed in once should not need re-entry every time `reclaim serve`
  restarts, unlike e.g. the AI-suggestions cache (ADR-0025), which is genuinely one click to
  regenerate.
- **The Settings tab may need consolidating** with a parallel category-toggle Settings surface if
  one lands separately — see decision 6.

## Alternatives considered

- **Persist the key in `config.toml`.** Rejected outright — `config.toml` is plaintext,
  human-editable, and this project already keeps every other secret-shaped value (there are
  none today) out of it; DPAPI is the correct mechanism for exactly this class of problem on
  Windows.
- **Use the `anthropic` SDK.** Rejected — see decision 2's env-var-fallback risk.
- **Wire the explainer directly into every category card with no explicit user action.**
  Rejected for this pass — mirrors ADR-0025's decision 1 reasoning (AI analysis is opt-in, never
  automatic): auto-calling a paid API on every scan/page-load would spend the user's money
  without asking, the same "presumptuous" failure mode that ADR already named. The route this
  ADR adds is deliberately request-scoped, user-triggered.

## Test coverage

`tests/test_anthropic_key_store.py` (real DPAPI round-trip, corrupted-blob rejection, no-op
delete), `tests/test_ai_category_explainer.py` (structural path-freedom proofs, cache-hit-avoids-
a-second-call via a call-counting `httpx.MockTransport`, key-missing/API-failure error paths,
never-logs-the-key-in-the-request-body proof), `tests/test_api_anthropic_settings.py`
(endpoint-level: key status/set/delete/test transitions, category-explanation degrade-gracefully
across every failure mode, and an explicit "the API key never appears in any response body,
including diagnostics" proof), `evals/test_ai_safety_gate.py` (extended: explicit coverage
sanity check + a negative test proving the AST scan would catch a regression in this module).
