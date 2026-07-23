# 0024. Stage 2 Part B: installer tooling and AI-dependency bundling decision

## Context

Stage 2 Part A (ADR-0023) made safe mode a structural default. Part B turns the tool into a
double-click Windows installer. Two decisions were open: which packaging toolchain to use, and
whether the public installer should carry the applied-AI layer's dependencies (some of which —
`torch`, `open-clip-torch`, `faiss-cpu` — are GB-class) or ship core-only with AI as a
documented post-install upgrade.

## Decision 1: Nuitka one-folder + Inno Setup, not Briefcase

**Chosen: extend the existing `uv`-managed project with a Nuitka `--standalone` build, wrapped
in an Inno Setup installer script.**

Rationale, in the order that mattered:

- **`reclaim`'s own uvicorn call site is already Nuitka-shaped.** `cli.py::_run_serve` calls
  `uvicorn.run(app, host=..., port=...)` with a real `FastAPI` object, not the
  `uvicorn.run("module:app_string")` form. The latter relies on a dynamic import Nuitka's static
  analysis cannot resolve and is the single most commonly reported Nuitka+FastAPI failure mode
  ([Nuitka#821](https://github.com/Nuitka/Nuitka/issues/821),
  [Nuitka#1063](https://github.com/Nuitka/Nuitka/issues/1063)); this codebase was never going to
  hit it. De-risks the highest-probability failure before writing a single packaging line.
- **Briefcase is built for Toga GUI apps, not a background FastAPI service with a browser-based
  dashboard.** Briefcase's FastAPI-serving path (via Toga's Positron plugin) is a 2026Q2
  feature — new this year, targeted at embedding a website inside a native window chrome, not
  at "run a local server, open the user's own browser" (which is what `reclaim dashboard`
  already does). Briefcase's Windows MSI output also requires the WiX Toolset as an additional
  build dependency Nuitka+Inno doesn't need.
- **Nuitka standalone mode is a mature, widely-used path for exactly this shape of app**
  (CLI/service + FastAPI backend + static/template assets), with well-documented flags for the
  failure modes that do exist (`--include-package`, `--include-data-dir`).
- **Inno Setup** is the de facto standard free Windows installer builder — scriptable, silent-
  install-capable (`/VERYSILENT`), and needs no additional runtime on the target machine (unlike
  MSIX/WiX-based paths, which have their own OS-version and signing quirks).

Installed via the project's normal channels — `nuitka` added as a `uv add --dev` entry
(`pyproject.toml`, `[dependency-groups].dev`); Inno Setup 7.0.2 (x64) installed from its official
GitHub release (`jrsoftware/issrc`), silently, system-wide (build-machine tooling, not a project
dependency — not committed, not part of any shipped artifact).

## Decision 2: core-only public installer; AI layer stays a documented post-install extra

**Chosen: the public installer ships the deterministic engine only (no `[ai]` extras). The AI
layer remains available via `pip install reclaim[ai]` / `uv sync --extra ai` for anyone who
wants it — no second installer artifact for now.**

### Measured evidence (this session, commit at HEAD of `feat/stage2-public`)

Two clean `uv venv` + `uv pip install` scratch environments (no dev-tool packages, so the
numbers are shippable-dependency-only, not contaminated by pytest/mypy/ruff/pyarrow/ollama):

| Profile | Command | Measured `site-packages` size |
|---|---|---|
| Core-only | `uv pip install -e .` | **13.6 MB** |
| With AI extras | `uv pip install -e ".[ai]"` | **1,041.8 MB** |

Delta from enabling `[ai]`: **~1,028 MB**. Per-package breakdown of the largest contributors
(measured via `Get-ChildItem -Recurse | Measure-Object -Sum Length` against the same install):

| Package | Size |
|---|---|
| `torch` | 464.3 MB |
| `cv2` (opencv, headless+full both resolve into the same import name) | 112.4 MB |
| `faiss_cpu.libs` | 49.5 MB |
| `onnxruntime` | 37.8 MB |
| `numpy` | 23.1 MB |
| `torchvision` | 14.0 MB |
| `faiss` | 14.3 MB |
| `PIL` | 15.1 MB |
| `rapidocr_onnxruntime` | 15.6 MB |
| `lightgbm` | 4.7 MB |
| `sentence_transformers` | 3.7 MB |
| `open_clip` + `open_clip_train` | 2.2 MB |

`torch` alone is 45% of the entire AI-extra delta. It is a shared transitive dependency of
`sentence-transformers` (Feature 1b, document near-dup) **and** `open-clip-torch` (Track B,
semantic image grouping) — not just Track B.

### The "lightweight AI vs. heavy semantic AI" split doesn't cleanly exist

The instruction framing this decision assumed core-installer AI could mean "core + lightweight
image/doc dedup" with only Track B's semantic grouping held back as heavy/optional. Measured
reality doesn't support that split: Feature 1b (document near-dup + version-chain, *not*
semantic grouping) already pulls in `sentence-transformers` → `torch`, the same 464MB dependency
Track B needs. A real three-way split (core / non-torch AI ≈ 210MB [Feature 1a image-hash dedup
+ Feature 2 OCR + Feature 3 ranker] / torch-anchored AI ≈ 550MB+ [Feature 1b + Track B]) is
possible but would mean restructuring the single `[ai]` extra in `pyproject.toml` into two or
three extras — a real design change, not a packaging detail, and not undertaken here. Flagging
this as a disclosed, deferred option rather than silently picking a finer split unasked: if GG
wants a `reclaim[ai-light]`/`reclaim[ai-semantic]` split later, this measurement is the starting
point.

### Why core-only, not full bundle

- **Every AI feature is recommend-only or browse-only by construction** (§7.5, ADR-0011,
  ADR-0022) — none of it is required for the tool's core value proposition (freeing disk space
  via the deterministic engine). A stranger's first install does not need it to get value.
  duplicates/model_caches/dev_artifacts are already force-disabled in safe mode (ADR-0023)
  regardless, further shrinking the day-one relevance of the AI layer for a fresh install.
