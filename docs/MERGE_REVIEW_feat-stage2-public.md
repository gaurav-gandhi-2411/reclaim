# Merge review: `feat/stage2-public` → `main`

**Status: NOT MERGED.** This document is the review artifact — merge is GG's explicit call
after reading it. Branch has no remote (local-only, never pushed). 3 commits, 32 files changed
(31 in Parts A+B, +1 for Part C's PLAN.md-only update), +2,090/−67 lines. Tested via a
disposable local branch (`merge-preview-stage2`, `git merge --no-ff feat/stage2-public` onto
`main`) — never touched real `main`. Clean merge, **zero conflicts**.

## What this branch adds

Stage 2: turns Reclaim from "a CLI tool you clone and run yourself" into a double-click Windows
installer aimed at people who won't read the source first.

- **Part A (ADR-0023):** safe mode as the structural default for every fresh install —
  Recycle-Bin-only deletes, review-only (Tier B) candidates, `duplicates`/`model_caches`/
  `dev_artifacts` forced off, all independent of `config.toml`. Power mode is a typed,
  exact-match, logged, reversible-without-confirmation opt-in.
- **Part B (ADR-0024):** a Nuitka `--standalone` + Inno Setup Windows installer
  (`packaging/`), a core-only AI-bundle decision (measured, not guessed), and a LICENSE (MIT +
  data-deletion disclaimer).
- **Part C:** signing options reported (Azure Trusted Signing vs. unsigned) — no spend, no
  code change, decision left to GG.

## 1. Safety gate — re-verified against the branch merged onto current `main`, not in isolation

`evals/test_safe_mode_gate.py` — **18/18 passed** at the merged commit:
- `test_safe_mode_never_produces_vault_or_direct_delete_method`,
  `test_apply_batch_refuses_non_recycle_bin_method_in_safe_mode_before_any_io`,
  `test_safe_mode_apply_never_calls_direct_delete_or_vault_primitives` — the recycle-bin-only
  guarantee, including the integration-level proof (`os.unlink`/`shutil.rmtree` monkeypatched to
  raise during a real `apply=True` batch).
- `test_purge_expired_refuses_unconditionally_in_safe_mode` — purge closed even for a machine
  with real vault entries from an earlier power-mode session.
- `test_dangerous_categories_forced_off_regardless_of_config_toml` — guarantee 2.
- `test_every_candidate_forced_to_tier_b_in_safe_mode` — guarantee 3.
- `test_power_mode_rejects_anything_but_the_exact_phrase` (6 parametrized near-misses: empty,
  "yes", "I agree", wrong case, trailing space, one character short) +
  `test_power_mode_accepts_the_exact_phrase` — typed confirmation is the only door.
- `test_safe_mode_reversion_never_requires_confirmation`,
  `test_mode_defaults_to_safe_when_no_log_exists`, `test_mode_persists_across_multiple_switches`,
  `test_mode_log_survives_a_fresh_read_not_just_the_in_memory_return_value`.

`evals/test_ai_safety_gate.py` — **21/21 passed** at the merged commit, confirming this branch
doesn't weaken the pre-existing applied-AI recommend-only boundary (this branch doesn't touch
`src/reclaim/ai/**` at all — zero overlap, confirmed by the diffstat above).

## 2. Both install profiles — confirmed against the merged state

**Core-only** (existing isolated scratch venv, editable install tracking the merged tree):
- `import reclaim.cli` succeeds cleanly.
- `reclaim.ai._optional.require("lightgbm", ...)` raises `AIExtraNotInstalledError` with the
  actionable message ("Install the AI extras: `uv sync --extra ai`...") — never a raw
  traceback.

**`[ai]`-extra profile** (existing isolated scratch venv, `uv pip install -e ".[ai]"`):
`torch`, `open_clip`, `sentence_transformers`, `lightgbm==4.7.0` all import cleanly.

**Packaged-artifact profile** (this branch's actual new surface — a compiled binary, not a
`uv`/`pip` install): built from the core-only venv specifically (Nuitka's static analysis can't
follow `reclaim.ai`'s dynamic `importlib.import_module` calls anyway, but building from a venv
that structurally cannot have AI deps makes the "core-only installer" guarantee airtight rather
than incidental). `packaging/test_packaged_safe_mode.ps1` — **13/13 checks passed**, run twice:
once against the raw `entry_point.dist/reclaim.exe`, once through a real silent
install → run → silent uninstall cycle of `reclaim-setup.exe` (`PrivilegesRequired=lowest`, zero
admin prompts at any step). Confirmed separately: the installed `reclaim.exe serve` answers
`GET /` with HTTP 200 on `127.0.0.1:8420`; uninstall removes all installed files but correctly
leaves runtime-created `data/` behind. Packaging source files are unchanged by the merge (no
conflicts touched `packaging/`), so this evidence — gathered pre-merge against
`feat/stage2-public` directly — still applies unmodified to the merged state.

