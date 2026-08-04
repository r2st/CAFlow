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

from starlette.exceptions import HTTPException
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
        # Resolved here, in the outermost layer, rather than where it is first
        # needed. A context variable set by an inner middleware is not visible
        # to this one — ``call_next`` runs the rest of the stack in its own
        # task — so an actor established further down would reach neither the
        # access line below nor a log written before that layer runs. Setting
        # it here means every line of the request carries it, and it no longer
        # depends on rate limiting being switched on.
        actor_token = actor_var.set(_token_subject(request) or "")
        request.state.request_id = request_id
        started = time.perf_counter()

        # The context is reset only once the access line has been written.
        # Resetting straight after ``call_next`` would strip the id from the
        # one log line that names the request, leaving the caller holding an
        # X-Request-ID that appears nowhere in the log.
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Handled here rather than left to Starlette's
                # ServerErrorMiddleware, which sits outside every application
                # middleware: a response produced there would carry no request
                # id, no CORS headers and no security headers. The registered
                # ``Exception`` handler stays in place as a backstop for
                # anything raised outside this middleware.
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
                    # Set by whichever layer resolved the caller; "-" until one
                    # does, which is the honest answer for an anonymous call.
                    "actor": actor_var.get() or "-",
                },
            )
            return response
        finally:
            request_id_var.reset(token)
            actor_var.reset(actor_token)

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
    """Cap the request body, by the declared length *and* by what arrives.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so the rejection happens
    before Starlette begins buffering the body.

    The declared ``Content-Length`` is the cheap half: it refuses an oversized
    upload before a byte of it is read. It is also the half a caller can simply
    omit. ``Transfer-Encoding: chunked`` carries no length, and nothing then
    stood between the socket and Starlette's multipart parser — which has no
    size limit of its own on a *file* part, spools it to a temporary file past
    one megabyte, and hands the route an ``UploadFile`` the route then reads
    whole into memory. The per-upload check in :mod:`app.services.storage` runs
    after all of that, so the 413 it produces is honest about the file and
    useless about the cost: thirty megabytes, or thirty gigabytes, are already
    on disk and in RAM by the time it is raised.

    ``POST /portal/documents`` is the sharp end of that. It is reachable by
    anyone holding a client's magic link — a party outside the firm entirely —
    and one chunked request with no Content-Length is enough to fill the
    storage volume's temporary space.

    So the body is counted as it is read. ``receive`` is wrapped, the running
    total is checked against the same limit, and the read *raises* as soon as
    it is passed — which is what stops the parser mid-part rather than merely
    reporting on it once it has finished. Nothing further is pulled off the
    socket, nothing more is spooled, and the temporary file the parser had open
    is closed as the exception unwinds.

    Raised as an ``HTTPException`` rather than an exception of our own for one
    specific reason: FastAPI wraps body parsing in a bare ``except Exception``
    that turns anything it catches into "There was an error parsing the body"
    — a 400 that says the caller sent something malformed, which is exactly the
    wrong thing to tell someone whose upload was too big. ``HTTPException`` is
    the one type it re-raises, so this reaches the registered handler and comes
    out as the same 413 envelope the declared-length path produces.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    @property
    def _detail(self) -> str:
        return f"Request body exceeds the {self.max_bytes / (1024 * 1024):.0f} MB limit"

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
                from app.core.errors import error_response

                response = error_response(
                    status_code=413, detail=self._detail, request=Request(scope)
                )
                await response(scope, receive, send)
                return
            break

        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    logger.warning(
                        "Undeclared body passed the %s-byte limit on %s %s",
                        self.max_bytes,
                        scope.get("method"),
                        scope.get("path"),
                        extra={"path": scope.get("path"), "received_bytes": received},
                    )
                    raise HTTPException(status_code=413, detail=self._detail)
            return message

        await self.app(scope, counted_receive, send)


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

        # Already resolved by RequestContextMiddleware, which runs outside this
        # one — decoding the token a second time would buy nothing.
        subject = actor_var.get()
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


# A real chain is a handful of hops. Anything longer is a caller padding the
# header, and walking all of it is parse work done at their request.
MAX_FORWARDED_ENTRIES = 20


def _parse_address(candidate: str) -> str | None:
    """``candidate`` as an IP address, or None if it is not one.

    A proxy sends an address; anything else is either a misconfigured hop or a
    caller writing junk, and neither should key a counter or reach a log line.
    """
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _forwarded_client(request: Request) -> str | None:
    """The originating address from the proxy headers, if there is a real one.

    Walked right to left, skipping hops that are themselves trusted proxies,
    and stopping at the first address that is not one.

    The left-most entry is the conventional answer, and it is also the one a
    caller can write. Every proxy in this stack *appends* rather than replaces
    — nginx forwards ``$proxy_add_x_forwarded_for`` — so a header that arrives
    with a forged prefix keeps that prefix all the way here. Reading from the
    right instead means the first untrusted entry is the address the last
    trusted hop actually accepted a connection from, which is the one part of
    the chain nobody upstream could invent; anything a caller made up sits
    harmlessly to the left of it.

    That is what stops Caddy's ``header_up X-Forwarded-For {remote_host}``
    from being the single line the rate limiter's honesty rests on. Overwriting
    at the edge is still right, and still tested — but losing it now costs a
    tidy log line rather than the limits themselves.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
    if not entries:
        candidate = request.headers.get("x-real-ip", "").strip()
        return _parse_address(candidate) if candidate else None

    walked: list[str] = []
    for entry in reversed(entries[-MAX_FORWARDED_ENTRIES:]):
        address = _parse_address(entry)
        if address is None:
            # A hop that is not an address means the chain cannot be read past
            # this point. Fall back to the peer rather than guess at which
            # side of the junk the client was on.
            return None
        if not _is_trusted_proxy(address):
            return address
        walked.append(address)

    # Every hop was a trusted proxy, so the caller is inside the network too
    # and the left-most entry is where it started.
    return walked[-1]


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
