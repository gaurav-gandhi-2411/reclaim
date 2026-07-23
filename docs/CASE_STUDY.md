# Reclaim: what it costs to let an agent delete files

Repo: [github.com/gaurav-gandhi-2411/reclaim](https://github.com/gaurav-gandhi-2411/reclaim)

Reclaim is a Windows disk-cleanup tool: scan a drive, propose what's safe to remove, delete it.
That one-line description hides the actual engineering problem. An agent that deletes files for
a living has exactly one job that matters more than finding space to reclaim: never destroying
something the user needed, and never lying about what it did. Every mechanism in this codebase —
the safety validator, the dry-run default, the retention tiers, the reclaimable-size accounting —
exists because a wrong number is embarrassing and a wrong delete is unrecoverable. This is the
record of what it actually took to earn that guarantee against a real, 3.1-million-file disk in
active daily use, not a fixture tree.

The honest version of this thesis is not "the safety system worked." It's narrower and less
comfortable than that: every individual safety mechanism in this codebase was defeated at least
once, by a real filesystem shape none of its tests modeled — and the project is still standing
only because the one operation that actually ran destructive was chosen to be reversible before
anyone knew it would need to be. That is not a triumphant ending. It's the accurate one.

What follows is the deterministic engine (rules, hashes, a real 3.1M-file disk), a security
audit of the review UI itself, an applied-AI layer built safety-first across four features plus
a cross-LLM-labeled ranker, and — the final chapter — a public installer, because the moment a
tool like this stops being watched by the person who wrote it, "the safety system worked so far"
stops being an answer anyone else can verify for themselves. **The residual-risk class is not
closed, and this document says so explicitly rather than claiming otherwise**: safe mode is a
structural boundary on *how* a mistake can manifest — Recycle-Bin-recoverable, always
human-confirmed — not a claim that every detector's judgment is correct for every environment
this project hasn't seen yet. That distinction, stated plainly instead of papered over, is the
actual credential this project is offering.

## Architecture

- **Rules-first.** No ML in the deletion path. Every candidate comes from a deterministic
  detector (path pattern, extension, byte-identical hash) or an explicit heuristic — nothing is
  ever proposed because a model scored it "probably junk."
- **SafetyValidator-first.** Every candidate, from every detector, passes through one
  deny-first validator before it can be tagged for any tier — protected roots, git repositories,
  protected extensions, database/VM files, Docker/WSL roots, cloud-sync placeholders. `BLOCKED`
  means excluded entirely, not degraded to a lower tier.
- **Dry-run by default.** `apply=False` makes zero filesystem calls — no moves, no deletes, no
  manifest writes. `--apply` is the only way anything on disk changes, ever.
- **Atomic, recoverable moves.** Vault and Recycle Bin quarantine are both real filesystem
  operations with parity checks before the source is ever removed — a partial move never leaves
  an orphaned half-copy and an intact source, or vice versa.
- **Tiered retention.** Permanence is a property of the *category*, not the run: rebuildable
  caches (`dev_artifacts`, `package_caches`, `temp_and_browser_caches`, `crash_dumps`) delete
  permanently because their real recovery path was always the rebuild command, never the vault;
  everything else — including every exact-duplicate candidate — is recoverable by construction.
- **Hardlink- and structure-aware reclaimable accounting.** Byte-identical content is not the
  same as reclaimable space. A "duplicate" can be a hardlink to its own keep copy (0 bytes
  freed), a hardlink to a shared blob (0 bytes freed), or a stdlib file inside a shared Python
  interpreter build that a dozen unrelated projects depend on (deleting it breaks all of them,
  regardless of what bytes it shares with something else).
- **Safe mode by default for anyone who isn't the author.** The public installer (Nuitka +
  Inno Setup, core-only, 13.6MB) ships with a structural boundary, not a convention: Recycle-
  Bin-only deletes, review-only candidates, and the highest-risk categories force-disabled,
  independent of any config file — proven against the actual compiled binary, not just the
  source tree, before it was allowed to be called done.

## The honesty arc — the centerpiece

The single throughline of this project is how many times the "how much can we reclaim" number
turned out to be wrong, and specifically wrong in the direction of overclaiming — never once
did a correctness fix increase the estimate.

| stage | exact_duplicate reclaimable estimate | what changed it |
|---|---|---|
| first real-disk dry run | ~48GB (logical size, unexamined) | baseline: sum of every non-kept cluster member's size |
| ADR-0006 | 23.09GB | hardlink-aware accounting — a "duplicate" sharing blocks with its own keep copy reclaims 0, not its full size |
| ADR-0008 | 4.26GB | model-cache and conda/venv-environment exclusion — byte-identical stdlib/model files inside a structurally-managed cache or a live environment are never safe to treat as arbitrary duplicates, regardless of what they share on disk |
| real apply (same day, natural disk drift) | 4.24GB selected | not a correction — the live disk had changed slightly between estimate and apply time |
| ADR-0009, after the real apply | 3.92GB, net of 186 restored files | a live incident found the ADR-0008 exclusion didn't cover *standalone* Python installations (no `conda-meta/`, no `pyvenv.cfg`) — closed, verified against the entire applied batch |
| ADR-0010, follow-up | unchanged number, tightened guarantee | root-caused *why* marker-only detection missed all three incident installs (none is conda or venv) and replaced it with structural detection by default — re-verified against the same 10,134-file batch: still exactly 186 violations, confirming ADR-0009's recovery was already complete, not that a fourth incident was hiding |

Each drop is a correctness fix, not a change of heart about what counts as "reclaimable." The
last two happened *after* the apply had already run for real — see below.

## The 11-bug trail

Four themes, each with real evidence a fixture-only test suite could not have produced.

**Observability — a silent hang looks exactly like a hang.**
1. The first real-disk dry run stalled with zero output for over 10 minutes: no heartbeat, no
   incremental hash-cache writes, no per-file read timeout. A single locked file, or simply a
   long-running hash pass, was indistinguishable from a crash. Fixed with a 5-second heartbeat,
   500-file write flushes, and a 30-second per-file timeout that converts a stuck read into a
   recorded skip instead of a wedge. Every unit test was green — none of them ran against enough
   files, or a long enough pass, to notice silence itself was the bug.

**Scalability — four separate ways "works on 500 fixture files" doesn't mean "works on 3.1M
real ones."**
2. Candidate generation materialized the *entire* scan index into `FileRecord` objects before
   any detector ran — 21+ minutes and ~4.9GB RSS with zero rows hashed. Fixed by pushing every
   detector query down into indexed SQL (ADR-0002); verified via `EXPLAIN QUERY PLAN`, not
   assumption.
3. The dedup pipeline did the same thing one level down: it collected an entire duplicate-size
   candidate bucket into memory before hashing a single file. Fixed by processing one size
   bucket at a time (bounded by the largest single bucket, not the total candidate count).
