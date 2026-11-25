"""Sortable identifier generation.

ULID: 48-bit millisecond timestamp + 80 bits of randomness, Crockford base32.
Chosen over UUID4 so that primary keys sort chronologically, which keeps SQLite
B-tree locality good and makes ID ranges meaningful in the audit log.
"""
from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I, L, O, U)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


# ULIDs generated in the same millisecond must still sort in creation order, so the
# random component is incremented (not re-rolled) within a millisecond. Without this,
# rows created in one millisecond ordered by (created_at, id) come back in random order.
_id_lock = threading.Lock()
_last_ms = 0
_last_random = 0
_MAX_RANDOM = (1 << 80) - 1


def new_ulid() -> str:
    """Return a 26-character, strictly increasing, lexicographically sortable id."""
    global _last_ms, _last_random
    with _id_lock:
        now = int(time.time() * 1000)
        if now <= _last_ms:
            # Same millisecond (or the clock stepped back): stay monotonic.
            now = _last_ms
            _last_random += 1
            if _last_random > _MAX_RANDOM:
                now += 1
                _last_random = int.from_bytes(os.urandom(10), "big")
        else:
            _last_random = int.from_bytes(os.urandom(10), "big")
        _last_ms = now
        randomness = _last_random
    return _encode(now & ((1 << 48) - 1), 10) + _encode(randomness, 16)


def ulid_timestamp(ulid: str) -> int:
    """Milliseconds encoded in a ULID (useful for debugging and audit display)."""
    if len(ulid) != 26:
        raise ValueError(f"not a ULID: {ulid!r}")
    value = 0
    for char in ulid[:10]:
        digit = _ALPHABET.index(char)
        value = (value << 5) | digit
    return value


def new_idempotency_key(*parts: str) -> str:
    """Deterministic key for an external side effect (INV-9).

    Same logical action must produce the same key so a retry can never create a
    second post. Inputs are normalised so cosmetic edits do not change identity.
    """
    import hashlib

    joined = "\x1f".join(part.strip() for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
