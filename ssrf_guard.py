"""
SSRF guard for all outbound fetches of attacker-influenced URLs.

This crawler issues server-side requests to URLs that can be controlled by an
audited (and possibly hostile) website — robots.txt, declared sitemaps, sitemap
"orphan" fill, external-link validation, schema fetches. A public SEO crawler
should NEVER legitimately reach an internal/private/loopback/link-local address,
so the policy here is deny-by-default for non-public targets.

Two things this does that a naive `ipaddress.ip_address(host)` check does not:

  1. Resolves hostnames via DNS and validates EVERY resolved A/AAAA record, so a
     hostname whose record points at 169.254.169.254 / 127.0.0.1 / 10.x is caught.
  2. Re-validates on every redirect hop (via `safe_get`), so a clean public host
     cannot 30x-redirect the request onto an internal address.

Residual risk: a tiny TOCTOU window exists between DNS validation and the actual
TCP connect (classic DNS rebinding). Closing it fully requires pinning the
connection to the validated IP; for defence-in-depth we recommend network-level
egress filtering of the deployment (see README hardening note), which also covers
the upstream LibreCrawl crawler that this module cannot reach.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Schemes a web crawler is ever allowed to fetch. Blocks file://, gopher://, etc.
ALLOWED_SCHEMES = ("http", "https")


class BlockedURLError(ValueError):
    """Raised when a URL is disallowed (bad scheme, no host, or non-public IP)."""


def _ip_is_blocked(ip_str: str) -> bool:
    """True if ip_str is any non-public address we refuse to connect to."""
    addr = ipaddress.ip_address(ip_str)
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) before classifying.
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local      # includes 169.254.0.0/16 (cloud metadata)
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_ips(host: str) -> set[str]:
    """Resolve host to the set of all its IPs. Raises BlockedURLError on failure."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise BlockedURLError(f"DNS resolution failed for {host!r}: {e}") from e
    ips = {info[4][0] for info in infos}
    if not ips:
        raise BlockedURLError(f"no DNS records for {host!r}")
    return ips


def validate_url(url: str) -> None:
    """
    Validate that `url` is safe to fetch. Raises BlockedURLError if not.

    A URL is safe only when its scheme is http(s), it has a hostname, and every
    IP the hostname resolves to is publicly routable.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BlockedURLError(f"URL has no host: {url!r}")

    # If the host is already a literal IP, classify it directly; otherwise resolve.
    try:
        candidate_ips = {str(ipaddress.ip_address(host))}
    except ValueError:
        candidate_ips = _resolve_ips(host)

    blocked = sorted(ip for ip in candidate_ips if _ip_is_blocked(ip))
    if blocked:
        raise BlockedURLError(
            f"refusing non-public address(es) {blocked} for host {host!r}"
        )


def safe_get(client, url: str, *, max_redirects: int = 5, **kwargs):
    """
    httpx GET with SSRF validation before the request AND on every redirect hop.

    `client` is an httpx.Client (or compatible). `follow_redirects` is forced off
    so we can validate each Location ourselves. Returns the final httpx.Response.
    Raises BlockedURLError if any hop targets a non-public address.
    """
    kwargs["follow_redirects"] = False
    current = url
    for _ in range(max_redirects + 1):
        validate_url(current)                       # validate BEFORE each request
        resp = client.get(current, **kwargs)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return resp
            # Resolve relative redirects against the current URL.
            current = str(resp.url.join(location))
            continue
        return resp
    raise BlockedURLError(f"too many redirects (>{max_redirects}) starting at {url!r}")