4. `direct_children()`'s `LIKE ? ESCAPE '\'` query silently defeated SQLite's index — a bare
   3.1M-row table scan on every call, 1,309 seconds for one detector alone. An `ESCAPE` clause
   unconditionally kills the LIKE-to-index-range optimization; rewritten as a prefix-range
   comparison (`path >= 'x/' AND path < 'x0'`), needing no escaping at all. 346x speedup,
   confirmed by query plan, not benchmark vibes.
5. `_drop_nested_candidates` re-scanned an entire `list` of kept directories for every candidate
   — O(candidates × kept_dirs × depth) — invisible until a real disk produced tens of thousands
   of non-nested sibling directories. A `set` and an inverted containment check made it
   O(candidates × depth): 3.5+ minutes (never finished) to 1.55 seconds.

**Selectivity — the 80% collision number was mostly noise.**
6. 2.49 million of 3.12M files on the real disk shared a size with at least one other file — a
   number that looked like it demanded hashing most of the disk. Querying the actual
   distribution showed it was dominated by 333K zero-byte files and a long tail of tiny sizes
   that could never reclaim anything material even in the best case. A materiality gate
   (`(member_count - 1) × size` must clear a 1MB floor before a bucket is even queried) turned a
   disk-I/O-bound multi-hour pass into one that skips the noise entirely.

