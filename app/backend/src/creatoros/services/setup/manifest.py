"""Pinned prerequisite manifest for the first-run setup experience.

A component entry is the only place a download URL, byte count, SHA-256 or expected signer
subject may live. The setup service verifies against this file and never against a value it
invented: a hash that came from a chat message is a claim, not a verification.

Loading is strict. A malformed manifest raises `ManifestError` instead of silently dropping an
entry, because a manifest that "mostly" parses is how an unverified payload reaches a user. The
file ships inside the `creatoros` package, so `scripts/build-backend.ps1` copies it with the rest of
the package and the installer signature covers it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_ID = "creatoros.prerequisites.v1"
MANIFEST_PATH = Path(__file__).resolve().parents[2] / "resources" / "prerequisites.v1.json"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KINDS = frozenset({"vendor-installer", "local-tool"})
_ACQUISITION_MODES = frozenset({"official-download", "local"})


class ManifestError(ValueError):
    """The manifest is missing, unreadable or internally inconsistent."""


@dataclass(frozen=True)
class Component:
    """One prerequisite, exactly as pinned in the manifest."""

    id: str
    kind: str
    display_name: str
    version: str
    required: bool
    acquisition: dict[str, Any]
    signature: dict[str, Any]
    install: dict[str, Any]
    consent: dict[str, Any]
    license: dict[str, Any]
    redistribution: dict[str, Any]

    @property
    def filename(self) -> str:
        return str(self.acquisition.get("filename") or "")

    @property
    def size_bytes(self) -> int | None:
        value = self.acquisition.get("size_bytes")
        return int(value) if isinstance(value, int) and value > 0 else None

    @property
    def sha256(self) -> str | None:
        value = self.acquisition.get("sha256")
        return str(value).lower() if isinstance(value, str) else None

    @property
    def download_url(self) -> str | None:
        if self.acquisition.get("mode") != "official-download":
            return None
        url = self.acquisition.get("url")
        return str(url) if url else None

    @property
    def release_page(self) -> str | None:
        url = self.acquisition.get("release_page")
        return str(url) if url else None

    @property
    def needs_consent(self) -> bool:
        return bool(self.consent.get("required"))

    @property
    def requires_signature(self) -> bool:
        return bool(self.signature.get("required"))

    @property
    def expected_subject(self) -> str | None:
        subject = self.signature.get("expected_subject")
        return str(subject) if subject else None

    @property
    def redistribution_approved(self) -> bool:
        return bool(self.redistribution.get("approved_for_bundling"))

    def to_public_dict(self) -> dict[str, Any]:
        """What the API may disclose. Contains no secret: a hash and a public URL are both public."""
        return {
            "id": self.id,
            "kind": self.kind,
            "display_name": self.display_name,
            "version": self.version,
            "required": self.required,
            "filename": self.filename or None,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "download_url": self.download_url,
            "release_page": self.release_page,
            "vendor": self.acquisition.get("vendor"),
            "license": self.license,
            "redistribution": self.redistribution,
            "consent": self.consent,
            "signature_required": self.requires_signature,
            "expected_subject": self.expected_subject,
        }


@dataclass(frozen=True)
class Manifest:
    schema: str
    updated: str
    source: str
    components: tuple[Component, ...]

    def get(self, component_id: str) -> Component:
        for component in self.components:
            if component.id == component_id:
                return component
        raise KeyError(component_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(component.id for component in self.components)


def _require(document: dict[str, Any], key: str, where: str) -> Any:
    if key not in document:
        raise ManifestError(f"{where} is missing {key!r}")
    return document[key]

def _require_object(document: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = _require(document, key, where)
    if not isinstance(value, dict):
        raise ManifestError(f"{where}.{key} must be an object")
    return value


def _validate_component(raw: Any, index: int) -> Component:
    where = f"components[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be an object")

    component_id = str(_require(raw, "id", where))
    if not _ID_PATTERN.match(component_id):
        raise ManifestError(f"{where}.id {component_id!r} is not a lowercase slug")

    kind = str(_require(raw, "kind", where))
    if kind not in _KINDS:
        raise ManifestError(f"{where}.kind {kind!r} is not one of {sorted(_KINDS)}")

    acquisition = _require_object(raw, "acquisition", where)
    mode = str(_require(acquisition, "mode", f"{where}.acquisition"))
    if mode not in _ACQUISITION_MODES:
        raise ManifestError(f"{where}.acquisition.mode {mode!r} is not one of {sorted(_ACQUISITION_MODES)}")

    filename = str(_require(acquisition, "filename", f"{where}.acquisition"))
    if not filename or filename != Path(filename).name:
        raise ManifestError(f"{where}.acquisition.filename must be a bare file name, got {filename!r}")

    sha256 = str(_require(acquisition, "sha256", f"{where}.acquisition")).lower()
    if not _SHA256_PATTERN.match(sha256):
        raise ManifestError(f"{where}.acquisition.sha256 is not a 64-character lowercase hex digest")

    size = _require(acquisition, "size_bytes", f"{where}.acquisition")
    if not isinstance(size, int) or size <= 0:
        raise ManifestError(f"{where}.acquisition.size_bytes must be a positive integer")

    if mode == "official-download":
        url = str(_require(acquisition, "url", f"{where}.acquisition"))
        if not url.startswith("https://"):
            raise ManifestError(f"{where}.acquisition.url must be https, got {url!r}")
        if not _require_object(raw, "consent", where).get("required"):
            # A network acquisition without consent is exactly what the plan forbids.
            raise ManifestError(f"{where} downloads over the network but does not require consent")

    signature = _require_object(raw, "signature", where)
    if signature.get("required") and not signature.get("expected_subject"):
        raise ManifestError(f"{where}.signature requires a signer but declares no expected_subject")

    license_block = _require_object(raw, "license", where)
    if not license_block.get("name"):
        raise ManifestError(f"{where}.license needs a name")

    redistribution = _require_object(raw, "redistribution", where)
    if not isinstance(redistribution.get("approved_for_bundling"), bool):
        raise ManifestError(f"{where}.redistribution.approved_for_bundling must be a boolean")

    return Component(
        id=component_id,
        kind=kind,
        display_name=str(_require(raw, "display_name", where)),
        version=str(_require(raw, "version", where)),
        required=bool(raw.get("required", False)),
        acquisition=acquisition,
        signature=signature,
        install=_require_object(raw, "install", where),
        consent=_require_object(raw, "consent", where),
        license=license_block,
        redistribution=redistribution,
    )


def parse_manifest(document: dict[str, Any]) -> Manifest:
    """Validate a decoded manifest document and return it as typed components."""
    if not isinstance(document, dict):
        raise ManifestError("manifest must be a JSON object")
    schema = str(_require(document, "schema", "manifest"))
    if schema != SCHEMA_ID:
        raise ManifestError(f"manifest schema is {schema!r}, expected {SCHEMA_ID!r}")
    components_raw = _require(document, "components", "manifest")
    if not isinstance(components_raw, list) or not components_raw:
        raise ManifestError("manifest.components must be a non-empty list")

    components: list[Component] = []
    seen: set[str] = set()
    for index, raw in enumerate(components_raw):
        component = _validate_component(raw, index)
        if component.id in seen:
            raise ManifestError(f"duplicate component id {component.id!r}")
        seen.add(component.id)
        components.append(component)

    return Manifest(
        schema=schema,
        updated=str(document.get("updated") or ""),
        source=str(document.get("source") or ""),
        components=tuple(components),
    )


@lru_cache(maxsize=4)
def _load_cached(path: str) -> Manifest:
    file = Path(path)
    if not file.is_file():
        raise ManifestError(f"prerequisite manifest not found: {file}")
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"prerequisite manifest is unreadable: {exc}") from exc
    return parse_manifest(document)


def load_manifest(path: Path | str | None = None) -> Manifest:
    """Load and validate the manifest. Raises `ManifestError`, never returns a partial manifest."""
    return _load_cached(str(Path(path) if path is not None else MANIFEST_PATH))


def clear_manifest_cache() -> None:
    """Tests and tooling may replace the manifest file; never call this on a request path."""
    _load_cached.cache_clear()