- **~1GB is a disproportionate cost for a "clean up your disk" tool's own installer** — the
  installer itself would be a meaningful fraction of what a typical cleanup run reclaims from a
  lightly-cluttered machine.
- **Anyone installing `reclaim[ai]` is already comfortable with `pip`/`uv`** — the tool's own
  `uv`-based dev workflow already documents this path; no new tooling needs to be built for
  power users, just documentation (README "Enabling the AI layer" section, to be added when the
  installer README lands).
- Both profiles are already CI-gated today (`ci.yml`'s `lint-and-test` job runs without
  `--extra ai`; `ai-layer-with-extras` runs with it) — core-only was never an untested path.

## Consequences

- The public installer's `reclaim.exe` cannot exercise any AI-layer feature out of the box;
  first-run/dashboard copy must not imply otherwise. `reclaim.ai.*` modules already degrade to a
  clear `AIExtraNotInstalledError` (`reclaim/ai/_optional.py`) rather than a raw traceback if
  ever reached without the extra — this already covers the "installer has no AI deps" case
  correctly, no code change needed for this ADR.
- A user who wants the AI layer must run `pip install reclaim[ai]` against the same Python the
  installer's `reclaim.exe` uses — except the installer ships a **compiled** Nuitka binary, not
  a Python environment `pip` can install into. This is a real, disclosed gap: today, "enabling
  AI on a Nuitka-installed `reclaim.exe`" has no working path. Resolving it (e.g., a documented
  `pip install reclaim` from PyPI as the alternative for AI-layer users, with the compiled
  installer positioned as the zero-Python-required fast path) is deferred to the installer
  README, not solved by this ADR — recorded here so it isn't silently forgotten.
- No `pyproject.toml` extras restructuring in this change. `[ai]` stays a single extra.

## Alternatives considered

- **Full bundle (ship `[ai]` in the installer).** Rejected on the ~1GB-for-a-cleanup-tool
  argument above, and because it would force every fresh install through the AI layer's own
  heavier startup/model-load cost even for users who never touch an AI-suggested review queue.
- **Split `[ai]` into `ai-light`/`ai-semantic` now, ship `ai-light` in the installer.** Rejected
  for this change as scope creep beyond what was asked (a packaging decision, not an extras
  redesign) — recorded above as the natural next step if finer AI tiering is wanted later.
- **Briefcase MSI.** Rejected — see Decision 1.
