"""S3-compatible media delivery, signed locally with SigV4.

Works with AWS S3 and with any provider that speaks the same API (Cloudflare R2, Backblaze B2,
Wasabi, MinIO). Nothing here is provider-specific: the endpoint, region, path-style flag and
bucket come from configuration.

Uploads are resumable in the S3 sense (multipart), verifiable (the object is HEADed afterwards and
its size and stored sha256 metadata are compared with what was measured locally), and short-lived
(the URL that Instagram fetches is presigned and expires). A failed multipart upload is aborted, so
an interrupted 2 GB upload does not keep paying for storage forever.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from .aws_sigv4 import EMPTY_SHA256, presign_url, sign_headers
from .base import (
    DeliveryConfig,
    DeliveryError,
    DeliveryHealth,
    DeliveryReceipt,
    MediaDeliveryProvider,
    ObjectInfo,
    S3Credentials,
    SignedUrl,
)

_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
DEFAULT_TIMEOUT_S = 60.0


class S3DeliveryProvider(MediaDeliveryProvider):
    name = "s3"
    configured = True

    def __init__(
        self,
        config: DeliveryConfig,
        credentials: S3Credentials,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.config = config
        self._credentials = credentials
        self._client = client
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout = timeout

    # ------------------------------------------------------------------ plumbing
    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _signed(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        payload_hash: str,
    ) -> dict[str, str]:
        return sign_headers(
            method=method,
            host=self.config.host,
            path=path,
            query=query,
            headers=headers,
            payload_hash=payload_hash,
            access_key=self._credentials.access_key_id,
            secret_key=self._credentials.secret_access_key,
            region=self.config.region,
            service="s3",
            now=self._clock(),
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        payload_hash: str | None = None,
    ) -> httpx.Response:
        digest = payload_hash or (hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256)
        signed = self._signed(method, path, query=query, headers=headers, payload_hash=digest)
        url = f"{self.config.scheme}://{self.config.host}{path}"
        client = self._ensure()
        try:
            return await client.request(
                method, url, params=query, content=body or None, headers=signed, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(
                "DELIVERY_UNAVAILABLE",
                "The delivery provider could not be reached.",
                detail={"method": method, "error": type(exc).__name__},
            ) from exc

    @staticmethod
    def _fail(response: httpx.Response, *, action: str, key: str | None = None) -> DeliveryError:
        if response.status_code in (401, 403):
            return DeliveryError(
                "DELIVERY_FORBIDDEN",
                "The delivery provider rejected the credentials.",
                http_status=409,
                detail={"action": action, "status": response.status_code},
            )
        if response.status_code == 404:
            return DeliveryError(
                "DELIVERY_NOT_FOUND",
                "The object or bucket does not exist.",
                http_status=404,
                detail={"action": action, "object_key": key},
            )
        if response.status_code == 429 or response.status_code >= 500:
            return DeliveryError(
                "DELIVERY_UNAVAILABLE",
                "The delivery provider is temporarily refusing requests.",
                detail={"action": action, "status": response.status_code},
            )
        return DeliveryError(
            "DELIVERY_UPLOAD_FAILED",
            "The delivery provider rejected the request.",
            detail={"action": action, "status": response.status_code, "object_key": key},
        )

    # -------------------------------------------------------------------- health
    async def health(self) -> DeliveryHealth:
        try:
            response = await self._send("HEAD", self.config.bucket_path())
        except DeliveryError as error:
            return DeliveryHealth(
                available=False,
                provider=self.name,
                detail=error.message,
                bucket=self.config.bucket,
                reason_code=error.code,
            )
        if response.status_code < 400:
            return DeliveryHealth(
                available=True,
                provider=self.name,
                detail="the bucket is reachable with the configured credentials",
                bucket=self.config.bucket,
            )
        if response.status_code in (401, 403):
            return DeliveryHealth(
                available=False,
                provider=self.name,
                detail="the bucket exists but the credentials are not allowed to use it",
                bucket=self.config.bucket,
                reason_code="DELIVERY_FORBIDDEN",
            )
        return DeliveryHealth(
            available=False,
            provider=self.name,
            detail=f"the bucket answered HTTP {response.status_code}",
            bucket=self.config.bucket,
            reason_code="DELIVERY_UNAVAILABLE",
        )

    # -------------------------------------------------------------------- upload
    async def upload(
        self,
        path: Any,
        *,
        object_key: str,
        sha256: str,
        size_bytes: int,
        content_type: str = "video/mp4",
    ) -> DeliveryReceipt:
        source = Path(path)
        if not source.exists():
            raise DeliveryError(
                "DELIVERY_SOURCE_MISSING",
                "The file to deliver no longer exists on disk.",
                http_status=409,
                detail={"object_key": object_key},
            )
        existing = await self.head(object_key)
        if existing is not None and existing.bytes == size_bytes and existing.sha256 == sha256:
            # The identical object is already there: never re-upload, never bill twice.
            return DeliveryReceipt(
                bucket=self.config.bucket,
                object_key=object_key,
                bytes=existing.bytes,
                sha256=sha256,
                etag=existing.etag,
                uploaded_at=self._clock(),
                parts=0,
            )

        if size_bytes <= self.config.part_size:
            receipt = await self._upload_single(source, object_key, content_type=content_type)
        else:
            receipt = await self._upload_multipart(source, object_key, content_type=content_type)

        info = await self.head(object_key)
        if info is None:
            raise DeliveryError(
                "DELIVERY_VERIFY_FAILED",
                "The upload reported success but the object is not there.",
                detail={"object_key": object_key},
            )
        if info.bytes != size_bytes:
            raise DeliveryError(
                "DELIVERY_VERIFY_FAILED",
                "The stored object is a different size from the file that was uploaded.",
                detail={"expected_bytes": size_bytes, "stored_bytes": info.bytes},
            )
        if info.sha256 and info.sha256 != sha256:
            raise DeliveryError(
                "DELIVERY_VERIFY_FAILED",
                "The stored object's checksum does not match the file that was uploaded.",
                detail={"expected_sha256": sha256, "stored_sha256": info.sha256},
            )
        return receipt

    async def _upload_single(self, source: Path, object_key: str, *, content_type: str) -> DeliveryReceipt:
        body = source.read_bytes()
        headers = {"Content-Type": content_type, "x-amz-meta-sha256": _meta_sha(body)}
        response = await self._send(
            "PUT", self.config.url_path(object_key), headers=headers, body=body
        )
        if response.status_code >= 400:
            raise self._fail(response, action="put", key=object_key)
        return DeliveryReceipt(
            bucket=self.config.bucket,
            object_key=object_key,
            bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            etag=response.headers.get("etag"),
            uploaded_at=self._clock(),
        )

    async def _upload_multipart(self, source: Path, object_key: str, *, content_type: str) -> DeliveryReceipt:
        path = self.config.url_path(object_key)
        created = await self._send(
            "POST",
            path,
            query={"uploads": ""},
            headers={"Content-Type": content_type, "x-amz-meta-sha256": _meta_sha(None, source)},
            body=b"",
        )
        if created.status_code >= 400:
            raise self._fail(created, action="create_multipart", key=object_key)
        upload_id = _xml_text(created.text, "UploadId")
        if not upload_id:
            raise DeliveryError(
                "DELIVERY_UPLOAD_FAILED",
                "The delivery provider did not return an upload id.",
                detail={"object_key": object_key},
            )

        parts: list[tuple[int, str]] = []
        digest = hashlib.sha256()
        total = 0
        try:
            with source.open("rb") as handle:
                index = 1
                while True:
                    chunk = handle.read(self.config.part_size)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    response = await self._send(
                        "PUT",
                        path,
                        query={"partNumber": str(index), "uploadId": upload_id},
                        body=chunk,
                        payload_hash=hashlib.sha256(chunk).hexdigest(),
                    )
                    if response.status_code >= 400:
                        raise self._fail(response, action=f"upload_part_{index}", key=object_key)
                    etag = response.headers.get("etag")
                    if not etag:
                        raise DeliveryError(
                            "DELIVERY_UPLOAD_FAILED",
                            "A part upload returned no ETag, so the upload cannot be completed.",
                            detail={"object_key": object_key, "part": index},
                        )
                    parts.append((index, etag))
                    index += 1
        except DeliveryError:
            await self._abort(object_key, upload_id)
            raise
        except OSError as exc:
            await self._abort(object_key, upload_id)
            raise DeliveryError(
                "DELIVERY_SOURCE_MISSING",
                "Reading the file failed during the upload.",
                http_status=409,
                detail={"object_key": object_key, "error": type(exc).__name__},
            ) from exc

        if not parts:
            await self._abort(object_key, upload_id)
            raise DeliveryError(
                "DELIVERY_SOURCE_MISSING",
                "The file to deliver is empty.",
                http_status=409,
                detail={"object_key": object_key},
            )

        completed = await self._send(
            "POST",
            path,
            query={"uploadId": upload_id},
            body=_complete_parts(parts),
            headers={"Content-Type": "application/xml"},
        )
        if completed.status_code >= 400:
            await self._abort(object_key, upload_id)
            raise self._fail(completed, action="complete_multipart", key=object_key)

        return DeliveryReceipt(
            bucket=self.config.bucket,
            object_key=object_key,
            bytes=total,
            sha256=digest.hexdigest(),
            etag=completed.headers.get("etag") or _xml_text(completed.text, "ETag"),
            uploaded_at=self._clock(),
            parts=len(parts),
        )

    async def _abort(self, object_key: str, upload_id: str) -> None:
        try:
            await self._send(
                "DELETE",
                self.config.url_path(object_key),
                query={"uploadId": upload_id},
                body=b"",
            )
        except DeliveryError:
            # Aborting is best effort; the failure that caused it is the one worth reporting.
            return
        return

    # ---------------------------------------------------------------------- head
    async def head(self, object_key: str) -> ObjectInfo | None:
        response = await self._send("HEAD", self.config.url_path(object_key), body=b"")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise self._fail(response, action="head", key=object_key)
        return ObjectInfo(
            object_key=object_key,
            bytes=int(response.headers.get("content-length") or 0),
            sha256=response.headers.get("x-amz-meta-sha256"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    # ---------------------------------------------------------------------- sign
    async def sign_get(self, object_key: str, *, ttl_s: int | None = None) -> SignedUrl:
        ttl = int(ttl_s or self.config.url_ttl_s)
        now = self._clock()
        url = presign_url(
            scheme=self.config.scheme,
            host=self.config.host,
            path=self.config.url_path(object_key),
            access_key=self._credentials.access_key_id,
            secret_key=self._credentials.secret_access_key,
            region=self.config.region,
            service="s3",
            expires_s=ttl,
            now=now,
        )
        if self.config.public_base_url:
            # A CDN in front of the bucket serves the object without a signature; the configured
            # base URL is used verbatim so a stale cache key is not silently different.
            base = str(self.config.public_base_url).rstrip("/")
            url = f"{base}/{object_key}"
        return SignedUrl(url=url, expires_at=now + timedelta(seconds=ttl), object_key=object_key)

    # -------------------------------------------------------------------- delete
    async def delete(self, object_key: str) -> bool:
        response = await self._send("DELETE", self.config.url_path(object_key), body=b"")
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise self._fail(response, action="delete", key=object_key)
        return True


def _meta_sha(body: bytes | None, path: Path | None = None) -> str:
    """sha256 stored as object metadata, so a future HEAD can prove what is really there."""
    if body is not None:
        return hashlib.sha256(body).hexdigest()
    assert path is not None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _xml_text(document: str, tag: str) -> str | None:
    if not document:
        return None
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError:
        return None
    for name in (f"{_NS}{tag}", tag):
        found = root.find(f".//{name}")
        if found is not None and found.text:
            return found.text.strip()
    return None


def _complete_parts(parts: list[tuple[int, str]]) -> bytes:
    body = ["<CompleteMultipartUpload>"]
    for number, etag in parts:
        body.append(f"<Part><PartNumber>{number}</PartNumber><ETag>{etag}</ETag></Part>")
    body.append("</CompleteMultipartUpload>")
    return "".join(body).encode("utf-8")
