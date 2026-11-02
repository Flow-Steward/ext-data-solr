"""Pure, bounded, lossless document preparation for vector ingestion."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024
RESERVED_KEYS = frozenset(
    {
        "id",
        "score",
        "document_id",
        "chunk_id",
        "chunk_index",
        "text",
        "start",
        "end",
        "source_uri",
        "title",
        "metadata",
        "content_hash",
        "source_id",
        "excerpt",
        "fs_scope",
    }
)


def bounded_string(value: Any, name: str, maximum: int, *, required: bool = False) -> str:
    """Validate UTF-8 text without altering its contents."""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be a string with the required content")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if size > maximum:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit")
    return value


def safe_source_uri(value: Any) -> str:
    """Permit descriptive HTTP(S)/artifact references, never credentials."""
    uri = bounded_string(value, "source_uri", 4096)
    if not uri:
        return uri
    if any(ord(char) < 33 or ord(char) == 127 for char in uri) or "\\" in uri:
        raise ValueError("source_uri contains unsafe characters")
    try:
        parsed = urlsplit(uri)
        if parsed.scheme not in {"http", "https", "artifact"} or not parsed.netloc:
            raise ValueError("source_uri must be an HTTP(S) or artifact URI")
        if parsed.username is not None or parsed.password is not None or not parsed.hostname:
            raise ValueError("source_uri must not contain credentials")
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("source_uri is invalid") from exc
    return uri


def safe_metadata(value: Any) -> dict:
    """Copy bounded JSON metadata and prevent internal payload-key spoofing."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    if any(
        not isinstance(key, str) or key in RESERVED_KEYS or key.startswith("_") for key in value
    ):
        raise ValueError("metadata contains a reserved key")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError("metadata exceeds its JSON byte limit")
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("metadata must be bounded JSON") from exc