**Honesty — logical size is not reclaimable size, and "byte-identical" is not "safe to
delete."**
7. The first vault-quarantine measurement showed a genuine 0-byte disk-free delta for a real
   200KB move — expected, not a bug (same-volume moves don't free space), but it meant the
   project's own top-level success criterion ("reclaims ≥30GB, verified via before/after
   disk-free measurement") was structurally unreachable under "no permanent delete in v1."
   Resolved by making retention a per-category property (ADR-0001): categories whose only real
   recovery path was always "rebuild it" get permanent deletion; everything else stays
   recoverable.
8. `.cache/huggingface/hub` (124.9GB) classified as a generic `package_cache` → permanent
   delete — wrong, because re-acquiring 100+GB is not `npm ci`, and a gated or fine-tuned model
   may not be re-downloadable at all. Split into its own `model_caches` category: vaulted,
   never Tier A, cost-aware regardless of size (ADR-0003).
9. The uv/cache purge measured a 3x gap between logical size and real freed space: 13.30GB
   logical, 5.21GB real disk-free delta. uv hardlinks cache blobs into live virtual
   environments — deleting the cache's own name for a blob just drops a link count, not the
   shared blocks. `exact_duplicate` was exposed to the identical mechanism in reverse:
   byte-identical content is exactly what a hardlink produces. Fixed with hardlink-aware
   accounting (ADR-0006) — a candidate credits its full size only if every name pointing to its
   inode is in the same delete set.
10. A side-by-side review of the largest duplicate clusters (ADR-0007) found the keep-heuristic
    could pick a Downloads-folder copy over a git-repository copy of the same file, and a
    structural review (ADR-0008) found `exact_duplicate` proposing deletion of HuggingFace
    blob/snapshot pairs and one conda environment's own binaries in favor of another's —
    byte-identical for structural reasons that have nothing to do with waste. Fixed with a
    location-ranked keep-heuristic, whole-cluster exclusion on a protected member, and
    model-cache/cross-environment exclusion, dropping the estimate from 23.09GB to 4.26GB.
11. **Capstone — the tool broke its own runtime, live, and recovered only because the delete
    was reversible.** The exact_duplicate apply ran for real, and within minutes this project's
    own `.venv` stopped working: `import socket` failed. `socket.py` and 185 other files had
    been recycle-binned from three shared Python interpreter installations (a uv-managed build,
    `gcloud`'s bundled Python, the Android NDK's toolchain Python) because none of them are a
    conda environment or a venv — no `conda-meta/`, no `pyvenv.cfg` — so the ADR-0008 protection
    never recognized them as environments at all. A first, keyword-driven recovery pass found
    and restored 71 files and looked complete; it wasn't. A systematic re-audit — re-running the
    fixed detector against every one of the 10,134 applied files, not just the paths a keyword
    scan thought to check — found 186 true violations, not 71. All 186 were recovered from the
    Windows Recycle Bin by parsing its `$I`/`$R` index format directly (the project's own
    restore command doesn't support Recycle Bin batches). Fixed twice, not once: first by adding
    a marker to the environment detector (`python.exe` + a sibling `Lib/` directory — ADR-0009),
    then by root-causing *why* marker-based detection was always going to be incomplete and
    replacing it with structural detection by default (ADR-0010) — checking this project's OWN
    `.venv` directly found that Windows venvs put their interpreter in `Scripts/`, not the venv
    root, meaning even ADR-0009's fix would have missed a venv whose `pyvenv.cfg` ever went
    missing. Nothing here proves the class of risk is closed; ADR-0010 says so explicitly.

Bug 11 is the whole thesis in one incident, not a coda after it. The estimate had already been
corrected twice (bugs 9 and 10) by the time this ran; the pre-apply review had explicitly
checked for exactly this class of risk and found zero violations; and it still happened —
because the check that existed was precise about the environments it knew to look for, and this
was one it didn't. Every one of the ten bugs before it was caught by a human or a script
watching a real run; this one was caught by the tool breaking something the person running it
depended on. Recycle Bin, chosen specifically for its recoverability and not because anything
was expected to go wrong, is the only reason this is a paragraph in a case study instead of a
rebuilt development machine.

## Security audit — the review UI was itself the attack surface

Every mechanism above defends the deletion engine. None of it defends the screen a human is
supposed to look at *before* trusting the engine — and that screen turned out to have the most
dangerous bug in the project, worse in kind than any of the eleven above, because it didn't
misjudge what to delete. It could have been made to delete on command from something that was
never the user at all.

**Finding — filename-driven XSS in the one UI whose entire job is "look before you delete."**
`renderClusterTable` (`src/reclaim/api/static/app.js`), the duplicate-cluster table in the Review
Queue, built its `<td>` for each cluster member by interpolating `member.path` directly into
`.innerHTML`. Reclaim's whole purpose is walking a real, arbitrary disk — a file or directory
literally named `<img src=x onerror="...">` is not a contrived test string, it's real, reachable
input the scanner will happily index and the dashboard will happily render. The chain that makes
this more than a display bug: the dashboard embeds its per-session CSRF token in the page itself
(a `<meta>` tag, added by this same audit — see below) so that legitimate `fetch()` calls can
carry it; a script running via this XSS executes in that exact page, with that exact token
already sitting in the DOM next to it. A filename is not supposed to be able to call
`POST /api/apply`. Before this fix, one could — the safety model's entire premise ("nothing is
deleted without review") assumes the reviewer sees what's actually on disk, not markup an
attacker chose. Fixed by rewriting the row to build every cell via `textContent`
(`git diff`-verifiable: zero `innerHTML` assignments carry a path field anywhere in the codebase
today). Verified two ways: a static audit of every other `innerHTML` call site in `app.js`
confirmed none of the rest interpolate attacker-controlled data (categories, tiers, and formatted
numbers all come from a fixed server-side lookup table, not a filename); and
`tests/frontend/xss.test.mjs` (jsdom, `node --test`, wired into CI) feeds the exact function an
`<img onerror>` and a `<script>` payload as a cluster member's path and asserts the resulting DOM
contains zero `<img>`/`<script>` elements and the cell's `textContent` equals the raw payload
verbatim — proving the payload survived as inert text, not that it was silently stripped.
**Residual, disclosed honestly:** this is a DOM-assertion regression test against the real render
function, not a live-browser end-to-end test (no Playwright/browser-automation dependency exists
in this repo yet) — it proves the specific vulnerable pattern can't recur without proving every
possible future rendering path is safe by construction.

**The rest of the audit**, same pass, each fixed and tested (not just documented):

| finding | fix | verification |
|---|---|---|
| `--host` accepted any value, including `0.0.0.0` | hard-gated at argparse parse time to `127.0.0.1`/`::1` only — not merely defaulted | `tests/test_cli.py` — 0.0.0.0/LAN/`localhost`/hostname all rejected at parse time, re-validated again inside `_run_serve` for any caller that bypasses argparse |
| No CSRF protection; no defense against DNS rebinding (a page that resolves to 127.0.0.1 but sends a foreign `Host` header) | per-process CSRF token required on every mutating `/api/*` call; `Host`/`Origin` headers checked against the exact loopback authority the server is bound to | `tests/test_api.py` — missing/wrong token rejected (403), mismatched Host/Origin rejected (403), matching Origin and read-only GETs without a token both still work |
| `restore_batch` trusted a manifest entry's `vault_path`/`original_path` unconditionally | refuses the entire restore if a `vault_path` doesn't resolve inside the configured vault directory, or if `original_path` matches a protected system root — the zip-slip-equivalent guard for this tool's own append-only manifest | `tests/test_executor.py` — two adversarial tests hand-construct a "tampered manifest" entry (escaping vault path; protected-root destination) and confirm the whole batch is refused, including the legitimate entries sharing it |
| Nothing stopped Reclaim from running elevated, silently discarding the OS's own permission backstop | every mutating command (`apply`/`undo`/`purge`/`serve`/`dashboard`) refuses to start if the process holds an elevated token | `tests/test_elevation.py` + `tests/test_cli.py` — mocked-elevated runs refused before touching disk; `tests/test_safety.py` separately confirms `SafetyValidator`'s protected-root verdict is identical regardless of elevation state (it was always a pure pattern match, never OS-permission-dependent — the guard closes the *other* backstop, not this one) |
| No dependency vulnerability scanning | `pip-audit` added to CI, failing the build on any known vulnerability in a locked dependency (zero found as of this pass) | `.github/workflows/ci.yml` `pip-audit` job |

## The applied-AI layer — safety-eval-first, and provisional means provisional

Everything above is the deterministic engine: hashes and path rules, no models. A separate
build added an applied-AI layer beside it — near-identical image clustering and a classical
keep-best scorer — and the build order itself is the interesting engineering decision: the
safety eval had to pass, independently verified, *before a single model existed*. Not "we'll
add a safety test eventually." The harness and the recommend-only guarantee came first,
against scaffolding and fabricated data, precisely so no feature could ever be built on top of
an unproven boundary.

**The boundary is structural, not conventional.** `AICluster`/`AIClusterMember`
(`src/reclaim/ai/models.py`) deliberately share zero field names with the deterministic
engine's `Candidate` — `reclaim.executor.apply_batch` accesses `candidate.safety_verdict`
unconditionally on every item it's handed, so passing it an AI-produced object raises
`AttributeError` before any filesystem call, not because a convention was followed but because
the object literally doesn't have the field. A static AST scan re-checks every file under
`src/reclaim/ai/` on every CI run for an import of `reclaim.executor` or `send2trash`; an
independent verifier pass tried to find a gap in that scan and found one for real (`from
reclaim import executor` — a form the scan's first version missed by reading only
`ImportFrom.module`, not the imported names) — closed with a regression test before the gate
was allowed to pass. The adversarial case named explicitly in the build brief — a
`config.toml` that tries to inject an AI-named category into the auto-quarantine path — is
rejected by `pydantic`'s `extra="forbid"` on every config model; there is no field for it to
land in.

**The XSS lesson from the security audit recurred immediately, in new code, and was caught the
same way.** Building the gold-set labeling tool's review UI, the first draft used
`onclick="selectKeep('...', i, '...')"` with `html.escape()`-wrapped filenames interpolated
into the JS string literal — the exact double-context injection class already fixed once this
session in the main dashboard, just in a different component. HTML-escaping a quote character
does not protect a JS string literal inside an inline event-handler attribute: the browser
HTML-decodes the attribute value *before* parsing it as JavaScript, so the escaped quote
reappears as a literal one and breaks out of the string. Caught and rewritten — every
filename/path now travels exclusively through `data-*` attributes read via `.dataset`, never
re-interpreted as code — before any test was written against the vulnerable version, and an
independent verifier separately constructed its own adversarial filenames
(`');alert(1);//`-style paths, backticks, mixed quotes) against the fixed version to confirm
the fix actually holds, not just that the diff looks right.

**Every operating point is labeled provisional, everywhere, on purpose.** The measured
Hamming-distance threshold (2, at precision 1.0) comes from a real PR-curve derivation — the
actual selection method the spec requires — run against synthetic, seeded (42) fixtures, not
real photos. `select_operating_point()` hardcodes `is_provisional=True`; there is no code path
that returns a non-provisional result. The CI regression gate deliberately uses a looser,
margined threshold (10) rather than the measured 2, because clean synthetic transforms
(resize, recompress, mild brightness shift) are an easier case than real near-duplicate
photos, and the gate's job is catching regressions, not asserting a production value. None of
this may be cited as "Reclaim's near-dup threshold is X" in any user-facing copy — that
requires the gold-set labeling tool this same build delivered (a loopback-only FastAPI review
UI reusing the dashboard's own audited Host/Origin/CSRF guard, verified end-to-end in a real
browser session: select a keeper, confirm, reload, confirm persistence, then a live `curl`
proof that a spoofed Host header and a missing CSRF token are both actually rejected by the
running server) to actually be run against real data — a deliberate, disclosed stop, not an
oversight.

**The fixture itself needed a real bug fix to give honest ground truth.** An early version of
the synthetic image fixtures resized every quality variant back to identical final pixel
dimensions before measuring quality — which silently zeroed out the keep-best scorer's
resolution signal and produced an arbitrary 0.667 top-1 agreement between two members that
were, by every signal the scorer actually measures, genuinely tied. The fix wasn't to tune the
scorer's weights until the number looked better; it was to make the fixture preserve real
resolution differences between variants (also the more realistic shape — an actual resized
copy usually does carry fewer pixels), which resolved agreement to 1.0 without touching the
scorer at all. The eval was catching real ambiguity in the test data, not a code defect — worth
noting because the instinct to "fix" a failing eval by adjusting the thing being measured is
exactly backwards, and this is a small, concrete example of resisting it.

## Provisional becomes measured — from a public dataset, not the nearest disk

The gold-set labeling tool above was built and verified, but GG hadn't run it yet — no
`data/ai_labels/gold_labels.jsonl` existed. Rather than wait, or select Feature 1a's operating
point against GG's own disk first (a real risk: a threshold tuned to one person's photo library
before any broader source was tried would ship a default that's quietly overfit to that one
disk), the instruction was to source ground truth from a public, human-labeled dataset first,
and to treat an LLM as a labeler only as a last resort, per feature, with its own measured error
rate — never as the source of a shipped number.

**The dataset hunt was itself an engineering decision, not a formality.** Five public
near-duplicate datasets were evaluated against three criteria: license permitting local research
use, task match to Reclaim's actual target (consumer photo-library duplicates, not object
recognition or scene retrieval), and a scriptable, non-interactive download path. California-ND
was the best conceptual match — 701 real personal photos, annotated by ten human subjects
specifically for near-duplicate judgment — and got disqualified anyway: its archive is a
password-protected zip whose password comes from emailing the (2013-era) author directly, which
fails "scriptable" outright. INRIA Copydays won instead: purpose-built for copy detection with
graduated attack severities, INRIA's own "as-is, cite it" license, and — once its original host
(`pascal.inrialpes.fr`) turned out to be dead (a genuine TCP connect timeout, not a typo) — a
live Meta/FAIR mirror used for their own published copy-detection research. An unofficial
Hugging Face mirror was found and rejected on sight: it shipped zero actual image bytes and
instead a `trust_remote_code=True` loader script from an unverified community account — a
supply-chain risk with no upside once a direct, checksum-verified HTTPS path existed.

