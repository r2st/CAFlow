"""HTTP middleware: request context, access logs, body limits, rate limiting,
security headers.

Order matters. ``add_middleware`` builds the stack inside-out, so the module
registers them in reverse: the request-context middleware is added last and
therefore runs first, which is what gives every other layer — including the
exception handlers — a request id to log against.
"""

from __future__ import annotations

import ipaddress
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import settings
from app.core import ratelimit
from app.core.logging import actor_var, request_id_var
from app.core.security import TokenError, decode_token

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"

# Probes and docs are exempt from rate limiting: a load balancer polling
# /health every second must never be throttled, and throttling /docs helps
# nobody.
RATE_LIMIT_EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json")

SECURITY_HEADERS = {
    # The API returns JSON and file downloads; both are served with an
    # explicit Content-Type and must not be re-sniffed.
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The API itself has no browsing context to speak of; a restrictive
    # policy costs nothing and stops a reflected payload from executing.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}

# Swagger UI needs its own CDN assets and inline styles, so the API-wide
# policy above would blank the page.
DOCS_CSP = (
    "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' "
    "https://cdn.jsdelivr.net; connect-src 'self'; frame-ancestors 'none'"
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log the request, and time it.

    An inbound ``X-Request-ID`` is honoured so a trace started at the edge
    carries through, but only if it looks like an id — an unbounded header
    would otherwise end up in every log line.
    """

    MAX_INBOUND_ID_LENGTH = 64

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = self._inbound_id(request) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        actor_token = actor_var.set("")
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception as exc:
            # Handled here rather than left to Starlette's ServerErrorMiddleware,
            # which sits outside every application middleware: a response
            # produced there would carry no request id, no CORS headers and no
            # security headers. The registered ``Exception`` handler stays in
            # place as a backstop for anything raised outside this middleware.
            from app.core.errors import GENERIC_SERVER_ERROR, error_response

            logger.exception(
                "Unhandled %s on %s %s",
                type(exc).__name__,
                request.method,
                request.url.path,
                extra={"method": request.method, "path": request.url.path},
            )
            response = error_response(
                status_code=500, detail=GENERIC_SERVER_ERROR, request=request
            )
        finally:
            request_id_var.reset(token)
            actor_var.reset(actor_token)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"

        if request.url.path.startswith("/health"):
            level = logging.DEBUG  # probes would otherwise dominate the log
        elif response.status_code >= 500 or elapsed_ms >= settings.slow_request_ms:
            level = logging.WARNING
        else:
            level = logging.INFO
        logger.log(
            level,
            "%s %s -> %d in %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
                "client_ip": client_ip(request),
            },
        )
        return response

    def _inbound_id(self, request: Request) -> str | None:
        candidate = request.headers.get(REQUEST_ID_HEADER, "").strip()
        if not candidate or len(candidate) > self.MAX_INBOUND_ID_LENGTH:
            return None
        # Keep it printable and log-injection free.
        if not all(char.isalnum() or char in "-_" for char in candidate):
            return None
        return candidate


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardening headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.path in ("/docs", "/redoc"):
            response.headers["Content-Security-Policy"] = DOCS_CSP
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class BodySizeLimitMiddleware:
    """Reject oversized bodies from the declared Content-Length.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so the rejection happens
    before Starlette begins buffering the body. A chunked request without a
    Content-Length still reaches the route, where the per-upload limit in
    ``services.storage`` applies.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", ()):
            if name != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                break
            if declared > self.max_bytes:
                limit_mb = self.max_bytes / (1024 * 1024)
                from app.core.errors import error_response

                response = error_response(
                    status_code=413,
                    detail=f"Request body exceeds the {limit_mb:.0f} MB limit",
                    request=Request(scope),
                )
                await response(scope, receive, send)
                return
            break

        await self.app(scope, receive, send)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting, keyed by practitioner, client or IP.

    The key is derived from the bearer token's subject when one is present, so
    a firm behind a single NAT address is not limited as one caller. The token
    is only decoded — never trusted for authorisation — and an unverifiable
    token falls back to the IP, which is the conservative direction.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.rate_limit_enabled or request.url.path.startswith(
            RATE_LIMIT_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        subject = _token_subject(request)
        if subject:
            actor_var.set(subject)
        key_source = subject or f"ip:{client_ip(request)}"
        bucket = ratelimit.bucket_for(
            request.url.path, request.method, authenticated=bool(subject)
        )
        decision = await ratelimit.get_counter().hit(
            f"{bucket.name}:{key_source}", bucket.limit, ratelimit.WINDOW_SECONDS
        )

        if not decision.allowed:
            logger.warning(
                "Rate limit hit on %s %s (bucket=%s)",
                request.method,
                request.url.path,
                bucket.name,
                extra={
                    "path": request.url.path,
                    "bucket": bucket.name,
                    "limit": decision.limit,
                    "client_ip": client_ip(request),
                },
            )
            from app.core.errors import error_response

            return error_response(
                status_code=429,
                detail=(
                    f"Too many requests. Try again in {decision.reset_after} second"
                    f"{'' if decision.reset_after == 1 else 's'}."
                ),
                headers=_limit_headers(decision) | {"Retry-After": str(decision.reset_after)},
                request=request,
            )

        response = await call_next(request)
        for header, value in _limit_headers(decision).items():
            response.headers.setdefault(header, value)
        return response


def _limit_headers(decision: ratelimit.Decision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after),
    }


def _token_subject(request: Request) -> str | None:
    """``<type>:<sub>`` from the bearer token, or None if there isn't a valid one."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    for token_type in ("access", "magic_link"):
        try:
            payload = decode_token(token.strip(), expected_type=token_type)
        except TokenError:
            continue
        subject = payload.get("sub")
        if subject:
            return f"{token_type}:{subject}"
    return None


def _is_trusted_proxy(peer: str) -> bool:
    """Is the machine that opened this connection a proxy we believe?

    ``TRUST_PROXY_HEADERS`` alone is not enough. The stack publishes the API
    port alongside nginx, so a caller reaching it directly could send whatever
    ``X-Forwarded-For`` it liked and mint a fresh rate-limit bucket per request
    — which is to say, no rate limit at all. The header is only believed when
    the connection came from a listed address.
    """
    if settings.trusts_every_proxy:
        return True
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in settings.trusted_proxy_networks)


def _forwarded_client(request: Request) -> str | None:
    """The originating address from the proxy headers, if there is a real one.

    The value has to parse as an IP address. A proxy sends one; anything else
    is either a misconfigured hop or a caller trying to write its own bucket
    key, and neither should end up keying a counter or landing in a log line.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    # Left-most entry is the original client.
    candidate = forwarded.split(",")[0].strip() or request.headers.get("x-real-ip", "").strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """The caller's address, honouring proxy headers only when configured.

    ``X-Forwarded-For`` is caller-supplied unless a proxy overwrites it, so it
    is believed only when the switch is on *and* the connection came from a
    trusted proxy. Falling back to the peer address is the conservative
    direction: a caller gets its own real address rather than one it chose.
    """
    peer = request.client.host if request.client else "unknown"
    if settings.trust_proxy_headers and _is_trusted_proxy(peer):
        forwarded = _forwarded_client(request)
        if forwarded:
            return forwarded[:64]
    return peer
