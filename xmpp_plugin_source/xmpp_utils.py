"""XMPP utility helpers."""

import logging
import mimetypes
import re
from typing import Optional

logger = logging.getLogger(__name__)


_URL_RE = re.compile(r"(https?://|aesgcm://)[^\\s<>\"{}|\\\\^`[\]]+")


def extract_url(text: str) -> Optional[str]:
    """Return the first URL found in *text*, or None."""
    if not text:
        return None
    match = _URL_RE.search(text)
    return match.group(0) if match else None


def is_voice_url(url: str) -> bool:
    """Return True only when *url* points to actual audio content."""
    if not url:
        return False
    url_lower = url.lower()
    audio_exts = (".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac", ".opus", ".wma")
    if any(url_lower.endswith(ext) for ext in audio_exts):
        return True
    if "voice" in url_lower:
        return True
    # Check MIME type from extension.
    mime, _ = mimetypes.guess_type(url)
    if mime and mime.startswith("audio/"):
        return True
    return False


def mime_from_extension(ext: str) -> str:
    if not ext.startswith("."):
        ext = f".{ext}"
    mime, _ = mimetypes.guess_type(f"file{ext}")
    return mime or "application/octet-stream"
