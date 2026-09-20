"""Zstd helpers for structured audit and browser-state payloads."""

from __future__ import annotations

import json
from typing import Any

import zstandard as zstd

DEFAULT_LEVEL = 3


def zstd_compress(data: bytes, *, level: int = DEFAULT_LEVEL) -> bytes:
    """Compress raw bytes with Zstd.

    Args:
        data: Uncompressed payload.
        level: Zstd compression level (higher is denser, slower).

    Returns:
        Zstd frame bytes.
    """
    compressor = zstd.ZstdCompressor(level=level)
    return compressor.compress(data)


def zstd_decompress(data: bytes) -> bytes:
    """Decompress a Zstd frame to raw bytes.

    Args:
        data: Zstd-compressed payload.

    Returns:
        Original uncompressed bytes.

    Raises:
        zstd.ZstdError: If the frame is truncated or corrupt.
    """
    decompressor = zstd.ZstdDecompressor()
    return decompressor.decompress(data)


def zstd_encode_json(obj: Any, *, level: int = DEFAULT_LEVEL) -> bytes:
    """Serialize ``obj`` as UTF-8 JSON and compress with Zstd.

    Args:
        obj: JSON-serializable value (dict, list, scalars, …).
        level: Zstd compression level.

    Returns:
        Zstd-compressed JSON bytes.
    """
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return zstd_compress(raw, level=level)


def zstd_decode_json(data: bytes) -> Any:
    """Decompress Zstd bytes and parse UTF-8 JSON.

    Args:
        data: Zstd-compressed JSON payload.

    Returns:
        Parsed JSON value.

    Raises:
        zstd.ZstdError: If decompression fails.
        json.JSONDecodeError: If the inner payload is not valid JSON.
    """
    raw = zstd_decompress(data)
    return json.loads(raw.decode("utf-8"))
