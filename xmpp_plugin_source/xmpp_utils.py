"""Utility helpers for the Hermes XMPP platform adapter."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from typing import Any, Optional


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse a loose boolean value from env/config."""
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _load_yaml_lib():
    """Return (yaml_module, is_ruamel)."""
    try:
        from ruamel.yaml import YAML

        return YAML(), True
    except ImportError:
        pass
    try:
        import yaml

        return yaml, False
    except ImportError:
        pass
    return None, False


def _load_config(text: str):
    mod, is_ruamel = _load_yaml_lib()
    if mod is None:
        raise RuntimeError("No YAML library available")
    if is_ruamel:
        return mod.load(text) or {}
    return mod.safe_load(text) or {}


def _dump_config(data):
    mod, is_ruamel = _load_yaml_lib()
    if is_ruamel:
        mod.default_flow_style = False
        mod.indent(mapping=2, sequence=4, offset=2)
        stream = StringIO()
        mod.dump(data, stream)
        return stream.getvalue()
    return mod.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _cleanup_empty_blocks(data):
    if not isinstance(data, dict):
        return data
    cleaned = {}
    for key, value in data.items():
        if isinstance(value, dict):
            value = _cleanup_empty_blocks(value)
            if value:
                cleaned[key] = value
        elif isinstance(value, list):
            value = [v for v in value if v not in (None, "", {})]
            if value:
                cleaned[key] = value
        elif value not in (None, ""):
            cleaned[key] = value
    return cleaned


def set_nested_config_value(
    config_text: str,
    top_key: str,
    sub_key: str,
    option: str,
    value: Any,
) -> str:
    """Set a scalar value inside a nested block (e.g. platforms.xmpp.home_channel)."""
    data = _load_config(config_text)
    top = data.setdefault(top_key, {})
    if not isinstance(top, dict):
        top = {}
        data[top_key] = top
    sub = top.setdefault(sub_key, {})
    if not isinstance(sub, dict):
        sub = {}
        top[sub_key] = sub
    sub[option] = value
    data = _cleanup_empty_blocks(data)
    return _dump_config(data)


def guess_content_type(data: bytes) -> str:
    """Inspect file magic bytes to determine the real content type."""
    if len(data) < 4:
        return "unknown"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/m4a"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"ID3"):
        return "audio/mp3"
    if len(data) >= 2 and data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mp3"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    return "unknown"


def guess_extension_from_data(data: bytes) -> str:
    """Return a file extension based on actual file content."""
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "audio/m4a": ".m4a",
        "audio/ogg": ".ogg",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/webm": ".webm",
    }.get(guess_content_type(data), "")


def mime_from_extension(ext: str) -> str:
    """Return an audio MIME type for common extensions."""
    return {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".opus": "audio/opus",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }.get(ext.lower(), "audio/mp4")


def is_voice_url(url: str, body: str = "") -> bool:
    """Heuristic to decide if an incoming audio URL is a voice message."""
    lowered = url.lower()
    if not lowered:
        return False
    if url.startswith("aesgcm://"):
        return True
    if "voice-message" in lowered:
        return True
    if any(lowered.endswith(ext) for ext in (".ogg", ".oga", ".opus", ".webm")):
        return True
    stripped = body.strip()
    if stripped == url or len(stripped) <= len(url) + 10:
        return True
    return False


def guess_audio_extension(url: str, data: bytes) -> str:
    """Return a sensible file extension for an audio file."""
    ext = guess_extension_from_data(data)
    if ext:
        return ext
    lowered = url.lower()
    for candidate in (".m4a", ".mp4", ".ogg", ".oga", ".opus", ".mp3", ".webm", ".wav"):
        if lowered.endswith(candidate):
            return candidate
    return ".ogg"


def extract_url(text: str) -> Optional[str]:
    """Return the first URL found in text, or None."""
    match = re.search(r"https?://\S+|aesgcm://\S+", text)
    return match.group(0) if match else None


def cache_media_path(data: bytes, kind: str = "image", ext: Optional[str] = None) -> Path:
    """Return a deterministic cache file path under ~/.hermes/cache/images."""
    from hermes_constants import get_hermes_home

    if not ext:
        ext = guess_extension_from_data(data) or ".bin"
    filename = f"xmpp_{kind}_{hash(data) & 0xffffffff:08x}{ext}"
    cache_dir = get_hermes_home() / "cache" / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / filename
    path.write_bytes(data)
    return path
