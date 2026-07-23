# Support

## Reporting a bug

Open an issue on [GitHub Issues](https://github.com/gaurav-gandhi-2411/reclaim/issues). To help
diagnose it quickly, please include:

- **Reclaim version** — shown in the dashboard footer ("Reclaim v...").
- **Windows version** (e.g. Windows 11 23H2) — `winver` if you're not sure.
- **What kind of folder you scanned** — e.g. "my Downloads folder" or "a project directory with
  several node.js repos." Describe the *kind* of location, not the actual path, if the path is
  personal or sensitive.
- **The plain-language group involved** — the category card or Review Queue entry name (e.g.
  "Dev Artifacts," "Browser & Temp Caches").
- **The exact error text**, copied verbatim, not paraphrased.
- **Steps to reproduce**, as precisely as you can manage.

**Never post file contents, full file listings, or your scan index (the SQLite file under
`data/`).** Maintainers will never ask you for these — Reclaim's entire design goal is that
nothing about your files needs to leave your machine (see [PRIVACY.md](PRIVACY.md)), and a bug
report shouldn't be the exception. A description of what happened is always enough.

## If Reclaim deleted something it shouldn't have

1. **Check the Recycle Bin first.** In safe mode (the default), every delete goes there —
   restoring is the same as recovering anything else you deleted (right-click -> Restore). See
   the README's ["How to restore something"](README.md#how-to-restore-something) section.
2. If the file isn't recoverable, or Reclaim proposed deleting something it clearly shouldn't
   have, **file an issue** and mark it with the plain-language category/group that proposed it
   (e.g. "Dev Artifacts wrongly flagged X") — that's the fastest way to point at the exact
   detection rule that needs fixing.

## What maintainers can and can't see

There is no server side to this project — see [PRIVACY.md](PRIVACY.md). The author has no
visibility into what you scanned, what was deleted, or that you exist as a user unless you
choose to file an issue. Bug reports are entirely voluntary.
