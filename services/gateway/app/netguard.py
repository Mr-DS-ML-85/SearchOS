"""Outbound URL validation — SSRF guard shared by the fetcher, crawler and renderer.

The gateway can be pointed at any URL by an authenticated caller (``/v1/extract``),
by an admin seeding a crawl, or by a link discovered on a crawled page. Without
this module all of those are unauthenticated-in-practice proxies into the
container network: ``169.254.169.254`` for cloud metadata, ``postgres:5432``,
``redis:6379``, ``renderer:8010``, or any RFC1918 host.

Checks applied, in order:
1. scheme must be http/https
2. host must resolve, and every resolved address must be publicly routable
   (unless the host is on an explicit allowlist)
3. re-checked on every redirect hop, since ``follow_redirects`` can be bypassed
   by an attacker-controlled 302 to an internal address
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import Settings, get_settings, is_blocked_ip

ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedURLError(ValueError):
    """Raised when a URL is refused by the egress policy."""


class DNSResolutionError(BlockedURLError):
    """The hostname could not be resolved.

    Deliberately a distinct type: a typo or a dead DNS record is not a security
    decision, and reporting it as "refused by egress policy" points whoever is
    debugging it at the SSRF guard instead of at DNS.
    """


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int


def _default_ports(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _is_allowlisted(host: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return False
    host = host.lower()
    for entry in allowlist:
        entry = entry.lower()
        if host == entry or host.endswith(f".{entry}"):
            return True
    return False


def resolve_addresses(host: str) -> list[str]:
    """All addresses ``host`` resolves to.

    Raises ``DNSResolutionError`` when the name does not exist, so callers can
    distinguish "unreachable name" from "refused destination".
    """
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as exc:
        raise DNSResolutionError(f"cannot resolve host: {host}") from exc


def validate_url(
    url: str,
    *,
    settings: Settings | None = None,
    check_dns: bool = True,
) -> ValidatedURL:
    """Validate ``url`` against the egress policy, or raise ``BlockedURLError``."""
    cfg = settings or get_settings()

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"scheme not allowed: {scheme or '(none)'!r}")

    try:
        host = parts.hostname
    except ValueError as exc:  # malformed IPv6 literal, bad port, ...
        raise BlockedURLError(f"malformed URL: {exc}") from exc
    if not host:
        raise BlockedURLError("URL has no host")

    try:
        port = parts.port or _default_ports(scheme)
    except ValueError as exc:
        raise BlockedURLError(f"invalid port in URL: {exc}") from exc

    if _is_allowlisted(host, cfg.fetch_allow_private_networks):
        return ValidatedURL(url=url, scheme=scheme, host=host, port=port)

    if not cfg.fetch_block_private_networks:
        return ValidatedURL(url=url, scheme=scheme, host=host, port=port)

    if check_dns:
        for addr in resolve_addresses(host):
            if is_blocked_ip(addr):
                raise BlockedURLError(
                    f"destination resolves to a non-public address "
                    f"({host} -> {addr}); add it to FETCH_ALLOW_PRIVATE_NETWORKS to permit"
                )
    else:
        # No DNS check requested: still refuse a literal private address.
        if is_blocked_ip(host):
            raise BlockedURLError(f"destination is a non-public address ({host})")

    return ValidatedURL(url=url, scheme=scheme, host=host, port=port)
