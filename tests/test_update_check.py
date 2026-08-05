from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from reclaim import update_check
from reclaim.update_check import (
    RELEASES_PAGE_URL,
    UpdateCheckResult,
    _is_newer,
    _parse_version,
    check_for_update,
)

_CURRENT_VERSION = "1.3.0"


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Every real caller shares one process-local `_Cache` instance -- reset it before each test
    so cache state from one test can never leak into the next (this project's standing "zero
    live API calls in CI" convention needs this to also mean "zero cross-test cache leakage")."""
    update_check._cache.clear()


def _client_with_handler(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Builds a real `httpx.Client` wired to an in-memory `MockTransport` -- no real network
    call is ever possible through this client, matching the project's zero-live-API-calls-in-CI
    convention (see CLAUDE.md / MEMORY.md)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _counting_handler(
    response_factory: Callable[[], httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    """Wraps a response factory with a call counter, used to assert the cache actually prevented
    (or didn't prevent) a second network call."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return response_factory()

    return handler, calls


def _release_response(tag_name: str, html_url: str | None = None) -> httpx.Response:
    body = {"tag_name": tag_name}
    if html_url is not None:
        body["html_url"] = html_url
    return httpx.Response(200, json=body)


# --- Version parsing / comparison ---------------------------------------------------------------


def test_parse_version_handles_plain_and_v_prefixed() -> None:
    assert _parse_version("1.3.0") == (1, 3, 0)
    assert _parse_version("v1.3.0") == (1, 3, 0)
    assert _parse_version("V2.0") == (2, 0)


def test_parse_version_strips_prerelease_and_build_suffixes() -> None:
    assert _parse_version("1.4.0-rc1") == (1, 4, 0)
    assert _parse_version("1.4.0+build.5") == (1, 4, 0)


def test_parse_version_returns_none_for_unparseable_text() -> None:
    assert _parse_version("not-a-version") is None
    assert _parse_version("") is None
    assert _parse_version("v") is None


def test_is_newer_true_when_latest_strictly_greater() -> None:
    assert _is_newer("1.4.0", "1.3.0") is True
    assert _is_newer("v2.0.0", "1.9.9") is True


def test_is_newer_false_when_equal_or_older() -> None:
    assert _is_newer("1.3.0", "1.3.0") is False
    assert _is_newer("1.2.0", "1.3.0") is False


def test_is_newer_pads_shorter_tuple_before_comparing() -> None:
    # "1.3" vs "1.3.0" must compare EQUAL, not "1.3 is newer" -- a naive tuple comparison without
    # zero-padding would get this backwards ((1, 3) < (1, 3, 0) in plain Python tuple ordering).
    assert _is_newer("1.3", "1.3.0") is False


def test_is_newer_false_when_either_side_unparseable() -> None:
    assert _is_newer("not-a-version", "1.3.0") is False
    assert _is_newer("1.4.0", "not-a-version") is False


# --- check_for_update: update available / no update ----------------------------------------------


def test_check_for_update_reports_update_available() -> None:
    handler, calls = _counting_handler(
        lambda: _release_response("v1.4.0", "https://example.test/r")
    )
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result == UpdateCheckResult(
        status="ok",
        current_version=_CURRENT_VERSION,
        latest_version="v1.4.0",
        update_available=True,
        release_url="https://example.test/r",
        checked_at=result.checked_at,
    )
    assert len(calls) == 1


def test_check_for_update_reports_no_update_when_current_is_latest() -> None:
    handler, _calls = _counting_handler(lambda: _release_response(f"v{_CURRENT_VERSION}"))
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "ok"
    assert result.update_available is False


def test_check_for_update_falls_back_to_releases_page_url_when_html_url_missing() -> None:
    handler, _calls = _counting_handler(lambda: _release_response("v1.4.0"))
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.release_url == RELEASES_PAGE_URL


# --- Graceful fallback on every failure mode ------------------------------------------------------


def test_network_failure_falls_back_gracefully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS/network failure", request=request)

    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "unknown"
    assert result.update_available is False
    assert result.latest_version is None
    assert result.release_url == RELEASES_PAGE_URL
    assert result.current_version == _CURRENT_VERSION


def test_timeout_falls_back_gracefully() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "unknown"


def test_non_2xx_status_falls_back_gracefully() -> None:
    handler, _calls = _counting_handler(lambda: httpx.Response(500, text="internal error"))
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "unknown"
    assert result.update_available is False


def test_malformed_json_falls_back_gracefully() -> None:
    handler, _calls = _counting_handler(lambda: httpx.Response(200, text="not json at all {{{"))
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "unknown"


def test_missing_tag_name_falls_back_gracefully() -> None:
    handler, _calls = _counting_handler(lambda: httpx.Response(200, json={"name": "Release"}))
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "unknown"


def test_unexpected_json_shape_falls_back_gracefully() -> None:
    # A JSON array instead of an object -- .get() would raise AttributeError if not guarded.
    handler, _calls = _counting_handler(lambda: httpx.Response(200, text=json.dumps([1, 2, 3])))
    client = _client_with_handler(handler)

    result = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert result.status == "unknown"


# --- Caching --------------------------------------------------------------------------------------


def test_second_call_within_ttl_does_not_refetch() -> None:
    handler, calls = _counting_handler(lambda: _release_response("v1.4.0"))
    client = _client_with_handler(handler)

    first = check_for_update(current_version=_CURRENT_VERSION, client=client)
    second = check_for_update(current_version=_CURRENT_VERSION, client=client)

    assert len(calls) == 1
    assert second == first


def test_force_bypasses_cache() -> None:
    handler, calls = _counting_handler(lambda: _release_response("v1.4.0"))
    client = _client_with_handler(handler)

    check_for_update(current_version=_CURRENT_VERSION, client=client)
    check_for_update(current_version=_CURRENT_VERSION, client=client, force=True)

    assert len(calls) == 2


def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, calls = _counting_handler(lambda: _release_response("v1.4.0"))
    client = _client_with_handler(handler)

    fake_monotonic = [1000.0]
    monkeypatch.setattr(update_check.time, "monotonic", lambda: fake_monotonic[0])

    check_for_update(current_version=_CURRENT_VERSION, client=client, ttl_seconds=10.0)
    fake_monotonic[0] += 5.0  # still within the 10s TTL
    check_for_update(current_version=_CURRENT_VERSION, client=client, ttl_seconds=10.0)
    assert len(calls) == 1

    fake_monotonic[0] += 6.0  # now 11s since the cached fetch -- past the 10s TTL
    check_for_update(current_version=_CURRENT_VERSION, client=client, ttl_seconds=10.0)
    assert len(calls) == 2


def test_cache_shared_across_calls_without_explicit_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (no injected `client`) path builds and closes its own real `httpx.Client` per
    call -- verified here only via monkeypatching `httpx.Client` itself to a mock-transport
    factory, so this still never reaches the real network."""

    handler, calls = _counting_handler(lambda: _release_response("v1.4.0"))
    real_client_cls = httpx.Client
    monkeypatch.setattr(
        update_check.httpx,
        "Client",
        lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)),
    )

    first = check_for_update(current_version=_CURRENT_VERSION)
    second = check_for_update(current_version=_CURRENT_VERSION)

    assert len(calls) == 1
    assert first == second
