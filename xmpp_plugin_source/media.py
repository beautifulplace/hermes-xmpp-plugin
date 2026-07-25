"""OMEMO media download/decrypt helpers for the Hermes XMPP platform adapter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from slixmpp.jid import JID

from .xmpp_utils import cache_media_path, extract_url, guess_audio_extension

logger = logging.getLogger(__name__)


async def download_url(url: str, http: httpx.AsyncClient) -> Optional[bytes]:
    """Download raw bytes from an http(s) URL."""
    try:
        response = await http.get(url, timeout=120.0, follow_redirects=True)
        response.raise_for_status()
        return response.content
    except Exception as exc:
        logger.warning("XMPP: media download failed for %s: %s", url, exc)
        return None


def _parse_aesgcm_url(url: str) -> tuple[Optional[str], Optional[bytes]]:
    """Parse an aesgcm:// URL into (https_url, key_material).

    XEP-0454 (OMEMO media sharing) uses a hex fragment of the form:
        <12-byte IV (24 hex chars)><32-byte key (64 hex chars)>
    for AES-256-GCM.
    """
    parsed = urlparse(url)
    fragment = parsed.fragment
    if not fragment:
        logger.warning("XMPP: aesgcm URL has no fragment: %s", url)
        return None, None

    if parsed.scheme == "aesgcm":
        https_url = f"https://{parsed.netloc}{parsed.path}"
    else:
        https_url = url

    try:
        key_material = bytes.fromhex(fragment)
    except ValueError as exc:
        logger.warning("XMPP: aesgcm URL fragment is not valid hex: %s", exc)
        return https_url, None

    return https_url, key_material


async def download_aesgcm(
    url: str,
    http: httpx.AsyncClient,
) -> Optional[bytes]:
    """Download and decrypt an aesgcm:// OMEMO media sharing URL."""
    https_url, key_material = _parse_aesgcm_url(url)
    if not https_url:
        logger.warning("XMPP: aesgcm URL parse failed: %s", url)
        return None
    if not key_material or len(key_material) != 44:
        logger.warning(
            "XMPP: aesgcm key material wrong length %d (expected 44) for %s",
            len(key_material) if key_material else 0,
            url,
        )
        return None

    data = await download_url(https_url, http)
    if data is None:
        return None

    # XEP-0454 fragment layout: 12-byte IV + 32-byte key
    iv = key_material[:12]
    key = key_material[12:]

    try:
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(iv, data, None)
        return plaintext
    except Exception as exc:
        logger.warning("XMPP: aesgcm decryption failed: %s", exc)
        return None


async def resolve_inbound_media(
    body: str,
    http: httpx.AsyncClient,
    kind: str = "image",
) -> tuple[str, Optional[Path]]:
    """If body contains a media URL, download/decrypt it and return (clean_body, path)."""
    url = extract_url(body)
    if not url:
        return body, None

    if url.startswith("aesgcm://"):
        data = await download_aesgcm(url, http)
    else:
        data = await download_url(url, http)

    if data is None:
        return body, None

    ext = guess_audio_extension(url, data) if kind == "audio" else guess_extension_from_data(data)
    if not ext:
        ext = guess_extension_from_data(data)
    if not ext:
        ext = ".bin"
    path = cache_media_path(data, kind=kind, ext=ext)

    # Replace the URL in the body with the local cache path so downstream tools
    # (vision, transcription) can read it.
    clean_body = body.replace(url, str(path))
    return clean_body, path


def guess_extension_from_data(data: bytes) -> str:
    """Forward helper used by media.py without importing utils twice."""
    from .xmpp_utils import guess_extension_from_data as _guess

    return _guess(data)


def jid_str(jid: JID | str) -> str:
    """Normalize a JID or string to its bare string form."""
    if isinstance(jid, str):
        return jid
    return str(jid.bare)
