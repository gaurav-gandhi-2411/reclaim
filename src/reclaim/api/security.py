from __future__ import annotations

import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

# Header name a legitimate same-origin browser request can only send if it read the token out
# of the page it was served (the `<meta name="reclaim-csrf-token">` tag `index.html` renders) —
# a cross-origin page (classic CSRF) has no way to read that tag, and a `<form>`-based CSRF
# POST can't set a custom header at all, so requiring this header on every mutating request
# blocks both.
CSRF_HEADER_NAME = "x-reclaim-csrf-token"

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def generate_csrf_token() -> str:
    """One per server process (`AppState.csrf_token`, set once in `create_app`) — this is a
    single-user, single-session localhost tool (see `AppState`'s own docstring), so a
    process-lifetime token is the right granularity: no login, no multi-tenant session store to
    key it against."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class LocalOriginPolicy:
    """The one loopback authority (`host:port`, e.g. `127.0.0.1:8420`) this server process is
    actually bound to — computed once in `create_app` from the same `host`/`port` the CLI
    already hard-validated in `cli.py::_loopback_host` before `uvicorn.run` was ever called.

    Every incoming request's `Host` header (and `Origin` header, when present) must name this
    exact authority — anything else is either a DNS-rebinding attempt (a page loaded from a
    hostname that only *resolves* to 127.0.0.1, but whose `Host` header the browser still sends
    as the original hostname) or a stray/misdirected request, and both are refused.
    """

    host: str
    port: int

    @property
    def authority(self) -> str:
        # IPv6 literal authorities are bracketed per RFC 3986 (`[::1]:8420`); IPv4/hostname
        # authorities are not (`127.0.0.1:8420`).
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    def host_header_is_valid(self, host_header: str | None) -> bool:
        return host_header is not None and host_header.lower() == self.authority.lower()

    def origin_header_is_valid(self, origin_header: str | None) -> bool:
        """`None` means the browser sent no `Origin` header at all (common for a plain
        navigation GET, or many non-browser HTTP clients) — treated as "nothing to check", not
        as a failure; the `Host` header check above is mandatory on every request regardless and
        is what actually carries this policy's weight. When an `Origin` header IS present, it
        must resolve to exactly this authority."""
        if origin_header is None:
            return True
        parsed = urlsplit(origin_header)
        return parsed.netloc.lower() == self.authority.lower()


def local_origin_violation(request: Request, policy: LocalOriginPolicy) -> str | None:
    """Returns a human-readable rejection reason, or `None` if the request passes. Pure
    function over `request.headers`/`request.method` — no I/O, trivially unit-testable without
    spinning up a real server."""
    host_header = request.headers.get("host")
    if not policy.host_header_is_valid(host_header):
        return (
            f"Host header {host_header!r} does not match this server's loopback address "
            f"({policy.authority!r}) — refusing a request that isn't from the local dashboard "
            "(possible DNS-rebinding attempt)."
        )

    origin_header = request.headers.get("origin")
    if not policy.origin_header_is_valid(origin_header):
        return (
            f"Origin header {origin_header!r} does not match this server's loopback address "
            f"({policy.authority!r}) — refusing a cross-origin request."
        )

    if request.method in _MUTATING_METHODS:
        token = request.headers.get(CSRF_HEADER_NAME)
        if token is None or not secrets.compare_digest(token, _current_csrf_token(request)):
            return (
                "Missing or invalid CSRF token on a mutating request — every apply/restore/"
                "scan call must carry the per-session token the dashboard reads from its own "
                f"page (header {CSRF_HEADER_NAME!r})."
            )

    return None


def _current_csrf_token(request: Request) -> str:
    token: str = request.app.state.reclaim.csrf_token
    return token
