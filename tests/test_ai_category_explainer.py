from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from reclaim.ai.category_explainer import (
    DEFAULT_MODEL,
    AnthropicAPIError,
    AnthropicKeyMissingError,
    CategoryDescriptor,
    CategoryExplanation,
    _fingerprint,
    explain_category,
    validate_api_key,
)

_DESCRIPTOR = CategoryDescriptor(
    category_group="dev_artifacts",
    display_name="Rebuildable developer files",
    file_count=1234,
    total_size_bytes=5 * 1024**3,
    tier="A",
    retention_days=None,
)


def _client_with_handler(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Same MockTransport convention `tests/test_update_check.py` already establishes for this
    project's other outbound-HTTP module -- no real network call is ever possible through this
    client (project convention: zero live API calls in CI)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _counting_handler(
    response_factory: Callable[[], httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return response_factory()

    return handler, calls


def _messages_response(text: str = "Some plain-prose explanation.") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 42, "output_tokens": 17},
        },
    )


# --- Structural guarantee: never a path in, never a path out -----------------------------------


def test_category_descriptor_has_no_path_or_candidate_shaped_field() -> None:
    """Type-level proof (not just a docstring claim): `CategoryDescriptor`'s field set is
    exactly the aggregate-stat fields it's documented to carry -- no `path`, `paths`,
    `sample_files`, or anything that could carry an individual file identity."""
    field_names = {f.name for f in dataclasses.fields(CategoryDescriptor)}
    assert field_names == {
        "category_group",
        "display_name",
        "file_count",
        "total_size_bytes",
        "tier",
        "retention_days",
    }
    forbidden_substrings = ("path", "candidate", "file_name", "filename", "selection")
    for name in field_names:
        assert not any(bad in name.lower() for bad in forbidden_substrings), name


def test_category_explanation_has_no_path_or_candidate_shaped_field() -> None:
    """Same structural proof for the OUTPUT side: `CategoryExplanation` cannot carry a path or
    anything resembling a delete instruction -- just prose, an id, and a cache-hit flag."""
    field_names = {f.name for f in dataclasses.fields(CategoryExplanation)}
    assert field_names == {"category_group", "explanation", "cached"}
    forbidden_substrings = ("path", "candidate", "file_name", "filename", "selection")
    for name in field_names:
        assert not any(bad in name.lower() for bad in forbidden_substrings), name


def test_explain_category_signature_accepts_only_a_descriptor_and_never_a_path_kwarg() -> None:
    """Static confirmation alongside the field-level proofs above: `explain_category`'s
    parameter list has no `path`/`candidate`-shaped argument a caller could smuggle one through
    even if the dataclasses themselves were bypassed."""
    params = inspect.signature(explain_category).parameters
    forbidden = {"path", "paths", "candidate", "candidates", "selection"}
    assert forbidden.isdisjoint(params.keys())


# --- Cache behavior: an unchanged fingerprint never makes a second API call --------------------


def test_explain_category_cache_hit_makes_zero_api_calls(tmp_path: Path) -> None:
    handler, calls = _counting_handler(_messages_response)
    client = _client_with_handler(handler)
    cache_dir = tmp_path / "ai_explanations"

    first = explain_category(
        _DESCRIPTOR, api_key="sk-ant-fake", cache_dir=cache_dir, http_client=client
    )
    assert first.cached is False
    assert len(calls) == 1

    second = explain_category(
        _DESCRIPTOR, api_key="sk-ant-fake", cache_dir=cache_dir, http_client=client
    )
    assert second.cached is True
    assert second.explanation == first.explanation
    assert len(calls) == 1  # no second network call for the unchanged fingerprint


def test_explain_category_changed_stats_are_a_cache_miss(tmp_path: Path) -> None:
    """A genuinely different category (different file_count/total_size_bytes) must NOT reuse a
    cached entry from a different fingerprint -- proves the cache key is real, not a constant."""
    handler, calls = _counting_handler(_messages_response)
    client = _client_with_handler(handler)
    cache_dir = tmp_path / "ai_explanations"

    explain_category(_DESCRIPTOR, api_key="sk-ant-fake", cache_dir=cache_dir, http_client=client)
    changed = dataclasses.replace(_DESCRIPTOR, file_count=_DESCRIPTOR.file_count + 1)
    explain_category(changed, api_key="sk-ant-fake", cache_dir=cache_dir, http_client=client)
    assert len(calls) == 2


def test_explain_category_cache_hit_needs_no_api_key_at_all(tmp_path: Path) -> None:
    """A cached explanation must be servable even with no key configured -- the cache-first
    check happens before the `api_key` presence check."""
    handler, calls = _counting_handler(_messages_response)
    client = _client_with_handler(handler)
    cache_dir = tmp_path / "ai_explanations"

    explain_category(_DESCRIPTOR, api_key="sk-ant-fake", cache_dir=cache_dir, http_client=client)
    result = explain_category(_DESCRIPTOR, api_key=None, cache_dir=cache_dir, http_client=client)
    assert result.cached is True
    assert len(calls) == 1


