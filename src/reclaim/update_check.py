from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

# See PRIVACY.md's "Updates" section, kept in sync with this module -- this is the ONLY outbound
# network call this codebase's core product ever makes, and only when a user has explicitly
# opted in via config.toml's `[update_check] enabled = true` (default OFF; see
# `reclaim.config.UpdateCheckConfig`). The request itself carries no file/scan/user data -- it is
# a plain, anonymous GET to GitHub's public releases API for this one repo's latest tag, the same
# shape a browser visiting the public releases page would make.
GITHUB_REPO = "gaurav-gandhi-2411/reclaim"
_RELEASES_LATEST_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"

# Best-effort background nicety, not a critical path -- short timeout, zero retries. A slow/dead
# GitHub API must never make the dashboard feel slow.
_REQUEST_TIMEOUT_SECONDS = 2.5

# Once per process is the real floor (`_Cache` below); this is the additional wall-clock TTL on
# top of that, so a long-running `reclaim serve` process re-checks at most once a day rather than
# on every single page load / API poll -- both wasteful and a bad API citizen for a tool with
# potentially many installs hitting the same public endpoint.
CACHE_TTL_SECONDS = 24 * 60 * 60.0


@dataclass(frozen=True)
class UpdateCheckResult:
    """Outcome of a best-effort GitHub release check.

    `status` is `"ok"` when the check actually reached GitHub and got a parseable response,
    `"unknown"` for every failure mode (network down, DNS failure, timeout, non-2xx response,
    malformed/unexpected JSON) -- callers render `"unknown"` as "couldn't check right now", never
    as an error. This type deliberately has no way to represent a raised exception -- see
    `check_for_update`'s docstring for why that's a hard invariant, not a convenience.
    """

    status: str  # "ok" | "unknown"
    current_version: str
    latest_version: str | None
    update_available: bool
    release_url: str
    checked_at: float  # unix timestamp (time.time()) this result was produced -- display only.


def _parse_version(text: str) -> tuple[int, ...] | None:
    """Parses a dotted numeric version (`"1.3.0"`, tolerating a leading `"v"` -- GitHub's tag
    convention -- and any trailing pre-release/build suffix split off at the first `-`/`+`).
    Returns `None` for anything that doesn't parse as a plain dotted-integer version; callers
    must treat that as "can't compare", never as an error."""
    cleaned = text.strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    cleaned = cleaned.split("-", 1)[0].split("+", 1)[0]
    parts = cleaned.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _is_newer(latest: str, current: str) -> bool:
    """`True` only when `latest` parses as a genuinely greater dotted-integer version than
    `current`. Any parse failure (either side) returns `False` -- silently "no update", never a
    crash or a false positive from a malformed/unexpected tag."""
    latest_parsed = _parse_version(latest)
    current_parsed = _parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    length = max(len(latest_parsed), len(current_parsed))
    # Pad the shorter tuple with zeros so e.g. (1, 3) vs (1, 3, 0) compare equal, not "newer".
    latest_padded = latest_parsed + (0,) * (length - len(latest_parsed))
    current_padded = current_parsed + (0,) * (length - len(current_parsed))
    return latest_padded > current_padded


