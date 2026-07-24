# 0027. Schema versioning for the three durable on-disk formats

## Context

A production-readiness audit (2026-07-24, see `PLAN.md`, item B8) found a gap shared by the
three pydantic models that are this app's durable on-disk schemas:

- `executor.QuarantineManifestEntry` — one line in `data/quarantine/manifest.jsonl`.
- `mode.ModeChangeEntry` — one line in `data/mode_log.jsonl`.
- `config.Config` (and its nested per-category configs) — parsed from `config.toml`.

All three set `model_config = ConfigDict(extra="forbid")` (or the `SettingsConfigDict` equivalent
for `Config`) and had no version field at all. Consequence: the next release that adds any field
to any of these three formats hard-crashes **every older installed copy** the instant it reads a
line/file written by the newer version — `pydantic_core.ValidationError` on the unrecognized
extra field, uncaught anywhere in the codebase.

This is especially bad for `ModeChangeEntry`: `mode.current_mode()` is on the load path of nearly
every CLI command (`apply`, `purge`, `undo`, `serve`, `mode` itself) to resolve the live
safe/power mode. One incompatible mode-log line — e.g. a rollback to an older binary after a
newer one already wrote a line, or two installs sharing a `data/` directory during an upgrade —
breaks the **entire CLI**, not just one subcommand.

`QuarantineManifestEntry` has an extra wrinkle ADR-0026 already exposed: entries are
**re-serialized** after being read. `restore_batch`/`purge_expired`/`reclaim.recovery` all call
`entry.model_copy(update={...})` on an already-parsed entry and then `model_dump_json()` the
result back to disk (e.g. `intent_entry.model_copy(update={"phase": "done", ...})`). A forward-
compat strategy that merely *tolerates* unrecognized fields on read but drops them on that
read-modify-write cycle would fix the crash while introducing a **new, silent data-loss bug** —
an unrecognized field written by a newer release would vanish the moment an older release's
`restore_batch` touched that same manifest line.

## Decision

**A `schema_version: int` field on all three models, defaulting to `1`.** ADR-0026 had already
added `phase`/`intent_id`/`operation` to `QuarantineManifestEntry` without any version concept at
all — there was no "version 0" of the format for those fields to be distinguished from, since
schema versioning did not exist yet at that point. `1` is defined here as the version identifying
the full current shape of each model (including ADR-0026's fields), not a retroactive re-numbering
of history. A pre-this-ADR line/file has no `schema_version` key at all and validates with the
field defaulting to `1` — this is the literal truth for that data, not an approximation, since `1`
is the version every field on these lines already belongs to.

**Forward compatibility (a newer release's data, read by this code) — different strategy per
model, chosen by whether the model is ever re-serialized:**

| Model | `extra=` | Why |
|---|---|---|
| `QuarantineManifestEntry` | `"allow"` | Re-serialized after being read (`model_copy` + `model_dump_json` in `executor.py`, `purge.py`, `recovery.py`). `"allow"` stores unrecognized fields on the model instance and includes them again on the next `model_dump_json()` — verified empirically: a field unknown to this model survives an `model_validate_json` → `model_copy(update=...)` → `model_dump_json()` round trip byte-for-byte. `"ignore"` would silently drop it on that exact round trip — a new data-loss bug, not a fix. |
| `ModeChangeEntry` | `"ignore"` | Never re-serialized — every entry is freshly constructed by `switch_to_power_mode`/`switch_to_safe_mode` and appended once, immutably (no `model_copy` call on this class anywhere in the codebase). There is no read-modify-write cycle for an unrecognized field to be lost from, so `"ignore"` is exactly as safe as `"allow"` here and simpler — no unbounded extra-data accumulation on a class deliberately kept minimal for `current_mode()`'s hot path. |
| `Config` and every nested category config | `"ignore"` | `config.toml` is only ever parsed into memory; nothing in the codebase serializes a `Config`/category config back to `config.toml` (`model_copy` calls in `load_effective_config`/`api/state.py` build in-memory views for a request, never write the file). No round-trip, so `"ignore"` is safe, and preferable to `"allow"` — an accumulating pile of unrecognized TOML keys has no purpose once nothing round-trips them. |

Both `read_manifest_entries` (executor.py) and `current_mode` (mode.py) additionally compare each
parsed entry's `schema_version` against the current known constant
(`QUARANTINE_MANIFEST_SCHEMA_VERSION` / `MODE_LOG_SCHEMA_VERSION`) and log a `structlog` warning —
never raise — listing every newer version actually encountered. `load_config` does the same for
`Config.schema_version` against `CONFIG_SCHEMA_VERSION`, and additionally walks the parsed TOML
dict to warn (never raise) about any top-level or per-category key it doesn't recognize
(`_warn_on_unknown_config_keys`) — `extra="ignore"` alone would silently absorb both a newer
release's new config key *and* a plain user typo with no signal at all; the warning preserves the
actionable diagnostic a hard crash used to accidentally provide, without the crash itself.

**Backward compatibility (older, pre-this-ADR data, read by this code) — already covered by
"add fields with defaults."** Every new field (`schema_version` itself, and ADR-0026's earlier
`phase`/`intent_id`/`operation`/`confirmed`-adjacent additions) has a default, so a manifest
line/log entry/config file written before this change parses and behaves exactly as before.
Verified explicitly with hand-written pre-this-change JSON/TOML fixtures in
`tests/test_manifest_schema_versioning.py`, `tests/test_mode.py`, and `tests/test_config.py`
(constructed from this exact pre-ADR-0027 shape, not synthesized from the post-change model).

## Consequences

- A future field addition to any of the three formats is a **routine, non-breaking** change again:
  bump the relevant `*_SCHEMA_VERSION` constant, add the field with a sensible default, and both
  directions (older code reading newer data, newer code reading older data) keep working without
  a crash.
- `QuarantineManifestEntry` instances now carry a `model_extra` dict for any field this version of
  the code doesn't recognize. Every place that reserializes an entry (`model_copy` + fresh
  `model_dump_json()`) already round-trips this correctly, verified by
  `test_manifest_schema_versioning.py::test_forward_compat_unknown_field_survives_read_modify_write_round_trip`.
  No existing call site inspects `model_extra` itself — a future release that wants a genuine
  migration path (not just tolerance) should do so explicitly, not by accident.
- `ModeChangeEntry`/`Config` unrecognized fields are silently dropped by design (no round-trip
  need), a deliberate trade-off documented here rather than an oversight.
- The warning-log path is best-effort observability, not a substitute for testing — it does not
  change parsing behavior, only what gets logged.
- `data/quarantine/manifest.jsonl` entries continue to use structural fields already present
  before this ADR (`phase`, `intent_id`, `operation` from ADR-0026) unchanged; this ADR is purely
  additive on top.

### A real regression this ADR caused, and its fix

A first version of `Config`'s `_check_unknown_config_keys` (originally `_warn_on_unknown_config_keys`)
tolerated **every** unrecognized top-level/category/category-field key unconditionally, warning
but never raising — this broke `evals/test_ai_safety_gate.py`'s adversarial requirement that a
hand-edited `config.toml` must never be able to smuggle an `ai_`-named category or field into the
deterministic candidate pipeline; two of that suite's tests (`test_malicious_config_cannot_inject_an_ai_category_section`,
`test_malicious_config_cannot_add_an_ai_tier_field_to_an_existing_category`) started silently
passing malicious input instead of rejecting it, caught by CI's `safety-gate` job on the
retargeted PR, not by `pytest tests/ -q` (the `evals/` suite runs as a separate CI job and was not
re-run against this branch specifically before that point — a real gap in this branch's own
pre-push verification, not just an unlucky miss).

