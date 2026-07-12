# Reclaim

Rules-first Windows disk-cleanup tool. Deterministic detection for provably-safe
categories, a hard safety gate that runs before any candidate is generated, and fully
recoverable actions (`send2trash` + manifest, dry-run by default). See `reclaim-spec.md`
for the full design and `PLAN.md` for build status.

## Quick start

```powershell
uv sync
uv run pytest
```

CLI and dashboard entry points land in later build stages — see `PLAN.md` for status.

## Status

Phase 1 (deterministic engine) in progress. Nothing here scans or modifies a real disk yet;
all development runs against synthetic fixtures in `evals/fixtures/`.
