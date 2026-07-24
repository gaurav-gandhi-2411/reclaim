# 0028. Model weight revision pinning and content-hash integrity verification

## Context

A production-readiness audit flagged two related supply-chain findings against the AI layer's
two pretrained-weight loading sites:

- **E17**: `src/reclaim/ai/image_embeddings.py` (`open_clip.create_model_and_transforms(
  "ViT-B-32-quickgelu", pretrained="openai")`, ADR-0022) and `src/reclaim/ai/
  text_embeddings.py` (`SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")`,
  ADR-0017) both resolve their Hugging Face Hub source via the **mutable default branch**
  (`main`), with no `revision=` pin. A future repoint of `main` on either repo would silently
  change what gets downloaded, with no way to detect or prevent it.
- **E18**: model-download integrity was asserted to rely on size-check only, not a content
  hash. The audit asked us to verify what actually happens on a partial/interrupted download
  (does `huggingface_hub`'s own caching already handle this?) and add content-hash
  verification of the downloaded weights where this repo controls that path.

## Decision

### E17 — pin both models to an explicit commit hash

**How the revisions were determined**: network-resolved via `HfApi().model_info(repo_id).sha`
against both repos, **cross-checked** against this machine's local HF Hub cache
(`~/.cache/huggingface/hub/models--.../refs/main`, which records what `main` last resolved to)
— both methods agreed exactly, for both repos:

| repo | pinned revision (commit hash) | resolution method |
|---|---|---|
| `timm/vit_base_patch32_clip_224.openai` | `a6f597a30f7b82c51704746581f9a4e41421e878` | `HfApi().model_info(...).sha` (network) == local `refs/main` (cache) |
| `sentence-transformers/all-MiniLM-L6-v2` | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` | `HfApi().model_info(...).sha` (network) == local `refs/main` (cache) |

**A finding that changed the fix's shape**: the CLIP checkpoint's actual HF Hub repo is
**`timm/vit_base_patch32_clip_224.openai`**, not the more obviously-named
`openai/clip-vit-base-patch32` — `open_clip`'s `"ViT-B-32-quickgelu"` + `pretrained="openai"`
tag resolves to it via `open_clip.pretrained.get_pretrained_cfg(...)["hf_hub"]`, verified
directly against the installed `open_clip==3.3.0` source (the exact version pinned in
`uv.lock`), not assumed from the checkpoint's common name.

**A second finding that changed the fix's shape**: `open_clip==3.3.0`'s tag-based
`pretrained="openai"` loading path has **no `revision=` parameter anywhere** —
`create_model_and_transforms` → `create_model` → `download_pretrained(cfg, cache_dir=
cache_dir)` → `download_pretrained_from_hf(model_id, cache_dir=cache_dir)` calls
`hf_hub_download(..., revision=None)` unconditionally, i.e. it always requests HF Hub's
mutable `main` ref. There is no public API to make this call site request a specific
revision. Two options were considered:

| option | verdict |
|---|---|
| Monkeypatch `open_clip.pretrained.download_pretrained_from_hf` to inject `revision=` | Rejected — patches private library internals not covered by open_clip's own compatibility guarantees; fragile across minor-version bumps, and violates this project's own "no reimplementation of a third-party internal" discipline (cf. ADR-0011's "shared, reused `SafetyValidator`" reasoning, applied in reverse — don't reach into someone else's internals either). |
| Bypass `pretrained="openai"` resolution entirely: download the checkpoint file ourselves via our own `huggingface_hub.hf_hub_download(revision=<pinned SHA>)` call, verify it (E18), and hand `create_model_and_transforms` the resulting **concrete local file path** via `pretrained=<path>`, reproducing the tag's exact preprocessing config (`mean`/`std`/`interpolation`/`resize_mode`/`quick_gelu`) via `open_clip.get_pretrained_cfg("ViT-B-32-quickgelu", "openai")` (a public, stable API) instead of the tag lookup the file-path branch skips. | **Selected.** |

`sentence_transformers.SentenceTransformer.__init__` **does** accept `revision=` directly (no
bypass needed) — `text_embeddings.py`'s fix is the simple, intended one: `SentenceTransformer(
_MODEL_NAME, revision=_MODEL_REVISION)`.

**Verified zero regression**: a side-by-side script loaded the model both the old way
(`pretrained="openai"`) and the new way (pinned path + explicit preprocess kwargs) in the same
process and compared the preprocessing tensor and the encoded feature vector for a test image
— both were **exactly bit-identical** (`torch.equal(...)` true, max abs diff `0.0`), proving
the new loading path reproduces the old one exactly; it only changes *where* the weight bytes
come from, not what they are or how they're used. ADR-0022's measured BCubed operating point
(precision 0.7897 / recall 0.7143 @ threshold 0.82) is therefore unaffected.

**A side benefit, not the goal**: pinning to a full 40-character commit hash also makes
`huggingface_hub`'s own cache-hit fast path apply ("if user provides a commit_hash and they
already have the file on disk, shortcut everything" — `file_download.py`), so once cached,
these loads make **zero** network calls, even to check whether `main` has moved. The
*previous*, unpinned code actually made a live metadata request on every load (to resolve
`main`'s current commit) — ADR-0022's claim of "no network access at runtime once cached" was
aspirational, not literally true, until this fix.

### E18 — is HF Hub's own download path already safe against partial/corrupted downloads?

**Yes, for atomicity — verified directly against the installed `huggingface_hub==1.24.0`
source** (the exact version pinned in `uv.lock`), not assumed:

- Downloads write to a process-unique `<blob>.<uuid>.incomplete` file, never the shared
  `.incomplete` name directly (`file_download.py`'s `_download_to_tmp_and_move`) — a broken
  `flock` on some filesystems costs duplicated bandwidth, never a corrupted shared file.
- `http_get` raises `OSError` ("Consistency check failed") if the downloaded byte count
  doesn't match the server-declared `Content-Length`, **before** the file is moved into place.
- The move into the content-addressed `blobs/<etag>` path is an atomic rename
  (`_chmod_and_move`), and only a fully-written, size-verified temp file is ever renamed. A
  genuinely partial/interrupted download is *never* left at a path a caller could load from as
  if it were complete — this part of E18's premise does not hold as a gap in this codebase's
  actual dependency; it's already correct, and no additional atomicity layer was added on top
  of it (inventing a fix for a problem that doesn't exist would be dishonest).

**No, for independent content-hash verification against a known-good digest — a real, if
narrow, gap**: HF Hub's blob storage key (its `etag`) is *server-declared* — for git-lfs
files (i.e. all model weight binaries) the etag genuinely is the file's sha256, but the client
never independently recomputes it and compares it to a value **this codebase** has separately
recorded as known-good. A malicious/compromised Hub server (or a compromised intermediary
capable of also forging the etag response) could in principle serve different, correctly-sized
bytes under the same etag without the existing size check ever catching it. This is the gap
E18 is actually asking about, and it's real, even though it's a different (and much narrower)
threat than "partial download."

**Fix**: `_verify_checkpoint_sha256_or_quarantine` (`image_embeddings.py`) and
`_verify_pinned_weights_or_quarantine` (`text_embeddings.py`) hash the downloaded weights file
and compare it to a hardcoded, pinned-at-audit-time SHA-256 digest recorded in this ADR and in
the source. On mismatch: **delete the bad blob** (quarantine — it is never left in the cache
to be silently reused as valid on a subsequent call) and raise a `RuntimeError` naming the
expected vs. actual digest. Both pinned digests were independently cross-checked against the
Hub's own server-declared LFS metadata (`HfApi().model_info(..., files_metadata=True).siblings
[...].lfs.sha256`) at the pinned revision, not merely recomputed from a local copy that could
itself already have been wrong:

| repo | weights file | sha256 | size (bytes) |
|---|---|---|---|
| `timm/vit_base_patch32_clip_224.openai` | `open_clip_model.safetensors` | `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31` | 605,143,284 |
| `sentence-transformers/all-MiniLM-L6-v2` | `model.safetensors` | `53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db` | 90,868,376 |

## Consequences

- Two new call sites (`huggingface_hub.hf_hub_download`) added to
  `_ALL_GATED_MODULE_NAMES`/the AST-scanned `require()` block list in
  `tests/test_ai_optional_extra.py` — `huggingface_hub` is imported lazily via `require()`,
  same discipline as every other AI-layer optional dependency (it was already a transitive
  dependency of `open-clip-torch`/`sentence-transformers`, same pattern as `numpy`/`torch`
  being explicit `require()` call sites despite also being transitive deps of other gated
  packages).
- `image_embeddings.py`'s CLIP loading path is now noticeably more code than a one-line
  `revision=` kwarg (which is all `text_embeddings.py` needed) — a direct consequence of
  `open_clip==3.3.0` genuinely not exposing that parameter on the tag-based path, not a
  design preference. If a future `open_clip` version adds `revision=` support to
  `create_model`/`create_model_and_transforms`, this can be simplified back down; tracked
  here, not as a TODO comment that could get lost.
- Neither model's dependency *version* is touched by this ADR — `open-clip-torch==3.3.0`/
  `sentence-transformers==5.6.0` stay exactly as `uv.lock` already resolved them. Freezing the
  preprocessing recipe (mean/std/interpolation) against a *future* `open_clip` upgrade
  changing its built-in tag config is a different, broader hardening question (dependency
  pinning, already handled at the `uv.lock` layer) and is explicitly out of this ADR's scope —
  `_model_and_preprocess` reads `get_pretrained_cfg` dynamically so it reproduces exactly what
  today's pinned `open_clip` version would have done, no more, no less.
- Bumping either pinned revision in the future (e.g. adopting a genuinely newer/better
  checkpoint) requires updating both the revision constant AND the sha256 constant together,
  and re-verifying with the same side-by-side embedding-identity script used here if the
  change is meant to be preprocessing-neutral — otherwise `ImageEmbeddingCache`'s
  `model_id`-keyed cache (ADR-0022) already forces a clean cache miss on any such change, so a
  stale embedding is never silently reused across the swap either way.

## Alternatives considered

- **Rely on `HF_HUB_OFFLINE=1` + a locally-written `refs/main` pointing at the pinned commit**,
  so `open_clip`'s own `revision=None` ("main") resolution would be forced to resolve to our
  pinned commit without a network round-trip. Rejected: this requires writing to the *shared*,
  system-wide HF Hub cache directory (`~/.cache/huggingface/hub/.../refs/main`) that every
  other tool/process on the machine also reads and writes — a real blast-radius concern
  outside this repo's own files, and fragile against any other process on the same machine
  legitimately re-resolving `main` with real network access and overwriting that pointer.
- **A live `HfApi().model_info(...).sha` network check on every model load**, refusing to
  proceed if it no longer matches the pinned constant. Rejected: this would make a network
  call *mandatory* on every load even when the weights are already fully cached locally,
  regressing ADR-0022's "no network access at runtime once cached" design goal instead of
  fixing it — the opposite of what this ADR's E17 fix achieves as a side benefit.
- **Hardcoding open_clip's preprocessing values as separate magic-number constants** instead of
  reading them from `open_clip.get_pretrained_cfg` at call time. Rejected: duplicating
  `mean`/`std`/`interpolation` as literals risks a transcription error and a second source of
  truth to keep in sync; reading them from the same function `open_clip` itself uses for the
  tag lookup means there is exactly one place these values live.

## Test coverage

`tests/test_ai_image_embeddings.py` — 3 new cases: pinned-revision + sha256 wiring proven via
mocked `require()` return values (asserting the exact `hf_hub_download(repo_id=, filename=,
revision=)` call and the exact `create_model_and_transforms(pretrained=, force_quick_gelu=,
image_mean=, ...)` call), a checksum-mismatch quarantine case, and a checksum-match pass-through
case. All 9 pre-existing real-embedding tests in this file continue to pass unmodified,
including with the `ai` extra genuinely installed and network available in this environment —
exercising the real pinned-download-and-verify path end to end, not just the mocked wiring.

`tests/test_ai_text_embeddings.py` — new file, 3 cases mirroring the above for
`text_embeddings.py`'s `_verify_pinned_weights_or_quarantine`/`_model`.

`tests/test_ai_optional_extra.py` — `_ALL_GATED_MODULE_NAMES` extended with
`huggingface_hub`; all 8 pre-existing cases (including the AST-based structural backstop that
would fail if a `require()` call site's module name were left off this list) continue to pass.

Full suite: `uv run pytest tests/ -q` — 627 passed, 2 skipped (pre-existing, unrelated to this
change), `uv run ruff check .` / `uv run ruff format --check .` / `uv run mypy` all clean.
