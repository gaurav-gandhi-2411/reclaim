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
- **The log file** (see below) — or, easiest, click **"Copy diagnostics"** in the dashboard
  header and paste the result into the issue.

**Never post file contents, full file listings, or your scan index (the SQLite file under
`data/`).** Maintainers will never ask you for these — Reclaim's entire design goal is that
nothing about your files needs to leave your machine (see [PRIVACY.md](PRIVACY.md)), and a bug
report shouldn't be the exception. A description of what happened is always enough.

## Where the log file is

Reclaim keeps a rotating log file at **`data\logs\reclaim.log`**, relative to wherever the app's
`data\` folder lives — same convention as the scan index, quarantine vault, and mode log (see
[PRIVACY.md](PRIVACY.md)). For the installed app, that's the install folder itself (every
shortcut's working directory points there); for a source checkout, it's `data\logs\reclaim.log`
under the repo root you're running from.

Every subcommand (`scan`, `apply`, `purge`, `undo`, `mode`, `serve`, `dashboard`) writes to this
file, capped at 5 MB with 5 rotated backups (30 MB total) so it can never grow unbounded. Like
everything else Reclaim logs, it contains file **paths**, counts, and error messages — never
file contents or extracted text (OCR/document text is never logged at all; see
[PRIVACY.md](PRIVACY.md)).

The fastest way to grab it: open the dashboard and click **"Copy diagnostics"** in the header
(next to "How this works"). It copies the app version, current mode, whether the AI extra is
installed, and the log file's recent tail — paste that straight into a GitHub issue. Prefer to
attach the file itself instead? It's plain text, at the path above.

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
