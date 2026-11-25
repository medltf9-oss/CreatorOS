"""Official social provider integrations.

Structure:

* ``config``      - endpoints, scopes, capability catalogue (data only)
* ``base``        - the adapter contract, value objects, the HTTP transport
* ``errors``      - one error taxonomy for three providers
* ``oauth``       - state and PKCE primitives, redirect and expiry rules
* ``account_discovery`` - how discovered accounts become account rows
* ``capability_rules`` - stored facts to normalized capability states
* ``facebook`` / ``instagram`` / ``tiktok`` - the adapters

Nothing here publishes, uploads, comments or messages. There is no publish endpoint in any
adapter, and a test asserts that.
"""
from __future__ import annotations

from .base import AdapterRegistry, HttpxTransport, SocialAdapter
from .errors import SocialError

__all__ = ["AdapterRegistry", "HttpxTransport", "SocialAdapter", "SocialError"]