**Fix**: forward-compat tolerance is now gated on the file's own `schema_version` genuinely being
**greater than** `CONFIG_SCHEMA_VERSION` — a real, checkable claim that the file came from a
newer release. Absent that claim (no `schema_version` key, or one equal to/less than current), an
unrecognized key raises `UnknownConfigKeyError` — the exact same hard-reject `extra="forbid"`
gave before this ADR. Forward compat was never meant to mean "any unrecognized key, ever, no
matter what" — only "a key this exact kind of file can honestly claim wasn't invented by this
version's own maliciously/accidentally malformed input." Two of this ADR's own test cases
(`test_forward_compat_unknown_top_level_key_does_not_raise`,
`test_forward_compat_unknown_category_level_key_does_not_raise`) had used `schema_version = 1`
(current, not newer) to demonstrate tolerance — updated to use a genuinely newer version (`2`),
since `schema_version = 1` alongside an unrecognized key is exactly the ambiguous case that must
now raise, not the forward-compat case it was meant to demonstrate.

## Alternatives considered

1. **`extra="ignore"` uniformly across all three models, accepting the round-trip data loss for
   `QuarantineManifestEntry`.** Rejected: the crux of this ADR is that a future field added to the
   manifest format would silently vanish the first time `restore_batch`/`purge_expired`/
   `reclaim.recovery` touched that line — a real, if delayed, data-loss bug masquerading as a fix
   for the crash bug.
2. **A dedicated migration/upcasting layer (explicit `schema_version`-keyed transform functions)
   instead of tolerate-and-warn.** Rejected for now: no concrete future field addition exists yet
   to migrate *to*; a speculative migration framework for a hypothetical future shape is
   complexity without a stated requirement (house rule: no premature abstraction). Revisit if/when
   a real breaking field change (not merely additive) is actually needed.
3. **Refuse (raise) a per-record error only for entries with a newer `schema_version`, rather than
   log-and-continue.** Rejected for `QuarantineManifestEntry`/`ModeChangeEntry`: an entry with a
   `schema_version` the code doesn't recognize almost always still has every *field* the code
   does recognize (additive evolution, the norm this project follows) — refusing it outright would
   throw away a perfectly readable record over a version number alone, exactly the over-eager
   crash this ADR exists to eliminate. Reserved as a future option if a genuinely
   backward-incompatible field change is ever introduced (see alternative 2).
4. **A single shared `SchemaVersioned` mixin base class for all three models.** Rejected: the three
   models already differ in base class (`BaseModel` vs `BaseSettings`) and in `extra=` policy per
   the table above — a shared mixin would either force one `extra=` policy on all three (wrong, per
   the round-trip analysis) or need per-model overrides that erase most of the mixin's value. Three
   small, independently-documented fields is simpler and matches "duplicate twice, abstract on the
   third occurrence" — there is no third schema-versioned format in this codebase today.
