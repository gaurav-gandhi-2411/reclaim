# 0029. Enabling AI features on a packaged (Nuitka) install — proposed, not built

## Context

ADR-0024 shipped the public installer core-only and left "enabling AI on a Nuitka-installed
`reclaim.exe`" as a disclosed, deferred gap (its Consequences section, 2026-07-23): the compiled
binary isn't a `pip`-installable environment, so `pip install reclaim[ai]` — the only instruction
this codebase has ever shown a user — has no working target for anyone who got Reclaim via the
installer, which is most real users. Reclaim also isn't published to PyPI, so that exact command
is doubly wrong for that audience (there's no `reclaim` package for `pip` to find at all).

Workstream C (SIMPLE/ADVANCED UX, 2026-07-26) surfaced this again under stricter framing:
"AI features must work end-to-end for a normal user... if installing the AI component is
currently hard for a non-technical user, fix that." Investigating the fix revealed it's not
"hard," it's **entirely absent** — there is no code path, in-app or installer-side, that can add
the AI extra to an already-installed copy of Reclaim today.

## Decision (this ADR): fix the honest disclosure now, defer the real mechanism

Two things happened in this pass, deliberately not the same size of change:

1. **Immediate fix, shipped in this pass**: `service.py`'s `_AI_UNAVAILABLE_REASON` and
   `_optional.py`'s `require()` error message no longer tell an installer-distributed user to run
   `pip install reclaim[ai]`. They say plainly that there is currently no way to add AI features
   to an installed copy, and point source-checkout developers at the one instruction that's
   actually true for them (`uv sync --extra ai`). This is a correctness fix to existing, shipped,
   misleading copy — small, safe, and worth landing independently of the larger question below.

2. **Proposed target architecture, NOT implemented in this pass**: see below. Explicitly deferred
   as its own scoped build, not rushed alongside Workstream C's scan-ETA and SIMPLE/ADVANCED UI
   work in the same session. Recorded here so the decision and its reasoning aren't lost, and so
   a future pass has a concrete starting design rather than an open question.

## Proposed architecture: a separate, downloadable AI runtime, invoked via subprocess

**Shape**: an Inno Setup optional component ("AI-powered suggestions") that, when checked at
install time, extracts a portable embeddable Python distribution to
`{app}\ai-runtime\python\`, bootstraps `pip` into it (the standard `get-pip.py` pattern for
embeddable Python, which ships without `pip` by default), and `pip install`s the `[ai]` extra
dependencies plus Reclaim's own wheel (bundled in the installer for this purpose, since Reclaim
isn't on PyPI) into that runtime. This needs internet access during install — an acceptable
trade-off for an opt-in, clearly-labeled ~1GB component, the same expectation most installers
with optional heavy components set.

At runtime, `reclaim.exe`'s AI-availability check (`ai_orchestration.ai_extra_available()`) gains
a second signal alongside the existing in-process `importlib.util.find_spec` probe: does
`{app}\ai-runtime\python\python.exe` exist? If so, AI operations dispatch to it via a small
worker script (`python.exe -m reclaim.ai.worker <command> <json-args>`, JSON over stdout) instead
of importing in-process — the compiled `reclaim.exe` never itself imports `torch`/`sentence-
transformers`/etc.; it shells out and reads structured output back.

**Why this shape, not the alternatives**: bundling AI deps directly into the Nuitka binary was
already rejected by ADR-0024 (~1GB, forces every install through AI's startup cost). Publishing
Reclaim to PyPI and telling installer users to separately `pip install` a whole second copy of
the tool is confusing (two installations of the same app, two update paths) and doesn't actually
solve "a normal user" — it just moves the Python-environment problem onto them instead of
automating it. A subprocess-based runtime the installer manages is the only shape that gives a
non-technical user a single checkbox, not a command line.

## Why this isn't built yet

This is a real architecture change, not a scoped bugfix:

- **New IPC boundary carrying the same safety obligations as everything else in this codebase.**
  ADR-0011's whole design — type-level separation, static AST import scanning
  (`evals/test_ai_safety_gate.py`) proving `reclaim.ai` never imports `reclaim.executor`/
  `send2trash` — exists because the recommend-only guarantee (§7.5) must hold structurally, not
  by convention. A subprocess worker needs the identical rigor extended across a process
  boundary: proving the worker process can't reach a delete path either, not just trusting that
  its Python imports happen to be safe today. That's new eval-suite work, not a checkbox.
- **New packaging engineering**: bundling and testing an embeddable-Python + `get-pip.py`
  bootstrap + wheel-install sequence reliably across real Windows machines (varying permissions,
  antivirus interference, partial-failure recovery if the install step fails partway) is real
  effort with real failure modes for exactly the non-technical audience this is meant to serve —
  a half-working installer component would be worse than today's honest "not available yet."
- **Requires building and shipping Reclaim's own wheel** as part of the release process (it isn't
  built today; the Nuitka pipeline never produces one), a small but real addition to
  `packaging/`.

Rushing this alongside Workstream C's scan-ETA and SIMPLE/ADVANCED UI work in the same session
would mean shipping it without the adversarial safety-boundary testing this project has
consistently applied to every other AI-adjacent change (ADR-0011, ADR-0022, ADR-0025) — worse
than shipping the honest "not yet" message above and building this properly as its own pass.

## Consequences

- Today, after this pass: a packaged-installer user sees an accurate message explaining AI
  features aren't available on their install and why, instead of a broken command. A
  source-checkout developer sees the one instruction that's actually true for them.
- The SIMPLE/ADVANCED UX work (Workstream C) ships without a working "enable AI" affordance in
  either mode — AI Suggestions stays ADVANCED-only and, for installer users, stays unavailable
  there too until this ADR's architecture is built. Not a regression (nothing worked before
  either) but worth stating plainly rather than letting it read as solved.
- This ADR is a proposal + reasoned deferral, not a build record — update it (or file the
  follow-up as its own dated PLAN.md entry) when the runtime is actually implemented.

## Alternatives considered

- **Publish Reclaim to PyPI, tell installer users to `pip install reclaim[ai]` from a system
  Python.** Rejected: still requires the user to have Python/pip at all (most non-technical
  Windows users don't), and produces two separate installations of the same app to reconcile.
- **Bundle `[ai]` directly into the Nuitka installer.** Already rejected by ADR-0024 (~1GB
  disproportionate installer cost, forces the AI startup tax on every install).
- **Ship it now, rushed, alongside Workstream C.** Rejected — see "Why this isn't built yet"
  above. The safety-boundary rigor this codebase has applied to every prior AI-adjacent change is
  not optional just because a deadline-shaped feature list wants it done.