## 3. Packaging choices (ADR-0024 summary — see the ADR for full reasoning)

- **Nuitka `--standalone` + Inno Setup**, not Briefcase MSI. `cli.py`'s
  `uvicorn.run(app, ...)` already avoids the most common Nuitka+FastAPI failure mode (a dynamic
  `"module:app"` string import); Briefcase's FastAPI path is an immature 2026Q2 feature for a
  different app shape (embedding a site in native window chrome) and needs WiX besides.
- **Core-only installer.** Measured: core `site-packages` 13.6 MB vs. 1,041.8 MB with `[ai]`
  extras (`torch` alone 464 MB, shared by Feature 1b's document dedup *and* Track B's CLIP
  grouping — no clean "light vs. heavy AI" split exists, disclosed rather than silently
  engineered around). AI stays a documented `pip install reclaim[ai]` post-install path.
  Installer: **18.2 MB**.
- **Unsigned**, per Part C — Azure Trusted Signing (~$9.99/mo) reported as an option but not
  purchased; decision left to GG.

## 4. Harness invariants — confirmed running and passing at branch head

The four pre-existing invariants (hard-tier rejection, per-tier no-pooling, version-chain
conditional-keeper, near-empty-OCR→UNKNOWN) are untouched by this branch (zero diff under
`src/reclaim/ai/**`) and pass as part of the standard `pytest` sweep below.

## Residual risks, disclosed

- **Detection logic is not made more correct by any of this** — safe mode is a provable
  boundary on *how* a mistake can manifest (Recycle-Bin-recoverable, always human-confirmed),
  not a claim that every rebuildable-category detector is perfect for every unmodeled
  environment. Same "provably safe boundary, not provably correct detection" distinction §7.5
  already draws for the AI layer (ADR-0023's own stated consequence).
- **No working path today for "enable AI on a Nuitka-installed `reclaim.exe`"** — the compiled
  binary is not a `pip`-installable environment. A user who wants the AI layer needs a separate
  `pip install reclaim[ai]` from source/PyPI, not an in-place upgrade of the installed exe.
  Disclosed in ADR-0024, deferred to a future installer-README "power users" section.
- **Unsigned binary** — expect a SmartScreen/AV prompt on first run; this project has already
  hit one AV/quarantine false positive on a freshly-built unsigned binary during earlier
  testing (noted in the original Stage 2 brief, not reproduced again this session but not
  assumed away either).
- **No real-disk validation of the packaged installer** — all testing this session used
  disposable fixture trees (a synthetic `node_modules`/`package.json` pair), never GG's real
  disk, consistent with the project's standing "never scan/modify GG's real disk without
  explicit sign-off" rule.
- **Tooling gotcha, not a product risk**: this session's dedicated PowerShell tool session
  became unresponsive partway through (every command returned exit 1 with zero output, cause
  undiagnosed from inside the session) — worked around via `powershell.exe`/`pwsh` invoked as a
  subprocess through Bash for the remainder of verification. Noting this so a future session
  doesn't waste time re-diagnosing the same symptom as a code bug.
- **Merge-preview branch left for GG's review**, not deleted: `merge-preview-stage2` (this
  review's disposable test branch) — safe to delete once this document has been read, per the
  project's standing "never delete branches unasked" rule.

## Verification commands (all run against the merged-onto-main state, `merge-preview-stage2`)

```
uv run ruff check .                                  # PASS
uv run ruff format --check .                         # PASS (127 files)
uv run mypy                                          # PASS (50 source files, strict)
uv run pytest --cov --cov-report=term-missing -q     # 563 passed, 2 skipped, 95.19% coverage
uv run pytest evals/test_safe_mode_gate.py -v        # 18 passed
uv run pytest evals/test_ai_safety_gate.py -q        # 21 passed

# Core-only / [ai]-extra profiles (isolated scratch venvs, editable install tracking this tree)
python -c "import reclaim.cli"                                          # OK, core-only
python -c "from reclaim.ai._optional import require; require('lightgbm', feature='x')"
    # -> AIExtraNotInstalledError, actionable message, core-only venv
python -c "import torch, open_clip, sentence_transformers, lightgbm"    # OK, [ai]-extra venv

# Packaged artifact (built pre-merge from feat/stage2-public; packaging/ unchanged by the merge)
uv run python -m nuitka --standalone --assume-yes-for-downloads ... packaging/entry_point.py
"C:\Program Files\Inno Setup 7\ISCC.exe" packaging\reclaim.iss
powershell -File packaging\test_packaged_safe_mode.ps1 -DistDir packaging\build\entry_point.dist
    # 13 passed
# ... repeated against a real silent install/run/uninstall cycle of reclaim-setup.exe — 13 passed
```

---

**Merge is not performed by this pass.** Everything above is evidence for GG's own review — the
decision to merge `feat/stage2-public` into `main` is his explicit call.