**The real measurement told a real, two-sided story, and both sides are in the ADR.** The FAIR
mirror only carried Copydays' `original` and `strong` splits — not the milder, graduated
`jpeg`/`crop` ladders the original host also offers. `strong` is the dataset's single hardest
tier: print-and-scan, blur, paint, deliberately adversarial. Run for real — 74,305 pairwise
Hamming distances, 314 genuine positives, 73,991 genuine negatives, zero synthetic data, zero
LLM labels — the PR curve puts the ≥0.95-precision operating point at Hamming distance 14,
precision 0.9600, **recall 0.0764**. That recall number is real and is reported as-is, but it
would be dishonest to let it stand alone: it's a floor measured against the hardest, most
adversarial subset available, not an estimate of how often Reclaim will actually flag an
ordinary resized-and-recompressed duplicate. ADR-0012 says this explicitly and forbids citing
0.0764 as "how often Feature 1a catches real duplicates" anywhere user-facing — the precision
number (0.96, meaning few false positives) carries no such caveat and is the one that actually
protects users from a bad delete-suggestion.

**Keep-best got a real answer to the opposite question — not "is our synthetic ground truth
consistent," but "does the scorer agree with a real, independently-known quality ordering."**
Every Copydays block pairs one untouched original against its print-and-scanned/blurred/painted
derivatives — a real, non-fabricated "which copy should you keep" ground truth, since those
attacks degrade quality by construction, not by assumption. Measured across all 157 blocks: 0.8726
top-1 agreement, and — the metric that matters more, per the spec's own ordering — 1.0000
never-picks-the-worst-quartile safety rate, not once in 157 blocks. The 20 blocks where the
scorer's pick differed from the original were not silently accepted or auto-corrected: they were
written to `reports/ai/copydays_keep_best_disagreements.json`, a small, reviewable, provenance-
tagged file, for GG's own optional look — the instruction was explicit that no preference label
gets fabricated, by an LLM or otherwise, to paper over a disagreement.

**What got skipped, and why that's disclosed rather than silent.** GG named AVA (a public
aesthetic-scoring dataset) as a possible check on the scorer's *general* quality signal,
separate from the keep-best question above. AVA turned out to be a 32GB torrent / 49GB single
Hugging Face zip of individual photographers' contest submissions — two orders of magnitude
larger than Copydays for a secondary sanity check, and the uploader's `apache-2.0` tag on their
own packaging doesn't resolve the underlying per-photographer copyright the way INRIA's blanket
grant does for Copydays. Skipped, with the reasoning recorded in ADR-0015 rather than quietly
dropped — the more operationally important half of the instruction (real preference ground
truth, disagreements surfaced not fabricated) was still fully delivered.

## A measured number can still be measuring the wrong thing

The Copydays measurement above shipped a real number — 0.0764 recall — and it was genuinely
measured, not fabricated. It was also, on its own, misleading, and catching that is its own
small case study in why "we ran a real eval" isn't the same claim as "we ran the right eval."

**The catch.** The only real gold data reachable turned out to be Copydays' `strong` split —
the dataset's single hardest, deliberately adversarial attack tier (print-and-scan, blur,
paint: built to test whether an algorithm survives someone actively *trying* to defeat copy
detection). Feature 1a's actual job is nothing like that — it's catching a photo library's
ordinary duplicate accumulation: a re-save, a resize, a messaging-app re-compression. The
0.0764 recall figure was real, reproducible, and completely uninformative about the thing
Feature 1a actually needs to do well at. Trusting it as-is would have meant either shipping a
feature that looks worse than it is, or — worse — tuning the threshold to chase recall on a
distribution nobody will ever actually hit.

**The fix wasn't to re-tune the threshold — it was to fix what was being measured.** Copydays'
own milder splits (graduated JPEG-quality and crop ladders, which would have been the right
comparison) turned out to be unreachable on every mirror tried, including a second search pass
specifically for those two files. So the realistic distribution was built directly: five named,
deterministic transforms — light re-save, moderate resize+recompress, a PNG round-trip
simulating a re-edit, and a WhatsApp/Instagram-style resave (downscale to a max long edge,
moderate quality, metadata stripped) — applied to Copydays' own 157 real photos, not synthetic
drawn shapes. Real photographic content, programmatically and deterministically attacked in
ways that actually resemble what a phone's camera roll accumulates.

**The result flipped the entire read of the feature.** At the exact same locked threshold,
recall on the realistic distribution was 1.0000 — not a typo, every one of 785 mild/moderate/
messaging-app duplicate pairs caught, at precision 0.9987. The `hard`-tier number wasn't wrong;
it was answering a question nobody needed answered. This also settled a second, harder
question cleanly: whether to loosen the threshold toward 90% precision for more recall, since
every AI suggestion is human-confirmed before deletion anyway. The realistic curve showed there
was no recall left to buy past a very tight threshold — precision stayed at 1.0000 all the way
out past where recall had already saturated. Loosening further would only have added false
positives to the review queue for zero benefit. And it answered a third question that wasn't
even being asked yet: whether pHash's limitations were the empirical trigger to justify CLIP
embeddings (Track B). They weren't — pHash's ceiling on the distribution that matters is
already close to perfect, so embeddings would be solving a problem Track A doesn't have.
Track B remains justified on its own separate merits (semantic grouping is a different problem
than copy detection), not as a rescue for a gap that this measurement shows doesn't exist.