class _Cache:
    """Process-local, thread-safe cache for the most recent `UpdateCheckResult`.

    Deliberately in-memory only, not persisted to `data/` -- `reclaim serve` is a long-running
    process for the lifetime of one dashboard session, so "once per process, refreshed after
    `CACHE_TTL_SECONDS`" already satisfies "don't hit GitHub's API on every page load" without
    adding a new on-disk artifact (and its own small privacy footprint -- a file recording when
    Reclaim last phoned home) for a best-effort nicety. A fresh process (a new `reclaim serve`
    invocation) re-checks once, which is the expected/desired behavior, not a gap.

    Uses `time.monotonic()` for the TTL comparison (robust against wall-clock adjustments, NTP
    corrections, DST) -- `UpdateCheckResult.checked_at` is a separate `time.time()` wall-clock
    timestamp kept only for display, never used to gate re-fetching.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result: UpdateCheckResult | None = None
        self._cached_at_monotonic: float | None = None

    def get(self, ttl_seconds: float) -> UpdateCheckResult | None:
        with self._lock:
            if self._result is None or self._cached_at_monotonic is None:
                return None
            if time.monotonic() - self._cached_at_monotonic > ttl_seconds:
                return None
            return self._result

    def set(self, result: UpdateCheckResult) -> None:
        with self._lock:
            self._result = result
            self._cached_at_monotonic = time.monotonic()

    def clear(self) -> None:
        """Test-only reset -- production code never needs to invalidate the cache early."""
        with self._lock:
            self._result = None
            self._cached_at_monotonic = None


_cache = _Cache()


def _unknown_result(current_version: str) -> UpdateCheckResult:
    return UpdateCheckResult(
        status="unknown",
        current_version=current_version,
        latest_version=None,
        update_available=False,
        release_url=RELEASES_PAGE_URL,
        checked_at=time.time(),
    )


def _extract_release_info(payload: object) -> tuple[str, str]:
    """Pulls `(tag_name, release_url)` out of a parsed GitHub releases-API JSON body. Raises
    `ValueError` for anything that doesn't have a usable, non-blank `tag_name` string -- the
    caller's broad `except Exception` (see `check_for_update`) turns that into a graceful
    `status="unknown"` result, same as a network failure."""
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError(f"GitHub releases API response had no usable tag_name: {payload!r}")
    html_url = payload.get("html_url") if isinstance(payload, dict) else None
    release_url = html_url if isinstance(html_url, str) and html_url else RELEASES_PAGE_URL
    return tag, release_url


def check_for_update(
    *,
    current_version: str,
    client: httpx.Client | None = None,
    force: bool = False,
    ttl_seconds: float = CACHE_TTL_SECONDS,
) -> UpdateCheckResult:
    """Best-effort check of GitHub's releases API for a newer tag than `current_version`.

    NEVER raises -- every failure mode (network down, DNS failure, timeout, non-2xx response,
    malformed/unexpected JSON body, a missing/blank `tag_name`) is caught here and turned into a
    `status="unknown"` result instead. This is a background nicety, not a critical path: nothing
    in the caller should ever have to wrap this in its own try/except, and nothing about the
    user's actual scan/apply/restore workflow can ever be slowed or blocked by this call.

    `client`: inject an `httpx.Client` (e.g. one built with `httpx.MockTransport`) for tests;
    defaults to a real short-timeout client. `force=True` bypasses the cache (used by callers that
    explicitly want a fresh check); the default respects the process-local cache described in
    `_Cache`'s docstring, so a caller can call this on every request/page load without it turning
    into a live network call every time.
    """
    if not force:
        cached = _cache.get(ttl_seconds)
        if cached is not None:
            return cached

    owns_client = client is None
    active_client = client if client is not None else httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        response = active_client.get(
            _RELEASES_LATEST_API_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        tag, release_url = _extract_release_info(payload)
        result = UpdateCheckResult(
            status="ok",
            current_version=current_version,
            latest_version=tag,
            update_available=_is_newer(tag, current_version),
            release_url=release_url,
            checked_at=time.time(),
        )
    except Exception:
        # Deliberately blind -- httpx.RequestError (network/DNS/timeout), httpx.HTTPStatusError
        # (non-2xx), json.JSONDecodeError, and the ValueError raised above for a malformed body
        # are all "GitHub didn't give us a usable answer," which this best-effort feature must
        # degrade gracefully from rather than propagate. Logged at info, not warning/error -- this
        # is expected background behavior for an offline machine, not a real problem.
        logger.info("update_check.failed", current_version=current_version, exc_info=True)
        result = _unknown_result(current_version)
    finally:
        if owns_client:
            active_client.close()

    _cache.set(result)
    return result
