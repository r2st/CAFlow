"""The deployment files, checked against what the app actually assumes.

Nothing here starts a container. These are the handful of lines in
``docker-compose.yml``, the production overlay, the Caddy site and
``nginx.conf`` that the running app silently depends on — where an edit that
looks harmless takes a protection away without anything failing.

The body limits are the clearest case. Caddy, nginx and the app each cap a
request body, and the app's cap is the only one that produces the app's own
message. Let either proxy drop below it and callers meet a bare 413 from
something that cannot explain itself — while every test still passes, because
nothing in the suite has an opinion about a number in a Caddyfile. So they are
pinned against ``settings`` here rather than against a constant.

Same for the published ports, the secrets the overlay refuses to default, and
Caddy's ``header_up X-Forwarded-For``. That last one is no longer load-bearing
on its own — ``middleware._forwarded_client`` reads the chain from the right
and skips trusted hops, so a forged prefix is ignored regardless — but it is
still what keeps the chain to a single honest entry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
PROD_OVERLAY = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
CADDYFILE = REPO_ROOT / "deploy" / "caflow.aiknol.com.caddy"
NGINX_CONF = REPO_ROOT / "frontend" / "nginx.conf"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return _load(COMPOSE)


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _load(PROD_OVERLAY)


@pytest.fixture(scope="module")
def caddyfile() -> str:
    return CADDYFILE.read_text()


@pytest.fixture(scope="module")
def nginx_conf() -> str:
    return NGINX_CONF.read_text()


class TestPublishedPorts:
    """Everything the stack publishes is reachable from this box and nowhere else.

    Caddy is the only listener that belongs on a public interface. A bare
    ``"5434:5432"`` binds every interface instead, which on a laptop puts the
    database on the coffee-shop wifi and on the VPS puts it on the internet —
    in both cases behind a password that ships in the file.
    """

    def test_every_published_port_is_bound_to_loopback(self, compose):
        # A list, not a dict keyed by service: a service publishing two ports
        # would otherwise lose one of them and go unchecked.
        published = [
            (name, mapping)
            for name, service in compose["services"].items()
            for mapping in service.get("ports", ())
        ]
        assert published, "no published ports found — has the file moved?"
        for name, mapping in published:
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{name} publishes {mapping!r} on every interface; "
                "bind it to 127.0.0.1 and let Caddy be the front door"
            )

    def test_the_api_port_is_not_published_at_all(self, compose):
        """nginx already proxies it. Publishing 8000 only adds a way around nginx.

        Reaching the API directly means reaching it with an ``X-Forwarded-For``
        of one's choosing, from a peer inside the compose network that the app
        trusts by default.
        """
        api = compose["services"]["api"]

        assert "ports" not in api
        assert "8000" in [str(port) for port in api.get("expose", ())]

    def test_the_overlay_does_not_publish_anything_further(self, overlay):
        """The production overlay narrows the base file; it must not widen it."""
        for name, service in overlay.get("services", {}).items():
            assert "ports" not in service, f"{name} publishes a port in the overlay"


class TestProductionSecrets:
    """Every secret that has a usable default in development loses it in production.

    ``${VAR:?message}`` fails the ``up`` by name. A deploy that stops is
    recoverable; a deploy running on ``dev-secret-change-me`` is a signing key
    anyone can reproduce from the repository.
    """

    @pytest.mark.parametrize("variable", ["SECRET_KEY", "POSTGRES_PASSWORD"])
    def test_the_overlay_refuses_to_start_without_it(self, variable):
        raw = PROD_OVERLAY.read_text()

        assert re.search(rf"\$\{{{variable}:\?", raw), (
            f"{variable} must be ${{{variable}:?...}} in the overlay, so a missing "
            "value stops the deploy instead of starting it on a default"
        )
        assert f"${{{variable}:-" not in raw, f"{variable} still has a fallback default"

    def test_production_runs_with_debug_off(self, overlay):
        """DEBUG on would put tracebacks in HTTP responses."""
        env = overlay["services"]["api"]["environment"]

        assert env["ENVIRONMENT"] == "production"
        assert str(env["DEBUG"]).lower() == "false"

    @pytest.mark.parametrize("variable", ["CORS_ORIGINS", "PORTAL_BASE_URL"])
    def test_the_public_urls_are_https(self, overlay, variable):
        """Magic links carry a session token, so the scheme is not cosmetic."""
        value = str(overlay["services"]["api"]["environment"][variable])

        assert "http://" not in value, f"{variable} names a plaintext URL: {value}"
        assert "https://" in value


class TestDatabaseCredentialsMoveTogether:
    """The password Postgres is initialised with, and the one the app connects with.

    They are two separate lines in the same file. Change one and the stack
    comes up with an API that cannot reach its database — at which point the
    failure is a connection error several services deep, not an obvious typo.
    """

    def test_both_read_the_same_variable(self, compose):
        url = compose["services"]["api"]["environment"]["DATABASE_URL"]
        postgres_password = compose["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]

        assert "${POSTGRES_PASSWORD:-" in url, "the connection URL hardcodes a password"
        assert "${POSTGRES_PASSWORD:-" in postgres_password

        # ...and to the same default, or an unset variable splits them.
        default = re.search(r"\$\{POSTGRES_PASSWORD:-([^}]*)\}", url).group(1)
        assert postgres_password == f"${{POSTGRES_PASSWORD:-{default}}}"


class TestCaddyIsTheFrontDoor:
    """The reverse proxy the whole forwarded-header story rests on."""

    def test_it_overwrites_the_forwarded_for_header(self, caddyfile):
        """Overwrite, not append.

        Caddy's default is to append the peer to whatever the caller sent,
        carrying a forged prefix through to the app. The app defends itself
        against that by reading the chain from the right, so this is defence in
        depth rather than the only line — but it is what keeps the chain to one
        entry that nobody upstream invented.
        """
        assert re.search(r"header_up\s+X-Forwarded-For\s+\{remote_host\}", caddyfile), (
            "Caddy must set X-Forwarded-For to {remote_host}, not append to it"
        )
        # A leading "+" is Caddy's syntax for append. It must not appear here.
        assert not re.search(r"header_up\s+\+X-Forwarded-For", caddyfile)

    def test_it_forwards_the_scheme_and_the_real_ip(self, caddyfile):
        assert re.search(r"header_up\s+X-Real-IP\s+\{remote_host\}", caddyfile)
        assert re.search(r"header_up\s+X-Forwarded-Proto\s+\{scheme\}", caddyfile)

    def test_it_proxies_to_the_loopback_port_the_web_container_publishes(
        self, caddyfile, compose
    ):
        published = compose["services"]["web"]["ports"][0]
        host_port = str(published).split(":")[1]

        assert f"reverse_proxy 127.0.0.1:{host_port}" in caddyfile, (
            f"Caddy proxies somewhere other than the {host_port} the web container publishes"
        )

    def test_it_sets_hsts(self, caddyfile):
        """TLS terminates here, so this is the only place that can honestly say so."""
        assert re.search(r"Strict-Transport-Security\s+\"max-age=(\d+)", caddyfile)
        max_age = int(re.search(r"max-age=(\d+)", caddyfile).group(1))
        assert max_age >= 31536000


class TestBodyLimitsAgree:
    """Three limits in a row, and the app's must be the one a caller meets.

    Caddy and nginx exist to turn an oversized body away before it reaches a
    Python worker. If either sits *below* the app's limit, the app's own check
    — and the message it gives — is never reached, and a caller gets a bare
    413 from a proxy instead.
    """

    def test_caddy_allows_at_least_what_the_app_does(self, caddyfile):
        max_size = re.search(r"max_size\s+(\d+)MB", caddyfile)
        assert max_size, "Caddy's request_body max_size is missing"

        allowed = int(max_size.group(1)) * 1024 * 1024
        assert allowed >= settings.max_request_body_bytes

    def test_nginx_allows_at_least_what_the_app_does(self, nginx_conf):
        client_max = re.search(r"client_max_body_size\s+(\d+)m", nginx_conf)
        assert client_max, "nginx's client_max_body_size is missing"

        allowed = int(client_max.group(1)) * 1024 * 1024
        assert allowed >= settings.max_request_body_bytes

    def test_the_proxies_outlast_the_slowest_thing_the_app_does(self, caddyfile, nginx_conf):
        """Upload plus AI categorisation runs long; a proxy timing out first is a 504."""
        caddy_read = int(re.search(r"read_timeout\s+(\d+)s", caddyfile).group(1))
        nginx_read = int(re.search(r"proxy_read_timeout\s+(\d+)s", nginx_conf).group(1))

        assert caddy_read >= nginx_read, (
            "Caddy must not give up before the nginx behind it does, or the app's "
            "own response never makes it back"
        )