## Turning an incident into a gate, then building the next feature inside it

The recall-artifact incident above didn't end with a fix. Before any more features got built,
the eval infrastructure itself changed: `select_operating_point` now requires a recall floor
alongside the precision floor, and a `DistributionDeclaration` that has to say, in a
structurally validated field, whether the data behind a number is realistic, adversarial-only,
or synthetic-only. A function called `assert_safe_to_promote_to_measured` refuses to let an
adversarial-tail-only or synthetic-only distribution justify the word "MEASURED" — and the
original incident became a permanent regression test: the exact Copydays curve that produced
the misleading 0.0764 recall now has to fail the new gate, on purpose, forever, or the test
itself fails.

Then the next feature — document near-duplicate detection and version-chain ordering — got
built inside that harder gate from the first line of code, not as an afterthought. Three real,
license-driven decisions shaped the dataset choice: Quora Question Pairs was rejected outright
despite being the most obviously available option, because Quora's terms of service carry a
non-commercial restriction and this project's stated posture is to build things that could
compete in market, not just work as a demo. PAWS — real Wikipedia sentences, cleanly licensed —
was still disqualified from being the *primary* measurement, because it's adversarial by
construction (its own name is "Paraphrase Adversaries from Word Scrambling"): using it alone
would have been the exact same mistake as Copydays' `strong` split, just with a different
flavor of hard. It stayed in as a disclosed secondary check, and what it showed was itself
telling — embedding similarity alone couldn't clear 90% precision on PAWS at any useful recall,
confirming it really is a hard adversarial set, not evidence that Feature 1b's actual pipeline
is unreliable.

The real measurement came from eight public-domain novels, chunked into document-length pieces
and edited with three deterministic, disclosed transforms simulating how people actually
accumulate duplicate documents — a light resave, a restructured revision, a copy-paste into
another app. On that distribution, the two-stage pipeline (MinHash prefilter, then a sentence-
embedding confirmation pass) turned up an honest, slightly humbling finding: at the threshold
that gave clean precision, Stage 1 alone already caught everything — there was no ambiguous
residual left for Stage 2 to resolve on this measurement. That got written down as exactly what
it is, not smoothed into a confident-sounding number the embedding stage never actually earned.

## Never pool across tiers — a second incident, the same shape, a different mechanism

The templated-document follow-up to Feature 1b produced the project's second recall-artifact-
shaped incident, and it's worth separating from the first because the *mechanism* was
different, not just the symptom. The Gutenberg-prose measurement above was real and correctly
gated — but it measured edited *prose*, and prose has no structural analog to what a resume,
invoice, or report actually looks like. Two different people's resumes share section headers
("OBJECTIVE," "EXPERIENCE"), standard connective phrasing, and formatting scaffolding while
being genuinely unrelated documents — exactly the shape of false-positive risk word-shingle
MinHash is vulnerable to and prose never exercises. Measured against a new templated tier (3
synthetic-but-realistic templates, 459 same-template-different-content negative pairs), the
shipped thresholds produced **71% false positives** — a feature that would confidently call
two different people's resumes duplicates roughly seven times out of ten it flagged anything.

**The fix (a stricter joint threshold, gated on both tiers independently) is not the
interesting part. The bug caught while building the fix is.** The first version of the
re-measurement pooled the prose tier's 7,140 clean negatives and the templated tier's 459
negatives into one combined precision calculation. That pooled number read **0.9524
precision** — comfortably clearing the 0.95 target. The templated tier **alone**, at the exact
same threshold, was actually **0.8634 precision** — a real, serious failure the pooled number
hid completely, because the large prose tier's thousands of cleanly-rejected negatives
mathematically diluted the small tier's real false positives into statistical insignificance.
This is the *same shape* of mistake as the Copydays recall artifact (a real number that looks
fine while hiding a real problem) but a *different mechanism*: the first incident was
measuring only one unrepresentative distribution and calling it representative; this one was
measuring several distributions correctly and then destroying the signal by pooling them
before computing the metric.

**Both are now structurally forbidden, not just documented as lessons.** `eval_harness.py`
gained `select_operating_point_per_tier`/`select_joint_operating_point_per_tier` — tier-gated
analogs that have no code path capable of concatenating counts across tiers before computing a
ratio; a candidate threshold only qualifies if *every* declared tier clears both floors
independently. The real incident became a permanent regression test that reconstructs the
exact 342/18/0/7140 and 158/4/25/434 counts, first proves the naive pooled function *would*
accept the bad operating point (reproducing the bug as a documented fact, not a strawman), then
proves the per-tier function rejects it — if that test ever starts passing with a non-`None`
result, the gating logic has silently regressed. A code reviewer (human or automated) seeing a
hand-rolled multi-tier precision loop in any future eval file now has a specific, named thing
to flag.

## When two signals about "which file is latest" disagree, silence isn't a compromise

Version-chain ordering is the one path in the whole AI layer that can recommend deleting a
file the user still considers current — not an accumulated near-duplicate, the actual live
draft. Filename pattern (`v1`/`v2`/`final`/`(1)`/`copy`) and modification time are two
*independent* signals about which file in a chain is latest, and the build brief's own
fixtures never deliberately pitted them against each other. The scenario that matters is
concrete: `report_final.docx`, modified BEFORE `report_v2.docx` — the filename claims "final"
is the latest version; the timestamp says otherwise. Nothing about that disagreement is exotic
— reverting to an old draft and continuing to edit it without renaming produces exactly this.

The fix is not "pick the signal we trust more." It's `version_signals_agree`: every pair of
files that both carry a recognizable filename rank gets compared against their mtime order; if
any pair's filename-implied order contradicts its mtime-implied order, the whole chain's
ordering is flagged as untrustworthy. `build_version_chain_cluster` only marks a keeper when
signals agree — when they disagree, no member is marked, which (via the same
`suggests_deletion` mechanism used everywhere else) demotes the cluster to browse-only: still
returned, still ordered, still visible for manual review, just never framed as a confident
deletion suggestion. Measured against a dedicated 4-chain conflict fixture including the exact
named scenario: **0 safety violations**, and a control chain with no real conflict still
correctly produces a keeper — proving the check doesn't cry wolf on ordinary chains, not just
that it catches the adversarial one.

This pattern — a track that's mechanically deletion-eligible, but whose orchestration
withholds the keeper flag under a specific, testable disagreement condition — became the
template Feature 2 reused directly, not a one-off fix specific to filenames.

## Feature 2 — a screenshot burst is not a duplicate, and OCR failure is not a safe default

