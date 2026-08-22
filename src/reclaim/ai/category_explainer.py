from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from reclaim.app_paths import data_root

logger = structlog.get_logger(__name__)

# R2 (per-category LLM explainer). Structural guarantee (spec'd by the build brief, enforced by
# `evals/test_ai_safety_gate.py` alongside every other module under `reclaim.ai`): this module
# NEVER receives or returns a path, a `Candidate`, or anything that could influence a delete
# decision. Its only input is `CategoryDescriptor` -- category name + AGGREGATE stats (file
# count, total bytes) -- never individual file paths or file content; its only output is
# `CategoryExplanation` -- a plain prose string. There is no field on either dataclass a caller
# could smuggle a path/selection into, and this module never imports `reclaim.executor` or
# `send2trash` (same AST-enforced guarantee every other `reclaim.ai` module has).
#
# This module never reads `ANTHROPIC_API_KEY` (or any other bare environment variable) as an
# implicit key source, and never will: it calls the Anthropic Messages API directly over
# `httpx` (already a project dependency -- see `reclaim.update_check`, the only other outbound
# HTTP call in this codebase) rather than importing the `anthropic` SDK. This is a deliberate
# choice, not an oversight -- the `anthropic` SDK's `Anthropic()` client falls back to reading
# `ANTHROPIC_API_KEY` from the environment when no `api_key` is passed explicitly, which is
# exactly the accidental-fallback path this project's key-handling requirement forbids (the
# user is on a Claude Max plan and must never have this app silently pick up that unrelated
# credential). Using raw `httpx` means there is no SDK-level fallback to guard against in the
# first place -- every call in this module takes `api_key` as an explicit, required argument,
# sourced by the caller from `reclaim.anthropic_key_store.load_key` (the DPAPI-decrypted,
# explicitly-entered-in-app key), never from `os.environ`.
#
# NEVER logged: the API key, the prompt (which never contains anything beyond the aggregate
# descriptor fields anyway), and the response prose are never passed to `structlog` here --
# only content-free metadata (model id, token counts) is logged, matching this project's
# `usd_cost`/`tokens_in`/`tokens_out` observability convention.
#
# MODEL ID NOTE: this session did not have access to a reference for Anthropic's current model
# catalog/pricing (the `claude-api` skill was unavailable in this environment). `DEFAULT_MODEL`
# below is a real, previously-documented Anthropic model id (Claude 3.5 Haiku) chosen for being
# cheap and stable -- it is NOT verified to be Anthropic's current cheapest/recommended model as
# of this feature's ship date. Treat it as a placeholder: verify against
# https://docs.anthropic.com/en/docs/about-claude/models before relying on it, and update this
# constant (or make it a Settings-configurable field) once verified. `explain_category`/
# `validate_api_key` both accept an explicit `model` override for exactly this reason.

DEFAULT_MODEL = "claude-3-5-haiku-20241022"  # see MODEL ID NOTE above -- unverified this session
_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_REQUEST_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_TOKENS = 500

# CWD-independent (see reclaim.app_paths.data_root's docstring, and PR #51 for the original
# confirmed-live crash this class of bug caused elsewhere) -- not yet reachable from any
# working-directory-less invocation today, but "not reachable today" is a property of today's
# call sites, not of the code.
DEFAULT_CACHE_DIR = data_root() / "data" / "ai_explanations"


@dataclass(frozen=True, slots=True)
class CategoryDescriptor:
    """The ONLY input `explain_category` ever accepts -- aggregate, category-level statistics.

    Structurally incapable of carrying an individual file path or file content: there is no
    `paths`/`sample_files`/`candidates` field here, and none should ever be added -- that would
    break the exact guarantee this module exists to provide (see module docstring). `tier` and
    `retention_days` are included only because they're useful context for the "what does
    deleting this cost me" prose, not because this module makes or influences any tier/retention
    decision itself (those are ALWAYS decided elsewhere -- `reclaim.config`/`reclaim.detectors`).
    """

    category_group: str
    display_name: str
    file_count: int
    total_size_bytes: int
    tier: str  # "A" | "B" | "both" -- display-only, mirrors CategoryCardOut's own tier field
    retention_days: int | None


@dataclass(frozen=True, slots=True)
class CategoryExplanation:
    """The ONLY output `explain_category` ever returns -- a plain prose string. No path, no
    selection, no field resembling `Candidate`/`AICluster` -- nothing here could be fed into
    `apply_batch` even by mistake (there is no compatible field to smuggle it through)."""

    category_group: str
    explanation: str
    cached: bool  # True if this came from data/ai_explanations/ without a fresh API call


class AnthropicKeyMissingError(RuntimeError):
    """Raised by `explain_category`/`validate_api_key` when no API key was supplied."""


class AnthropicAPIError(RuntimeError):
    """Raised when the Anthropic API call itself fails (network failure, non-2xx response, or a
    response body this module can't parse). Never wraps or includes the API key."""


