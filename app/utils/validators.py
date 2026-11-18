"""URL validation and SSRF protection.

Every URL we fetch (the user's URL, every redirect hop and every internal
sub-page) goes through `validate_public_url`, which rejects:

* non-http(s) schemes, embedded credentials and non-standard ports
* hostnames like ``localhost`` / ``*.internal`` / ``*.local``
* any hostname that resolves to a private, loopback, link-local, multicast,
  reserved or otherwise non-globally-routable address (IPv4 and IPv6),
  including cloud metadata endpoints such as 169.254.169.254
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


class InvalidURLError(ValueError):
    """The URL is malformed or uses an unsupported scheme/port."""


class BlockedURLError(ValueError):
    """The URL points to a private/internal network address (SSRF guard)."""


def normalize_url(raw: str) -> str:
    """Trim, add a scheme when missing, lower-case the host and drop fragments."""
    raw = (raw or "").strip()
    if not raw:
        raise InvalidURLError("URL must not be empty.")
    if len(raw) > 2048:
        raise InvalidURLError("URL is too long (max 2048 characters).")
    if "://" not in raw:
        raw = "https://" + raw

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError(f"Unsupported URL scheme '{parts.scheme}'. Use http or https.")
    if parts.username or parts.password:
        raise InvalidURLError("URLs with embedded credentials are not allowed.")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise InvalidURLError("URL has no hostname.")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("URL has an invalid port.") from exc
    if port not in ALLOWED_PORTS:
        raise InvalidURLError(f"Port {port} is not allowed.")

    # Plain hostnames (no dot) are almost always internal names, unless it's an IP literal.
    if "." not in host and not _is_ip_literal(host):
        raise InvalidURLError("URL must contain a fully qualified domain name.")

    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    # Unwrap IPv4-mapped / 6to4 style addresses so ::ffff:127.0.0.1 can't sneak through.
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif addr.sixtofour:
            addr = addr.sixtofour
    return addr.is_global and not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def resolve_host(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InvalidURLError(f"Could not resolve hostname '{host}'.") from exc
    return sorted({info[4][0] for info in infos})


async def validate_public_url(raw: str) -> str:
    """Normalize `raw` and make sure it only resolves to public IP addresses.

    Returns the normalized URL. Raises InvalidURLError / BlockedURLError.
    """
    url = normalize_url(raw)
    host = urlsplit(url).hostname or ""

    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedURLError("Access to internal hostnames is not allowed.")

    ips = [host] if _is_ip_literal(host) else await resolve_host(host)
    if not ips:
        raise InvalidURLError(f"Could not resolve hostname '{host}'.")
    for ip in ips:
        if not is_public_ip(ip):
            raise BlockedURLError("Access to private or internal network addresses is not allowed.")
    return url