Screenshot-burst detection layers a new risk on top of near-duplicate clustering: the content
of a screenshot determines whether deleting the rest of a burst is safe at all. A burst of six
near-identical "still loading" screenshots is disposable clutter; a burst of six near-identical
screenshots of a receipt, mid-scroll, is not — and the two can be visually almost
indistinguishable by pHash alone. Burst detection itself reused, deliberately, an
already-measured signal rather than inventing a new one: three rules — matching dimensions,
capture-time proximity, and pHash within the exact Hamming-distance bound the Copydays
measurement already established — all three must agree, not a majority vote, and the pHash
threshold was not re-derived from scratch; re-measuring "screenshots taken moments apart" would
have been measuring the same near-identical-image problem Track A already solved. The one new
parameter, a 60-second capture-time window, is disclosed as a policy choice, not a measured
one, because no real usage telemetry exists to derive it from.

**The content-tag classifier is where "bias STRONGLY toward keep" had to become code, not just
a sentence in a spec.** Five tags — receipt, document, code, chat, transient-UI — and by
explicit design, only `transient-UI` is ever deletion-eligible; `KEEP_BIASED_TAGS` structurally
excludes it and includes everything else, including the low-confidence `UNKNOWN` fallback
(ambiguity is exactly when caution matters most, not a reason to guess). A dataset search for
this exact taxonomy came up empty on public data with a clean license — SROIE's canonical
receipt corpus sits behind gated registration and its GitHub mirror's MIT tag almost certainly
covers the mirror's scripts, not the underlying receipt photographs; RVL-CDIP's document
corpus reports a bare `"license": ["other"]` on Hugging Face, tracing back to real
tobacco-litigation business records. Both rejected on the same license-ambiguity grounds
Copydays' unofficial mirror and Quora Question Pairs were rejected on earlier. Two of five
classes still got real content instead of pure synthesis: Gutenberg prose for `document`,
Reclaim's own source tree for `code` — zero licensing risk, real text, not fabricated.

**A real false-positive risk was caught by testing the classifier against itself, not by
inspection.** The first version of the transient-UI scorer gave short/sparse text a confidence
bonus large enough to clear the classification threshold on its own — meaning three words of
OCR noise with zero transient-UI vocabulary (`"xk qz 42"`) confidently tagged as `transient-UI`,
the one deletion-eligible tag, purely for being short. Caught while building the eval, not
after shipping: capping that bonus below the confidence floor closed it, verified both as a
unit-level regression test and against a dedicated pool of ambiguous OCR-noise fixtures
(`"OK"`, stray punctuation, gibberish) that all now correctly resolve to `UNKNOWN`.

**That fix was proven against synthetic short strings — which is not the same as proving it
against a real photo that happens to OCR badly**, and closing that specific gap was a separate,
later, explicitly requested pass. A genuinely meaningful screenshot — a receipt, a document, a
chat — degraded by an ordinary real-world capture defect (dark/low-contrast, out-of-focus
blur, a partial/cut-off crop) produces the same shape of sparse, fragmented OCR output as
gibberish does, and nothing about the synthetic fix proved that path was closed too. Four real
content types, rendered as real images and degraded four realistic ways each through the real
`rapidocr` engine and the real, shipped classifier — not a simulation — produced **0 of 16
degraded real-content images ever classified transient-UI**. Twelve of the sixteen degraded all
the way to zero-character OCR output; every one of those twelve resolved to `UNKNOWN`, exactly
the required behavior: *OCR found little* must mean *I can't tell*, never *transient, therefore
deletable*. A separate, image-independent check locked in the same invariant directly —
`None`, empty, whitespace, and a handful of one-to-two-character stray-OCR strings all resolve
to `UNKNOWN` — so the property holds even if no future image fixture happens to degrade that
far on a given run.

