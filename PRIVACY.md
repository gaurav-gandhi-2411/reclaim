# Privacy

Reclaim is a disk-cleanup tool: it reads your filenames, file metadata, and — for some optional
AI features — file *content* (text extraction from documents, OCR of screenshots). A tool with
that level of access owes you a precise answer about where that data goes. Here it is:

## Everything stays on your machine

- **No telemetry. No analytics. No crash reporting to any server. No account, ever.**
- **No network communication in the core product, with one narrow opt-in exception.** The
  scanner, detectors, deduplicator, and executor make zero outbound network calls, always. The
  dashboard also makes zero outbound network calls by default; the one optional exception is the
  update check described below, which stays off unless you turn it on. The dashboard is a local
  web page served only to your own machine (`127.0.0.1` — hard-enforced; binding to a
  network-reachable address is a startup error, not a setting).
- **Scan results stay local.** The scan index (a SQLite file), the quarantine vault, the
  manifest, mode logs, the application log (`data/logs/reclaim.log` — see SUPPORT.md), and all
  AI analysis results live under Reclaim's own `data/` folder on your disk. Nothing is uploaded,
  anywhere, at any time. The application log contains file **paths**, counts, and error
  messages only — never file contents (same guarantee as below).
- **AI features run locally.** Image similarity, document comparison, OCR, and ranking all
  execute on your CPU against your files. Extracted text and OCR output are never written to
  logs — this is enforced by automated tests, not just policy (a canary-string test asserts
  OCR'd content appears in zero log records).

These claims are verifiable in the source: `src/reclaim/update_check.py` (the opt-in update
check below) is the only module under `src/reclaim/` that imports an HTTP client, and it is
never reached unless `config.toml` explicitly sets `[update_check] enabled = true` — the core
scan/detect/dedup/execute pipeline still makes zero outbound network calls, unconditionally. We
verify this as part of our own audits, and you can too — the code is public.

## The network exceptions, disclosed precisely

The **optional AI component** (`pip install reclaim[ai]` — not included in the standard
installer) **downloads open-source model weights the first time an AI feature runs**:

- CLIP ViT-B/32 (~338 MB, for semantic image grouping) — downloaded from the model host by the
  `open_clip` library
- all-MiniLM-L6-v2 (~90 MB, for document similarity) — downloaded by `sentence-transformers`

These downloads fetch *models to your machine*. **Your files, filenames, and scan results are
never part of any request** — the download is a plain fetch of public model files, the same as
a browser download. After the first download the models are cached locally and no further
network access occurs. The OCR engine's models ship inside the package itself (no download).

If you install only the standard installer (the default), none of this applies — there is no
AI component and no network access at all.

## Updates

By default, Reclaim does **not** check for updates automatically — no phone-home, not even a
version ping. The dashboard's "Check for updates" link simply opens the public GitHub Releases
page in your own browser; nothing is sent beyond that ordinary page visit, which you initiate.

**Optional, opt-in automatic check:** setting `enabled = true` under `[update_check]` in
`config.toml` turns on a background check for a newer release. When enabled, the precise
behavior is:

- One plain, anonymous `GET` request to GitHub's public releases API for this repo's latest
  release tag — the same kind of request a browser makes visiting the public releases page.
  **Your files, filenames, scan results, and any other Reclaim data are never part of this
  request** — it carries nothing but the request itself.
- Cached for the running dashboard process (roughly once a day for a long-running `reclaim
  serve` session) — not on every page load.
- A short (2-3 second) timeout, no retries, and wrapped so any failure (offline machine, DNS
  failure, GitHub outage, unexpected response) degrades to "couldn't check" — it can never slow
  down or block anything else in the dashboard.
- Off by default. Turning it on is a deliberate, explicit config change; the standard installer
  ships with it off, and no update check of any kind happens unless you opt in.

## What we can't see

Everything. There is no server side. The author has no way to know you installed Reclaim, what
you scanned, what was deleted, or that you exist as a user. Bug reports are voluntary, through
[GitHub Issues](https://github.com/gaurav-gandhi-2411/reclaim/issues) — see SUPPORT.md for
what diagnostic information is useful (never file contents).

---

*This statement describes Reclaim as of the version it ships with, and is kept in the
repository so any change to it is visible in version control history. If a future feature ever
needed network access beyond the model-download exception above, it would be opt-in and this
document would say so before the feature shipped.*
