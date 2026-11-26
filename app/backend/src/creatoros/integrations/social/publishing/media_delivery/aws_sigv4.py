"""AWS Signature Version 4, implemented from the specification with no new dependency.

Written by hand because the alternative was a dependency that drags in a second HTTP stack, and
because a signing bug should be visible in this repository rather than inferred from a library's
behaviour. The correctness anchor is the AWS test suite vector in `test_publishing_delivery.py`
(`get-vanilla`), which pins the exact signature the specification says must come out.

Only two things are needed here: signing a request with headers (uploads, HEAD, DELETE) and
presigning a URL (the short-lived GET that Instagram fetches). Both build the same canonical
request; presigning moves the authentication into the query string and uses `UNSIGNED-PAYLOAD`.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def canonical_uri(path: str) -> str:
    """Encode each segment, leaving the separators alone (S3 is strict about this)."""
    if not path:
        return "/"
    return quote(path if path.startswith("/") else f"/{path}", safe="/-_.~")


def canonical_query(params: Mapping[str, str] | None = None) -> str:
    if not params:
        return ""
    pairs = sorted((quote(str(key), safe="-_.~"), quote(str(value), safe="-_.~")) for key, value in params.items())
    return "&".join(f"{key}={value}" for key, value in pairs)


def canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """Lower-case, trim, sort and join; returns the block and the signed-header list."""
    normalized = {str(key).strip().lower(): " ".join(str(value).split()) for key, value in headers.items()}
    names = sorted(normalized)
    block = "".join(f"{name}:{normalized[name]}\n" for name in names)
    return block, ";".join(names)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    key = _hmac(f"AWS4{secret_key}".encode(), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def credential_scope(datestamp: str, region: str, service: str) -> str:
    return f"{datestamp}/{region}/{service}/aws4_request"


def amz_date(now: datetime | None = None) -> tuple[str, str]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")


def string_to_sign(
    *, amz_timestamp: str, scope: str, canonical_request: str
) -> str:
    digest = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    return f"{ALGORITHM}\n{amz_timestamp}\n{scope}\n{digest}"


def canonical_request(
    *,
    method: str,
    path: str,
    query: Mapping[str, str] | None,
    headers: Mapping[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    block, signed = canonical_headers(headers)
    request = "\n".join(
        [method.upper(), canonical_uri(path), canonical_query(query), block, signed, payload_hash]
    )
    return request, signed


def sign_headers(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime | None = None,
    include_content_sha256: bool = True,
) -> dict[str, str]:
    """Return the headers a signed request needs (everything given, plus date and authorization).

    ``include_content_sha256`` is on for S3, which mandates the header, and off for the generic AWS
    test vectors, which do not send it. Turning it off is what lets the `get-vanilla` vector below
    reproduce the exact signature the specification publishes.
    """
    stamp, datestamp = amz_date(now)
    merged = {str(key): str(value) for key, value in (headers or {}).items()}
    merged["Host"] = host
    merged["x-amz-date"] = stamp
    if include_content_sha256:
        merged["x-amz-content-sha256"] = payload_hash
    request, signed = canonical_request(
        method=method, path=path, query=query, headers=merged, payload_hash=payload_hash
    )
    scope = credential_scope(datestamp, region, service)
    signature = hmac.new(
        signing_key(secret_key, datestamp, region, service),
        string_to_sign(amz_timestamp=stamp, scope=scope, canonical_request=request).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    merged["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return merged


def presign_url(
    *,
    scheme: str,
    host: str,
    path: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    expires_s: int = 900,
    now: datetime | None = None,
) -> str:
    """A query-string-signed GET URL. The URL itself is a credential: never log it."""
    stamp, datestamp = amz_date(now)
    scope = credential_scope(datestamp, region, service)
    params = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(max(1, min(int(expires_s), 604_800))),
        "X-Amz-SignedHeaders": "host",
    }
    request, _signed = canonical_request(
        method="GET", path=path, query=params, headers={"host": host}, payload_hash=UNSIGNED_PAYLOAD
    )
    signature = hmac.new(
        signing_key(secret_key, datestamp, region, service),
        string_to_sign(amz_timestamp=stamp, scope=scope, canonical_request=request).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query = canonical_query({**params, "X-Amz-Signature": signature})
    return f"{scheme}://{host}{canonical_uri(path)}?{query}"


def payload_sha256(body: bytes | None = None) -> str:
    return hashlib.sha256(body if body is not None else b"").hexdigest()