**The OCR text itself never had anywhere to leak, structurally, not by policy.** The three
modules that ever touch raw OCR output contain zero logging or print calls, verified by an AST
scan that would fail on a hidden or aliased logging call, not just an obvious one — and a
runtime test goes further: a real OCR extraction on an image containing a unique canary string,
every Python logger forced open at `DEBUG`, asserts the canary appears in **zero** captured log
records, proving the guarantee holds even against loggers Reclaim's own code didn't call
directly (a dependency's internal logging included). A second version of the same test proves
the canary never appears anywhere in the actual `AICluster` objects a caller receives back —
checked via `repr()` over the whole result as a structural catch-all, not just the fields
expected to matter.

## Feature 3 — logging decisions before there's anything to train

The honest version of "the tool learns from your decisions" is not a model trained on a
handful of clicks. It's a decision log, versioned and commit-keyed the same way the gold-set
labeling tool's own label store already is, that accumulates real, timestamped training rows —
and an explicit refusal to train anything until there's enough of them to split into a genuine
past/future evaluation. Every accept/reject/keep decision gets logged with its feature vector:
size, extension, a location classification (cloud-sync placeholder, git repo, a recognized
special folder, or "other," in that priority order), modification and creation time, cluster
statistics (how big the group was, whether this member was the recommended keeper), the AI
track it came from, a cloud-sync flag, and — the one feature that needs the store's own history
to compute — how the user has already decided about this cluster's other members, since a
cluster where three siblings were already accepted for removal is real evidence the fourth is
clutter too, in a way no single file's own attributes could capture alone. Access-time is not
just omitted from that list; it's structurally absent from the type, because NTFS commonly
disables last-access-time tracking system-wide, which would make it an unreliable feature even
where it existed.

**Nothing about a ranker got built.** No LightGBM import exists anywhere in this codebase; the
label-gate (500 real decisions before training anything is attempted) and the time-split
evaluation discipline (train on the past, evaluate on strictly later decisions — never let a
future decision leak backward into a training set) are documented as a future PR's obligations,
not implemented as dead code waiting to be switched on. In the meantime, the review queue is
ordered by a heuristic that is loudly, structurally not a model: `compute_cold_start_priority`
multiplies log-compressed size and staleness by a location weight and a cluster-size bonus, and
every result carries an `is_heuristic` field hardcoded to `True` — not because a docstring says
so, but so a UI or a log line has something to actually assert against if it ever needs to
prove the number it's showing isn't a prediction.

## The generic clutter-likelihood ranker — cross-LLM consensus, not majority vote

Feature 3 above stopped deliberately short of a model, because personal preference isn't
knowable from anywhere except a real person's real decisions, and there weren't 500 of them
yet. A separate, later build asked a narrower, answerable question instead: not "does this
user want this file gone," but "is this file the *kind* of thing that's usually safe to
suggest" — a property that generalizes across users, the way "build artifacts are usually
clutter" is true regardless of whose disk it is. That distinction isn't cosmetic — it's
load-bearing enough that the ranker is named `clutter_likelihood`, not `preference`, and
`ClutterLikelihoodScore.is_generic` is hardcoded `True` everywhere the type is constructed, the
same pattern `is_heuristic` uses one section up: a field a caller can actually assert against,
not a docstring's claim about itself.

**Labels came from three independent local LLMs, not one model asked three times.** `qwen3:8b`
(Alibaba), `llama3.1:8b` (Meta), and `gemma2:9b` (Google) — genuinely different model families,
not three sizes of the same one — each rated synthetic file-record fixtures on a fixed 0–4
clutter-likelihood rubric, entirely through a local Ollama server. Zero paid API calls anywhere
in the labeling pipeline; the cost of three-judge consensus labeling on this feature was $0.

**Real inter-rater statistics, not an assumed "the judges agree":** Fleiss' kappa across all
three raters (N=120) came out to **0.6768** — "substantial agreement" on the standard
Landis–Koch scale, not perfect, and reported as exactly that. Pairwise Cohen's kappa broke down
which pair agreed most: `llama3.1:8b`/`gemma2:9b` at 0.7250, `qwen3:8b`/`gemma2:9b` at 0.6664,
`qwen3:8b`/`llama3.1:8b` at 0.6444 — `qwen3` was the most frequent outlier of the three, a real
finding about model behavior, not noise averaged away.

**The disagreement wasn't voted away — it was excluded from training entirely.** 41 of 120
records (34.2%) had no unanimous 3-judge agreement, and the ranker trains on the **79-record
unanimous subset only** — roughly one in three records is exactly where "generic
clutter-likelihood" turned out not to be as generic as the label taxonomy assumed, and papering
over that with 2-of-3 majority voting would have taught the model to imitate the noisiest
judge's tie-breaks instead of a real, agreed-upon property. A LightGBM LambdaMART ranker (MIT,
CPU-only, zero GPU requirement — same zero-cost posture as every other dependency in this
layer) trained on a grouped split (6 batches / 62 records train, 2 batches / 17 records eval,
whole batches never split within one, so no cluster's records leak across the split) scored
**NDCG@5 = 0.9763** (floor 0.70) and **precision@3 = 1.0000** (floor 0.50) on the held-out
batches.

**Permanently provisional, structurally, not by convention.** `_DISTRIBUTION.is_synthetic_only`
is `True` — both the file records the judges rated and the labels themselves are
synthetic/LLM-generated, not real personal decisions — and `assert_safe_to_promote_to_measured`
(the same gate ADR-0016 built to close the Copydays recall-artifact incident) *raises* on this
distribution by construction. There is no code path in this feature that can ever report itself
as MEASURED. That's not a hedge added for this write-up; it's a function call in the eval suite
that fails the build if anyone tries to relax it.

## Track B — CLIP semantic grouping, justified on its own terms, not as a rescue

Once Feature 1a's realistic-distribution remeasurement showed pHash catching 100% of ordinary
resize/recompress duplicates, the obvious follow-up question was whether embeddings (Track B,
CLIP-based semantic image grouping) were still worth building at all — if pHash already solves
near-duplicate detection, what's left for a heavier model to earn its keep on? The measured
answer: a different problem entirely. Track B never re-examines anything Track A already
clustered — it groups the *residual* by semantic similarity (two different photos of the same
beach, not two copies of the same photo), a browse-only feature with no deletion path, ever.

**Model choice was a real licensing decision, not a default.** Apple's MobileCLIP was rejected
outright despite matching the spec's suggestion — its pretrained weights carry an Apple "ML
Research Model" Terms of Use that explicitly excludes "commercial exploitation, product
development, or use in any commercial product or service," a hard prohibition inherited by any
derivative, on top of not being pip-installable at all. OpenCLIP ViT-B/32 (MIT end-to-end, code
and weights both) shipped instead — `pip install open_clip_torch`, one ~338MB checkpoint, zero
gating. A real correctness bug surfaced during model loading itself: the `"openai"` pretrained
checkpoint silently mismatches `ViT-B-32`'s default GELU activation (a cosmetic-looking warning
that isn't cosmetic) — fixed by loading the `"ViT-B-32-quickgelu"` variant instead, verified
against the warning disappearing, not assumed from the docs.

**Measured on a real, disclosed distribution, per-tier gated the same way ADR-0018 requires
everywhere else:** BCubed precision 0.7897, recall 0.7143 at similarity threshold 0.82, across
98 real images. Gated on precision ≥ 0.70 (looser than dedup's 0.95 — the spec's own explicit
framing that browse-tidiness carries a different risk profile than a deletion decision) AND
recall ≥ 0.20, both cleared with real margin. The full precision/recall curve swept 0.70–1.00
tells an honest, two-sided story instead of just the chosen operating point: below ~0.80,
precision collapses fast (CLIP's semantic similarity is genuinely permissive at that range —
this is the model being itself, not a bug); above ~0.90, precision is perfect but recall
plateaus around 0.41–0.46, the ceiling of what "semantically similar" can mean before it starts
demanding near-duplication again.

## Shipping to strangers — safe mode as the last structural boundary

Every mechanism in this case study so far was built, measured, and incident-hardened against
one disk, watched by one person who wrote the code. The tool's next real change of trust model
wasn't a new detector or a new AI feature — it was a public installer, aimed at people who will
never read this document, never watch a dry-run report, and have no reason to trust a stranger's
GitHub repo by default. The instruction for that pass was explicit and non-negotiable: safe mode
had to be provable the same way the §7.5 AI-layer boundary is provable — a structural guarantee
a test can assert against, not a default value some code path could accidentally bypass around.

**Three independent guarantees, not one flag.** (1) `apply_batch`'s method resolution checks
`mode == Mode.SAFE` *before* any other rule and returns `recycle_bin` unconditionally — the
`vault` and `direct_delete` branches become unreachable by construction whenever mode is safe,
not merely unreached in the cases a test happened to try. Proven the same way the §7.5 gate is
proven: by monkeypatching `os.unlink` and `shutil.rmtree` to raise if called, then running a
real `apply=True` batch and confirming nothing raises. (2) The three highest-risk category
groups (`duplicates`, `model_caches`, `dev_artifacts` — the exact class of category that broke
this project's own `.venv` in the incident above) are forced off regardless of what
`config.toml` requests, via a config-resolution function kept deliberately separate from plain
config parsing — the first version that merged the two broke ~240 pre-existing tests that had
never been about safety mode at all, which is its own small lesson about not overloading one
function with two concerns. (3) Every candidate is forced to review-only, and the API refuses a
one-click "apply everything this tier matches" request with no explicit, human-picked paths.
Power mode — today's full behavior — becomes an explicit, typed, case-sensitive, logged opt-in
(`"I understand this can permanently delete files"`, checked for an *exact* match, no fuzzy
matching, no partial credit for close enough), reversible back to safe with no gate at all,
because becoming more conservative is never the dangerous direction.

**The boundary had to survive being compiled, not just written.** A source-level test suite
proving all of the above was necessary but not sufficient — a Nuitka-compiled, Inno-Setup-
installed `reclaim.exe` is a different artifact than the Python this project has tested all
along, and nothing guarantees a packager's static analysis, module bundling, or installer
scripting preserves a safety property by accident. So the same 13-check proof ran twice against
the actual compiled binary, not the dev tree: once against the raw Nuitka `--standalone` build,
once through a real silent install → launch → silent uninstall cycle of the built installer
(zero admin prompts at any step, matching this project's pre-existing "never runs elevated"
invariant end to end) — fresh install defaults to safe, a real `--apply` batch against a
config.toml explicitly trying to re-enable the forced-off categories still resolves to
recycle-bin-only, and the typed-confirmation phrase is confirmed to be the only door to power
mode, in the compiled artifact, not just the source.

**The installer's own size became a measured decision, not a guess.** Two clean, isolated
installs — no dev tooling, no assumptions — put core-only Reclaim at **13.6MB** and the full
applied-AI dependency stack at **1,041.8MB**, with `torch` alone accounting for 464MB of that,
shared by both the document-dedup and the CLIP-grouping features, so the "ship a little AI, hold
back the heavy AI" split this pass was asked to consider turned out not to exist as a clean
line — disclosed as a real finding, not engineered around silently. The installer that shipped
carries the deterministic engine only (**18.2MB**, unsigned); every AI feature in this project is
recommend-only or browse-only by construction, so a stranger's first install loses nothing
essential by not carrying ~1GB of machine-learning dependencies it can add later with one
command if it wants them.

**Unsigned, and said so out loud.** Azure Trusted Signing was priced (~$9.99/month) and
rejected for now — there's no revenue or user base yet to justify a recurring cost against, and
nothing about shipping unsigned today forecloses signing later (the packaging pipeline has no
signing directive one way or the other, so adding one is additive, not a rework). The
consequence — a SmartScreen "more info → run anyway" click, and a real, previously-observed risk
of an antivirus engine quarantining a freshly-built, unsigned binary — is documented plainly in
the README as an expected first-run experience, not left as a surprise for the first stranger to
hit it.

## Five invariants the harness now enforces structurally, not narratively

Five incidents across four features and one distribution-trust boundary produced five rules
that no longer depend on anyone remembering them:

1. **Precise-but-useless is not a passing gate.** `select_operating_point` requires a recall
   floor alongside the precision target — a threshold that's precise because it flags almost
   nothing is rejected, not accepted, the exact failure that let Feature 1a's first operating
   point clear CI while catching under 8% of real duplicates.
2. **Never pool precision/recall across a declared tier boundary.** `select_operating_point_
   per_tier`/`select_joint_operating_point_per_tier` compute every tier's metrics from only
   that tier's own data, with a regression test reproducing the exact 0.9524-pooled/
   0.8634-real-templated-tier incident that motivated it — permanently, so the mistake can't
   silently recur in a future eval file.
3. **A number is only "MEASURED" if the distribution behind it is honestly realistic.**
   `assert_safe_to_promote_to_measured` refuses that word for any adversarial-tail-only,
   synthetic-only, *or LLM-consensus-only* distribution — Copydays' `strong` split, the
   content-tag classifier's partly-synthetic fixture, and the clutter-likelihood ranker's
   cross-LLM-labeled data all stay honestly labeled PROVISIONAL, forever, structurally, not by
   discipline.
4. **A track can be mechanically deletion-eligible and still withhold the keeper flag under a
   specific, testable disagreement condition.** Version-chain's filename-vs-mtime conflict
   check and screenshot-burst's mixed-content-tag check are the same pattern applied twice —
   an AICluster only ever suggests deletion once a keeper has actually been identified, and
   the orchestration is free to simply never identify one when the safety condition isn't met.
5. **A safety mode is a boundary provable the same way a deletion-eligibility gate is provable,
   not a default value.** Safe mode's recycle-bin-only guarantee is proven by monkeypatching
   the actual destructive primitives (`os.unlink`, `shutil.rmtree`) to raise if ever called, the
   identical technique the AI layer's `apply_batch`/`send2trash` import scan already used to
   prove its own boundary — one proof technique, applied at two different layers of the same
   codebase, not two different standards of rigor.

## Honest metrics

| metric | value | source |
|---|---|---|
| Real disk-free reclaimed (measured, before/after `shutil.disk_usage`) | **36,216,430,592 bytes — 33.73GB** | sum of three real applies' own before/after deltas: `data/real-disk-run/real_apply_report.txt` (10,270,556,160), `redo_real_apply.txt` (20,349,280,256), `purge_real_apply.txt` (5,596,594,176) — independently cross-checked byte-for-byte against the actual filesystem in `data/real-disk-run/headline_33_73GB_verification.txt`, not just the reports' own claims |
| exact_duplicate, pending (Recycle Bin, not yet real free space) | 4,205,571,147 bytes — 3.92GB | `data/real-disk-run/final_reconciliation.txt` — net of 186 files restored per ADR-0009; real only once the Recycle Bin is emptied, disclosed separately, never summed into the headline above |
| Unrelated vault content, also still pending (found while verifying the headline, not part of this apply) | 5,425,947,894 bytes — 5.05GB | `data/real-disk-run/quarantine/batch_1784296779_d5389247/` — 4 size-guard-downgraded `windows_temp` directories from an earlier apply, mid-30-day retention (expires 2026-08-16); confirmed on disk, confirmed excluded from the headline above, disclosed for completeness |
| exact_duplicate candidates applied | 10,134 succeeded / 10,247 selected / 113 failed (all explained: access-denied, file-in-use, long-path, vanished-in-race) | `data/real-disk-run/exact_duplicate_real_apply.txt` |
| Estimate corrections, same category | 48GB → 23.09GB → 4.26GB → 3.92GB | ADR-0006, ADR-0008, ADR-0009 |
| Public installer, core-only (deterministic engine, no AI deps) | 13.6MB installed / 18.2MB installer | ADR-0024, clean isolated `uv venv` install + built `reclaim-setup.exe`, this session |
| Public installer, full `[ai]` extra (measured, not shipped) | 1,041.8MB (`torch` alone 464MB) | ADR-0024, clean isolated `uv venv` install, this session |
| Safe-mode-survives-packaging proof, against the compiled binary | 13/13 checks, twice (raw build + real install/run/uninstall cycle) | `packaging/test_packaged_safe_mode.ps1`, ADR-0023 |
| Ranker cross-LLM label agreement | Fleiss' κ = 0.6768 (N=120); 79/120 (65.8%) unanimous, used for training | ADR-0021 |
| Ranker held-out performance (unanimous-subset, grouped split) | NDCG@5 = 0.9763 (floor 0.70); precision@3 = 1.0000 (floor 0.50) | ADR-0021 |
| Track B (CLIP) semantic grouping | BCubed precision 0.7897, recall 0.7143 at threshold 0.82 (98 images) | ADR-0022 |

Every number above traces to a specific file in `data/real-disk-run/` produced by the actual
run it describes — none of them is a recomputation or a rounded restatement. The 33.73GB
headline was independently re-derived from the raw before/after numbers, not copy-pasted from
an earlier claim, specifically to check it wasn't quietly counting bytes that had only moved
(vault or Recycle Bin) rather than been freed — it wasn't; both pending pools above are real,
separate, and neither one is hiding inside it. Nothing here is a final total: `model_caches` and
several other reviewed categories remain unapplied by design, and both pending figures above
stay pending numbers, not freed ones, until someone empties them.
