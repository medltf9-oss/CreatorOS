"""URL checks for anything a publisher is about to fetch or send to a provider.

Publishing involves two kinds of URL that the application does not fully control:

* a media URL that Instagram will fetch (`video_url`), which comes from our own delivery provider
  but is still a string until it is checked, and
* an upload URL that TikTok returns in its API response, which is a value from the network.

Both are SSRF inputs. The rule is the same for both: `https` only, a hostname (never an IP literal),
no userinfo, no non-standard port, and a hostname that is either one of the provider's own or one the
user configured. Redirects are never followed, so a public URL cannot bounce the request somewhere
private after the check has passed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Hosts the providers themselves serve. Instagram fetches from whatever we give it; TikTok hands
#: back upload URLs on its own API and CDN hosts.
PROVIDER_HOSTS: tuple[str, ...] = (
    "graph.facebook.com",
    "graph.instagram.com",
    "open.tiktokapis.com",
    "tiktokapis.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "ibytedtos.com",
)

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}


@dataclass(frozen=True, slots=True)
class UrlVerdict:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    for candidate in allowed:
        entry = candidate.strip().lower().lstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith(f".{entry}"):
            return True
    return False


def validate_provider_url(url: str | None, *, extra_hosts: tuple[str, ...] = ()) -> UrlVerdict:
    """Decide whether a URL may be handed to a provider or fetched on its behalf."""
    if not url:
        return UrlVerdict(False, "no url")
    candidate = str(url).strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return UrlVerdict(False, "the url cannot be parsed")

    if parts.scheme.lower() != "https":
        return UrlVerdict(False, "only https urls are allowed")
    if parts.username or parts.password:
        return UrlVerdict(False, "urls may not carry credentials of their own")
    if parts.port not in (None, 443):
        return UrlVerdict(False, "only the default https port is allowed")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return UrlVerdict(False, "the url has no host")
    if host in _BLOCKED_HOSTNAMES:
        return UrlVerdict(False, "localhost is never reachable by a provider")
    if _is_ip_literal(host):
        return UrlVerdict(False, "ip literals are not allowed")
    if _is_private(host):
        return UrlVerdict(False, "private addresses are not allowed")

    allowed = tuple(PROVIDER_HOSTS) + tuple(extra_hosts)
    if not _host_allowed(host, allowed):
        return UrlVerdict(False, f"{host} is not an allowed host")

    return UrlVerdict(True)


def host_of(url: str | None) -> str | None:
    """The hostname of a URL, for allowlist comparisons and for redacted logging."""
    if not url:
        return None
    try:
        return (urlsplit(str(url)).hostname or "").lower() or None
    except ValueError:
        return None