def test_fingerprint_is_stable_and_deterministic() -> None:
    assert _fingerprint(_DESCRIPTOR) == _fingerprint(_DESCRIPTOR)
    other = dataclasses.replace(_DESCRIPTOR, total_size_bytes=_DESCRIPTOR.total_size_bytes + 1)
    assert _fingerprint(_DESCRIPTOR) != _fingerprint(other)


def test_fingerprint_ignores_display_name_changes() -> None:
    """`display_name` is a cosmetic label derived from `category_group` -- it must not affect
    the cache key (see `_fingerprint`'s docstring)."""
    renamed = dataclasses.replace(_DESCRIPTOR, display_name="A totally different label")
    assert _fingerprint(_DESCRIPTOR) == _fingerprint(renamed)


# --- Failure modes: no key, API failure -- both must degrade, never crash into a fake answer ---


def test_explain_category_raises_when_no_key_and_no_cache(tmp_path: Path) -> None:
    with pytest.raises(AnthropicKeyMissingError):
        explain_category(_DESCRIPTOR, api_key=None, cache_dir=tmp_path / "ai_explanations")


def test_explain_category_raises_when_no_key_is_empty_string(tmp_path: Path) -> None:
    with pytest.raises(AnthropicKeyMissingError):
        explain_category(_DESCRIPTOR, api_key="", cache_dir=tmp_path / "ai_explanations")


def test_explain_category_raises_anthropic_api_error_on_non_200(tmp_path: Path) -> None:
    handler, _ = _counting_handler(lambda: httpx.Response(401, json={"error": "bad key"}))
    client = _client_with_handler(handler)
    with pytest.raises(AnthropicAPIError):
        explain_category(
            _DESCRIPTOR,
            api_key="sk-ant-bad",
            cache_dir=tmp_path / "ai_explanations",
            http_client=client,
        )


def test_explain_category_raises_anthropic_api_error_on_network_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    client = _client_with_handler(handler)
    with pytest.raises(AnthropicAPIError):
        explain_category(
            _DESCRIPTOR,
            api_key="sk-ant-fake",
            cache_dir=tmp_path / "ai_explanations",
            http_client=client,
        )


def test_explain_category_raises_anthropic_api_error_on_empty_text_content(tmp_path: Path) -> None:
    handler, _ = _counting_handler(lambda: _messages_response(text=""))
    client = _client_with_handler(handler)
    with pytest.raises(AnthropicAPIError):
        explain_category(
            _DESCRIPTOR,
            api_key="sk-ant-fake",
            cache_dir=tmp_path / "ai_explanations",
            http_client=client,
        )


def test_explain_category_never_writes_a_cache_entry_on_failure(tmp_path: Path) -> None:
    cache_dir = tmp_path / "ai_explanations"
    handler, _ = _counting_handler(lambda: httpx.Response(500))
    client = _client_with_handler(handler)
    with pytest.raises(AnthropicAPIError):
        explain_category(
            _DESCRIPTOR, api_key="sk-ant-fake", cache_dir=cache_dir, http_client=client
        )
    assert not cache_dir.exists() or list(cache_dir.iterdir()) == []


def test_explain_category_sends_model_and_prompt_but_never_the_key_in_the_body(
    tmp_path: Path,
) -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return _messages_response()

    client = _client_with_handler(handler)
    explain_category(
        _DESCRIPTOR,
        api_key="sk-ant-should-be-header-only",
        cache_dir=tmp_path / "ai_explanations",
        http_client=client,
        model=DEFAULT_MODEL,
    )
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert request.headers["x-api-key"] == "sk-ant-should-be-header-only"
    body = request.content.decode("utf-8")
    assert "sk-ant-should-be-header-only" not in body  # key travels only in the header
    assert DEFAULT_MODEL in body
    # Prompt carries only aggregate stats, never a path.
    assert "C:\\" not in body
    assert "/home/" not in body


# --- validate_api_key: cheap validation call ----------------------------------------------------


def test_validate_api_key_returns_true_on_200() -> None:
    handler, calls = _counting_handler(lambda: httpx.Response(200, json={"data": []}))
    client = _client_with_handler(handler)
    assert validate_api_key("sk-ant-good", http_client=client) is True
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_validate_api_key_returns_false_on_auth_failure(status: int) -> None:
    handler, _ = _counting_handler(lambda: httpx.Response(status, json={"error": "unauthorized"}))
    client = _client_with_handler(handler)
    assert validate_api_key("sk-ant-bad", http_client=client) is False


def test_validate_api_key_raises_on_unexpected_status() -> None:
    handler, _ = _counting_handler(lambda: httpx.Response(500))
    client = _client_with_handler(handler)
    with pytest.raises(AnthropicAPIError):
        validate_api_key("sk-ant-fake", http_client=client)


def test_validate_api_key_raises_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    client = _client_with_handler(handler)
    with pytest.raises(AnthropicAPIError):
        validate_api_key("sk-ant-fake", http_client=client)
