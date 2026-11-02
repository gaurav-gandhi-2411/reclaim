# README numbers audit (1.4.0 release prep)

Every number in `README.md` as of this branch, the source that backs it, and what was done. Rule
applied: a number stays only if a file in the repo (code constant, report, ADR, runbook) supports it,
and the citation sits next to the number in the README. No number was invented or estimated; where
no source exists the number was removed or replaced with non-numeric wording.

SHAs are the last commit that touched the cited line (`git blame -L`), or, where a whole file is
the source, the last commit that touched that file (`git log -1 -- <file>`), on this branch. "Dev-tree value" means the number is a constant in the
code, not a measurement of anything.

Method: `grep -n "[0-9]" README.md` over the whole file (every line containing a digit; image
`src` lines were skipped because they only hold screenshot file names), then each hit classified.
Covered: prose, code blocks and comments in code blocks. Not covered: the content of linked docs.

## Numbers that make a claim

| # | README text (short) | Source | Action |
|---|---|---|---|
| 1 | "Windows 10 and 11" (Download line) | `packaging/RELEASE_RUNBOOK.md` ("Windows 10 (1607+, i.e. every supported version) and Windows 11"), `c0ad89f` | Cited |
| 2 | "temp files untouched for 7+ days" (weekly clean) | `DEFAULT_MIN_AGE_SECONDS = 7 * 24 * 3600.0`, `src/reclaim/regenerable.py:48`, `6f1379e` (dev-tree value) | Cited |
| 3 | "Sundays, 10:00" (weekly clean) | `src/reclaim/autoclean_schedule.py:49` (`_START_BOUNDARY = "2026-01-04T10:00:00"`, a Sunday) and `:135` (`<DaysOfWeek><Sunday />`), `2b08812` (dev-tree value) | Cited |
| 4 | "newest file over 7 days old" (pytest temp) | `PYTEST_TEMP_MIN_AGE_SECONDS = 7 * 24 * 3600.0`, `src/reclaim/regenerable.py:53`, `67925c6` (dev-tree value) | Cited |
| 5 | "30-day" vault retention window | `docs/architecture/adr/0001-category-tiered-retention.md` ("`retention = 30` (days), the default for everything else"), `bead7a0`; defaults in `src/reclaim/config.py` | Cited |
| 6 | "CLIP fp16, 175.8MB" | `reports/ai/onnx_quality_parity/payload_sizes.json` (`fp16_onnx: 175784030` bytes, measured 2026-07-30), `8ca59fb`; also ADR-0030 | Cited, exact byte count added. The runbook's "167.6 MB" is the same file in MiB (175,784,030 / 1,048,576 = 167.6); the README now says so, so the two figures no longer look contradictory |
| 7 | "MiniLM int8, 23.6MB" | same file (`minilm_full_pipeline.int8_onnx_dynamic: 23600000`), `8ca59fb`; also ADR-0030 | Cited |
| 8 | "199.4MB total" | same file (`recommended_bundle.total_new_payload_mb: 199.4`), `8ca59fb` | Cited |
| 9 | "~1,028MB delta" (old torch-based `[ai]` extra) | same file (`vs_current_torch_ai_extras_delta_mb: 1028.0`), `8ca59fb`; ADR-0030 says "measured at ~1,028MB delta" | Cited. It is a 2026-07-30 measurement of a dependency set that no longer ships |
| 10 | "~$9.99/month" (Azure Trusted Signing) | `docs/architecture/adr/0031-code-signing-approach.md` (Basic tier, $9.99/mo), `8ca59fb` | Cited. A vendor price quoted in the ADR, not a Reclaim measurement; it can change |
| 11 | build-instructions comment "clip_vision_fp16.onnx (175.8MB ...)" | same JSON as row 6 | Cited, replaced with the exact byte count |
| 12 | First run example "Temporary & browser cache files — 890 MB" | none: an illustrative figure, no report holds it | **Removed** (the step was rewritten without a figure) |
| 13 | "GitHub's 100MB plain-git limit" | a GitHub platform limit, not measured in this repo | **Reworded** to "GitHub's plain-git per-file limit" (the file is shown at 175,784,030 bytes, which is over it) |

Totals by table row: 13 rows, **cited 11, removed 1, reworded 1**. Rows 6 to 9 are four separate
numbers taken from one file, so counted per number the totals are cited 14, removed 1, reworded 1
(16 number instances).

## Left as is, not measurements

These contain digits but make no quantitative claim a measurement could back:

- Version and stage labels: `v1.0.0`, `v1.2.0`, "Stage 2", "Wave 1 P0-B", "1.4.0" headings.
- ADR numbers and names (`ADR-0001`, `-0003`, `-0023`, `-0024`, `-0030`, `-0032`, `-0034`,
  `-0040`, `-0043`, `-0044`); the two new links were checked to resolve to existing files.
- Network and CLI literals: `127.0.0.1`, `::1`, `0.0.0.0`.
- Numbered list steps in "First run".
- "within minutes" (build monitor): no digit, not a number. The actual stall timeout is
  `[int]$StallTimeoutSeconds = 300` in `packaging/build_installer.ps1:38`, `8ca59fb`; the README was
  not given a number.
- Screenshot file names `01-` to `11-` inside image URLs.

## Stale statements corrected while auditing (no number involved)

Found because they sat next to numbers, and they would have made the README wrong at 1.4.0. Each
was checked against code on this branch.

1. First run said "Clean My Computer" scans every fixed drive and then offers "Clean now". On this
   branch it is a one-click clean of the fixed regenerable list with no scan first
   (`src/reclaim/api/static/app.js`, `startSimpleOneClick`, `renderSimpleGroups`), a separate
   **Scan my files for more to review** scans the user's own files, and the whole-drive scan is a
   separate confirmed opt-in (PRs #47, #65).
2. The intro and the safe-mode bullet said every delete goes to the Recycle Bin; the one-click and
   weekly cleans use each tool's own cleaning command (ADR-0034, `docs/HOWTO.md` section 2). The
   exception is now stated.
3. "No ML" in the developer section contradicted the AI suggestions and the bundled ONNX models;
   reworded to "recommend-only".
4. The AI Suggestions screenshot caption said the installer does not bundle the AI layer (true of
   v1.3.0, false since ADR-0030). The caption now says the screenshot is from v1.3.0 and has not
   been retaken.
5. "aren't retaken this release" referred to a past release; now "have not been retaken since".

Added: a "New in 1.4.0" section with no numbers.

## Not done

- The AI Suggestions screenshot (`docs/assets/screenshots/10-ai-suggestions.png`) is not retaken;
  that needs a built 1.4.0.
- The README's claims about the installer (no admin prompt, per-user, SmartScreen text) were not
  re-measured here; they are behaviours, not numbers, and the release checklist's VM gate covers them.
