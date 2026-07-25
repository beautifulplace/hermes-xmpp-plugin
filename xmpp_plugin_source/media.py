"""Media download and AES-GCM helpers for XMPP."""

import hashlib
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


_MEDIA_CACHE_DIR = Path.home() / ".hermes" / "cache" / "images"


def is_aesgcm_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith("aesgcm://")


def _parse_aesgcm_url(url: str) -> Optional[Tuple[bytes, bytes, str]]:
    """Parse an aesgcm:// URL.

    Returns (iv, key, https_url) or None on failure.
    XEP-0454 uses a hex fragment: 12-byte IV (24 hex chars) + 32-byte key (64 hex chars).
    """
    try:
        parsed = urlparse(url)
        fragment = parsed.fragment
        if not fragment or len(fragment) < 88:
            return None
        iv = bytes.fromhex(fragment[:24])
        key = bytes.fromhex(fragment[24:88])
        https_url = f"https://{parsed.netloc}{parsed.path}"
        return iv, key, https_url
    except Exception as exc:
        logger.debug("XMPP: failed to parse aesgcm URL: %s", exc)
        return None


def _extension_from_content(data: bytes) -> str:
    # Avoid non-ASCII byte literals by comparing integer sequences.
    if data[:2] == bytes([0xFF, 0xD8]):
        return ".jpg"
    if data[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
        return ".png"
    if data[:6] == bytes([0x47, 0x49, 0x46, 0x38, 0x37, 0x61]) or data[:6] == bytes([0x47, 0x49, 0x46, 0x38, 0x39, 0x61]):
        return ".gif"
    if data[:4] == bytes([0x1A, 0x45, 0xDF, 0xA3]):
        return ".webm"
    if data[:4] == bytes([0x4F, 0x67, 0x67, 0x53]):
        return ".ogg"
    if data[:4] == bytes([0x52, 0x49, 0x46, 0x46]):
        return ".wav"
    if data[:3] == bytes([0x49, 0x44, 0x33]) or data[:2] == bytes([0xFF, 0xFB]) or data[:2] == bytes([0xFF, 0xF3]):
        return ".mp3"
    return ""


def _extension_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        ext = Path(path).suffix.lower()
        if ext:
            return ext
    except Exception:
        pass
    return ""


def _content_type_from_extension(ext: str) -> str:
    mime, _ = mimetypes.guess_type(f"file{ext}")
    return mime or "application/octet-stream"


async def cache_media(url: str) -> Optional[str]:
    """Download an aesgcm:// or https:// URL, decrypt if needed, and cache locally."""
    _MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(url)
    is_encrypted = parsed.scheme == "aesgcm"

    if is_encrypted:
        parsed_aes = _parse_aesgcm_url(url)
        if parsed_aes is None:
            logger.warning("XMPP: could not parse aesgcm URL")
            return None
        iv, key, https_url = parsed_aes
    else:
        https_url = url
        iv = key = None

    url_ext = _extension_from_url(https_url)
    base_name = hashlib.sha256(url.encode()).hexdigest()[:8]

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(https_url)
            response.raise_for_status()
            data = response.content
    except Exception as exc:
        logger.warning("XMPP: media download failed: %s", exc)
        return None

    if is_encrypted and iv is not None and key is not None:
        try:
            aesgcm = AESGCM(key)
            data = aesgcm.decrypt(iv, data, None)
            logger.debug("XMPP: aesgcm decrypted %d bytes", len(data))
        except Exception as exc:
            logger.warning("XMPP: aesgcm decryption failed: %s", exc)
            return None

    content_ext = _extension_from_content(data)
    ext = url_ext or content_ext or ".bin"
    suffix = f"_{base_name}{ext}"
    mime = _content_type_from_extension(ext)
    if mime.startswith("audio/"):
        prefix = "xmpp_audio"
    elif mime.startswith("image/"):
        prefix = "xmpp_image"
    elif mime.startswith("video/"):
        prefix = "xmpp_video"
    else:
        prefix = "xmpp_file"

    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=_MEDIA_CACHE_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        logger.info("XMPP: cached inbound %s media to %s", mime, path)
        return path
    except Exception as exc:
        logger.warning("XMPP: failed to write cached media: %s", exc)
        try:
            os.close(fd)
        except OSError:
            pass
        return None