def _fingerprint(descriptor: CategoryDescriptor) -> str:
    """Cache key: a hash of the descriptor's own fields only -- deliberately NOT a scan id or a
    timestamp, so a re-scan that finds the exact same category totals is a guaranteed cache hit
    (zero tokens spent) and a genuinely changed category (different file_count/total_size_bytes)
    is a guaranteed cache miss. `display_name` is excluded on purpose -- it's a cosmetic label
    derived from `category_group` (see `schemas.category_label`) and never changes independently
    of it, so including it would only ever create spurious cache misses."""
    payload = json.dumps(
        {
            "category_group": descriptor.category_group,
            "file_count": descriptor.file_count,
            "total_size_bytes": descriptor.total_size_bytes,
            "tier": descriptor.tier,
            "retention_days": descriptor.retention_days,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _cache_path(descriptor: CategoryDescriptor, cache_dir: Path) -> Path:
    return cache_dir / f"{_fingerprint(descriptor)}.json"


def _read_cache(path: Path) -> CategoryExplanation | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CategoryExplanation(
            category_group=data["category_group"],
            explanation=data["explanation"],
            cached=True,
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        # A corrupted/hand-edited cache file is treated as a miss (re-fetch, overwrite), never a
        # crash -- same "degrade gracefully" posture as every other cache/state file this
        # codebase reads (e.g. `mode.py`'s handling of a malformed mode log line).
        logger.warning("ai.category_explainer.cache_corrupted", cache_path=str(path))
        return None


def _write_cache(path: Path, explanation: CategoryExplanation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"category_group": explanation.category_group, "explanation": explanation.explanation}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _build_prompt(descriptor: CategoryDescriptor) -> str:
    size_gib = descriptor.total_size_bytes / (1024**3)
    retention = (
        "deleted immediately with no recovery window"
        if descriptor.retention_days is None
        else f"recoverable for {descriptor.retention_days} days after deletion (vaulted, not "
        "immediately permanent)"
    )
    return (
        "A disk-cleanup tool found a category of files on the user's computer called "
        f"'{descriptor.display_name}' (internal id: {descriptor.category_group}): "
        f"{descriptor.file_count} files, {size_gib:.2f} GiB total, safety tier "
        f"{descriptor.tier}, {retention}. In 3-5 short sentences aimed at a non-technical "
        "user, cover: (1) what this general category of files typically is, (2) the pros of "
        "deleting it now, (3) the cons or risks of deleting it, (4) roughly what it costs "
        "(time, bandwidth, re-setup effort) to regenerate later if needed. You were not given "
        "any specific file name or path -- do not invent or reference one. Respond as plain "
        "prose only: no markdown headers, no bullet points, no code blocks."
    )


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }


def _call_messages_api(prompt: str, *, api_key: str, model: str, http_client: httpx.Client) -> str:
    """One Anthropic Messages API call. Returns the plain-text response. Never logs `api_key`,
    the prompt, or the response text -- only content-free metadata (model id, token counts)."""
    body: dict[str, object] = {
        "model": model,
        "max_tokens": _MAX_RESPONSE_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = http_client.post(
            _ANTHROPIC_MESSAGES_URL,
            headers=_headers(api_key),
            json=body,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise AnthropicAPIError(f"Anthropic API request failed: {exc}") from exc

    if response.status_code != 200:
        raise AnthropicAPIError(f"Anthropic API returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise AnthropicAPIError("Anthropic API returned a non-JSON response") from exc

    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    logger.info(
        "ai.category_explainer.api_call",
        model=model,
        tokens_in=usage.get("input_tokens") if isinstance(usage, dict) else None,
        tokens_out=usage.get("output_tokens") if isinstance(usage, dict) else None,
    )

    content_blocks = data.get("content", []) if isinstance(data, dict) else []
    text_parts = [
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(text_parts).strip()
    if not text:
        raise AnthropicAPIError("Anthropic API returned no text content")
    return text


def explain_category(
    descriptor: CategoryDescriptor,
    *,
    api_key: str | None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    http_client: httpx.Client | None = None,
    model: str = DEFAULT_MODEL,
) -> CategoryExplanation:
    """The one entry point this module exposes for generating a prose explanation of a cleanup
    category. Cache-first: an unchanged category (same `_fingerprint`) NEVER triggers a second
    API call -- see `_fingerprint`'s docstring. Raises `AnthropicKeyMissingError` if `api_key`
    is `None`/empty and nothing is cached yet; raises `AnthropicAPIError` if the API call itself
    fails. Callers (the API layer) are expected to catch both and degrade gracefully -- this
    function itself never returns a placeholder/fake explanation on failure."""
    cache_path = _cache_path(descriptor, cache_dir)
    cached = _read_cache(cache_path)
    if cached is not None:
        return cached

    if not api_key:
        raise AnthropicKeyMissingError(
            "no Anthropic API key configured -- add one in Settings to enable AI category "
            "explanations"
        )

    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.Client()
    try:
        prompt = _build_prompt(descriptor)
        text = _call_messages_api(prompt, api_key=api_key, model=model, http_client=client)
    finally:
        if owns_client:
            client.close()

    explanation = CategoryExplanation(
        category_group=descriptor.category_group, explanation=text, cached=False
    )
    _write_cache(cache_path, explanation)
    return explanation


def validate_api_key(api_key: str, *, http_client: httpx.Client | None = None) -> bool:
    """Cheapest reasonable validation of a candidate key before any real spend -- lists
    available models rather than generating a completion (no completion-token cost). Returns
    `True` iff the key authenticates (HTTP 200); `False` for an authentication failure (401/403,
    "bad key," not "couldn't reach Anthropic"); raises `AnthropicAPIError` for anything else
    (network failure, unexpected status) so a caller can tell the two apart."""
    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.Client()
    try:
        try:
            response = client.get(
                _ANTHROPIC_MODELS_URL,
                headers=_headers(api_key),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise AnthropicAPIError(f"Anthropic API request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code == 200:
        return True
    if response.status_code in (401, 403):
        return False
    raise AnthropicAPIError(f"Anthropic API returned unexpected HTTP {response.status_code}")
