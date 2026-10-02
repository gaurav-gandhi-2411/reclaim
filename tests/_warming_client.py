from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


class WarmingTestClient(TestClient):
    """`TestClient` that behaves like the dashboard: a GET answered with the typed 409
    `candidates_not_warm` (every candidate-cache reader -- summary, treemap, candidates,
    one-click summary -- refuses a cold/stale cache instead of computing it in the request
    thread) is retried ONCE after the courtesy warm-up the 409 started. Starlette's TestClient
    runs a response's background task before returning, so the retry sees a warm cache.

    Set `retry_not_warm = False` for a test that asserts the 409 itself."""

    retry_not_warm: bool = True

    def get(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        response = super().get(*args, **kwargs)
        if (
            self.retry_not_warm
            and response.status_code == 409
            and response.headers.get("content-type", "").startswith("application/json")
            and response.json().get("code") == "candidates_not_warm"
        ):
            return super().get(*args, **kwargs)
        return response
