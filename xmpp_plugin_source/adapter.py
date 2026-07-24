import asyncio
import io
import logging
import os
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_media_bytes,
    validate_inbound_media_size,
)
from PIL import Image
from slixmpp import JID, ClientXMPP
from slixmpp.plugins.base import register_plugin
from slixmpp.stanza import Message
from tools.transcription_tools import transcribe_audio

logger = logging.getLogger(__name__)


def _omemo_available() -> bool:
    try:
        import slixmpp_omemo  # noqa: F401
        return True
    except Exception:
        return False


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _guess_content_type(data: bytes) -> str:
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


def _guess_extension_from_data(data: bytes) -> str:
    """Return a file extension based on actual file content."""
    content_type = _guess_content_type(data)
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
    }.get(content_type, "")


def _mime_from_extension(ext: str) -> str:
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


def _is_audio_url(url: str) -> bool:
    return any(url.lower().endswith(ext) for ext in (
        ".ogg", ".oga", ".mp3", ".m4a", ".webm", ".wav", ".opus"
    ))


def _is_voice_url(url: str) -> bool:
    """Return True if the URL looks like a voice message rather than a generic audio file."""
    lowered = url.lower()
    # Conversations and similar clients often use voice-message-* filenames.
    if "voice-message" in lowered:
        return True
    # Container formats commonly used for voice messages.
    return any(lowered.endswith(ext) for ext in (".ogg", ".oga", ".opus", ".webm"))


def _guess_audio_is_voice(url: str, body: str) -> bool:
    """Heuristic to decide if an incoming audio URL is a voice message."""
    lowered = url.lower()
    # aesgcm:// URLs are only used for OMEMO media sharing in XMPP clients,
    # and audio uploads that way are almost always voice messages.
    if url.startswith("aesgcm://"):
        return True
    # Conversations and similar clients often use voice-message-* filenames.
    if "voice-message" in lowered:
        return True
    # Container formats commonly used for voice messages.
    if any(lowered.endswith(ext) for ext in (".ogg", ".oga", ".opus", ".webm")):
        return True
    # Voice messages are usually sent as standalone media with little or no
    # accompanying text. If the body is empty or just the URL, assume voice.
    stripped = body.strip()
    if not stripped or stripped == url or len(stripped) <= len(url) + 10:
        return True
    return False


def _guess_audio_extension(url: str, data: bytes) -> str:
    """Return a sensible file extension for an audio file.

    First inspects the file magic bytes, then falls back to the URL extension,
    then defaults to .ogg.
    """
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".m4a"
    if data.startswith(b"OggS"):
        # Could be .ogg, .oga, or .opus; .ogg is the safe default.
        return ".ogg"
    if data.startswith(b"ID3"):
        return ".mp3"
    if len(data) >= 2 and data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"

    lowered = url.lower()
    for ext in (".m4a", ".mp4", ".ogg", ".oga", ".opus", ".mp3", ".webm", ".wav"):
        if lowered.endswith(ext):
            return ext
    return ".ogg"

class XMPPAdapter(BasePlatformAdapter):
    """
    XMPP Platform Adapter for Hermes.

    Features:
      - Plain-text or OMEMO-encrypted messaging
      - XEP-0085 typing indicators
      - XEP-0333 read receipts (chat markers)
      - XEP-0066 / XEP-0363 inbound images, files, and voice messages
      - aesgcm:// OMEMO media sharing decryption
      - XEP-0084 avatar publishing
      - Outgoing voice/audio messages via the Hermes core TTS tool
    """

    def __init__(self, config, **kwargs):
        platform = Platform("xmpp")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self.user_jid = os.getenv("XMPP_USER_JID") or extra.get("user_jid", "")
        self.password = os.getenv("XMPP_PASSWORD") or extra.get("password", "")
        self.server = os.getenv("XMPP_SERVER") or extra.get("server", "")
        self.port = 5222
        raw_port = os.getenv("XMPP_PORT") or extra.get("port")
        if raw_port:
            try:
                self.port = int(raw_port)
            except (ValueError, TypeError):
                self.port = 5222

        self.omemo_enabled = _parse_bool(
            os.getenv("XMPP_OMEMO_ENABLED") or extra.get("omemo_enabled"), False
        )
        self.omemo_allow_untrusted = _parse_bool(
            os.getenv("XMPP_OMEMO_ALLOW_UNTRUSTED")
            or os.getenv("XMPP_OTR_ALLOW_UNTRUSTED")
            or extra.get("omemo_allow_untrusted"),
            True,
        )
        self.omemo_plugin_name = "xep_0384"
        self.omemo_storage_path: Optional[Path] = None
        self._omemo_ready_event = asyncio.Event()

        # Typing indicators are a core part of the XMPP chat UX and are always
        # enabled. They are not configurable.
        self.typing_indicator = True
        self.avatar_path = os.getenv("XMPP_AVATAR_PATH") or extra.get("avatar_path", "")
        self.home_channel = os.getenv("XMPP_HOME_CHANNEL") or extra.get("home_channel", "")

        self._session_started_event = asyncio.Event()
        self.client: Optional[ClientXMPP] = None
        self._http = httpx.AsyncClient(timeout=300.0, follow_redirects=True)
        self._keepalive_task: Optional[asyncio.Task] = None
        self._avatar_republish_task: Optional[asyncio.Task] = None
        self._internal_reconnect_task: Optional[asyncio.Task] = None
        self._xmpp_background_tasks: set[asyncio.Task] = set()
        self._last_activity: float = 0.0
        self._ping_interval = 30.0
        self._ping_timeout = 10.0

        # Track chats where the last inbound message was a voice message so we
        # can reply with TTS audio when voice.auto_tts is enabled and the gateway
        # uses the streaming response path (which skips base-adapter auto-TTS).
        self._voice_reply_chats: set[str] = set()
        self._last_resources: Dict[str, str] = {}
        # Track bare JIDs that have sent us OMEMO-encrypted messages so replies
        # to those chats are always encrypted rather than falling back to plaintext.
        self._omemo_chats: set[str] = set()

    @property
    def name(self) -> str:
        return "XMPP"

    # -- OMEMO helpers -------------------------------------------------------

    def _configure_omemo(self) -> bool:
        if not self.omemo_enabled or self.client is None:
            return False
        if not _omemo_available():
            logger.error(
                "XMPP_OMEMO_ENABLED=true but slixmpp-omemo is not installed; "
                "run: pip install slixmpp-omemo"
            )
            return False
        try:
            from .omemo_plugin import HermesOMEMO

            register_plugin(HermesOMEMO, name=self.omemo_plugin_name)
            self.client.register_plugin(
                self.omemo_plugin_name,
                pconfig={
                    "allow_untrusted": self.omemo_allow_untrusted,
                    "storage_path": str(self.omemo_storage_path) if self.omemo_storage_path else None,
                },
            )
            logger.info("XMPP: OMEMO plugin enabled")
            return True
        except Exception as exc:
            logger.exception("XMPP: failed to configure OMEMO: %s", exc)
            return False

    def _omemo_plugin(self) -> Any:
        if self.client is None:
            return None
        try:
            return self.client[self.omemo_plugin_name]
        except Exception:
            return None

    # -- Connection ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.user_jid or not self.password:
            logger.error("XMPP: user_jid and password are required")
            self._set_fatal_error(
                "missing_credentials", "XMPP user_jid/password missing", retryable=False
            )
            return False

        # If this is a reconnect, tear down the old client and tasks first so
        # we don't end up with two slixmpp event loops racing or the old one
        # silently swallowing events while the new one claims to be connected.
        if is_reconnect:
            logger.info("XMPP: tearing down old client before reconnect")
            await self._cleanup_client()

        self._session_started_event.clear()
        self._omemo_ready_event.clear()

        try:
            # Use a fixed Hermes resource if none was provided, and advertise
            # a service-discovery identity so XMPP clients label the account
            # as "Hermes" in tooltips/contact lists.
            jid_str = self.user_jid
            if "/" not in jid_str:
                jid_str = f"{jid_str}/Hermes"

            self.client = ClientXMPP(jid_str, self.password)
            self.client.use_message_ids = True
            self.client.register_plugin("xep_0030")  # Service discovery
            try:
                disco = self.client.plugin.get("xep_0030")
                if disco is not None:
                    disco.add_identity(
                        category="client",
                        itype="pc",
                        name="Hermes",
                    )
            except Exception as exc:
                logger.debug("XMPP: could not set disco identity: %s", exc)

            self.client.register_plugin("xep_0004")  # Data Forms
            self.client.register_plugin("xep_0060")  # PubSub
            self.client.register_plugin("xep_0066")  # Out of Band Data
            self.client.register_plugin("xep_0054")  # vcard-temp
            self.client.register_plugin("xep_0084")  # User Avatar
            self.client.register_plugin("xep_0153")  # vCard-based Avatars
            self.client.register_plugin("xep_0085")  # Chat State Notifications
            self.client.register_plugin("xep_0163")  # PEP
            self.client.register_plugin("xep_0280")  # Message Carbons
            self.client.register_plugin("xep_0333")  # Chat Markers
            self.client.register_plugin("xep_0334")  # Message Processing Hints
            self.client.register_plugin("xep_0363")  # HTTP File Upload
            self.client.register_plugin("xep_0199")  # XMPP Ping

            if self.omemo_enabled:
                from hermes_constants import get_hermes_home

                self.omemo_storage_path = get_hermes_home() / "sessions" / "omemo.json"
                self.client.add_event_handler(
                    "omemo_initialized", self._omemo_initialized
                )
                if not self._configure_omemo():
                    logger.warning(
                        "XMPP: OMEMO requested but could not be enabled; falling back to plaintext"
                    )

            self.client.add_event_handler("session_start", self._session_start)
            self.client.add_event_handler("message", self._on_message)
            self.client.add_event_handler("exception", self._slixmpp_exception_handler)
            self.client.add_event_handler("disconnected", self._on_disconnected)
            self.client.add_event_handler("stream_negotiated", self._on_stream_negotiated)
            self.client.add_event_handler("failed_auth", self._on_failed_auth)

            logger.warning("XMPP: connecting as %s to %s:%s ...", self.user_jid, self.server or "(auto)", self.port)

            # slixmpp connect() returns a Future that completes when the
            # connection *ends*; do not await it. Wait for session_start instead.
            connect_future = self.client.connect(host=self.server or None, port=self.port)
            if connect_future is not None:
                self._xmpp_background_tasks.add(
                    asyncio.create_task(self._watch_client_future(connect_future))
                )

            try:
                await asyncio.wait_for(
                    self._session_started_event.wait(), timeout=30.0
                )
                logger.warning("XMPP: session_start event received and awaited")
            except asyncio.TimeoutError:
                logger.error("XMPP: session_start did not arrive within 30s")
                self._set_fatal_error(
                    "connect_timeout", "session_start timed out", retryable=True
                )
                return False

            self._mark_connected()
            self._last_activity = asyncio.get_event_loop().time()
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

            # Finish slow setup in the background so connect() returns quickly.
            asyncio.create_task(self._finish_setup())
            return True
        except Exception as e:
            logger.error("XMPP: failed to connect as %s — %s", self.user_jid, e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

    async def _watch_client_future(self, future) -> None:
        """Wait for slixmpp's connection future and trigger reconnect if it exits."""
        try:
            await future
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("XMPP: client future ended with error: %s", exc)
        if self.is_connected:
            logger.warning("XMPP: client future ended while still marked connected")
            self._schedule_internal_reconnect("client_future_done", "slixmpp connection future ended")

    async def _finish_setup(self):
        if self.omemo_enabled:
            try:
                await asyncio.wait_for(self._omemo_ready_event.wait(), timeout=30.0)
                logger.info("XMPP: OMEMO ready")
            except asyncio.TimeoutError:
                logger.warning("XMPP: OMEMO did not signal readiness within 30s")
        if self.avatar_path:
            logger.info("XMPP: publishing avatar from %s", self.avatar_path)
            await self._publish_avatar()
            self._schedule_avatar_republish()

    def _schedule_avatar_republish(self) -> None:
        """Schedule a one-time avatar republish in case the first attempt did not propagate."""
        if self._avatar_republish_task and not self._avatar_republish_task.done():
            return

        async def _republish_after_delay() -> None:
            await asyncio.sleep(60.0)
            if self.is_connected and self.avatar_path:
                logger.info("XMPP: republishing avatar from %s", self.avatar_path)
                await self._publish_avatar()

        self._avatar_republish_task = asyncio.create_task(_republish_after_delay())

    async def _keepalive_loop(self) -> None:
        while self.is_connected:
            await asyncio.sleep(self._ping_interval)
            if not self.is_connected or self.client is None:
                break
            try:
                ping = self.client.plugin.get("xep_0199", None)
                if ping is not None:
                    logger.debug("XMPP: sending keepalive ping")
                    await asyncio.wait_for(
                        ping.send_ping(jid=self.client.boundjid.bare),
                        timeout=self._ping_timeout,
                    )
                    self._last_activity = asyncio.get_event_loop().time()
                else:
                    # Fallback: send a whitespace keepalive.
                    self.client.send_raw(" ")
                    self._last_activity = asyncio.get_event_loop().time()
            except Exception as exc:
                logger.warning("XMPP: keepalive ping failed: %s", exc)
                if self.is_connected:
                    self._schedule_internal_reconnect("ping_failed", str(exc))
                break

    def _schedule_internal_reconnect(self, code: str, message: str) -> None:
        """Schedule an internal reconnect attempt before escalating to the gateway.

        This handles transient TCP/XMPP stream drops without requiring the
        gateway-level reconnect watcher to wake up.
        """
        if self._internal_reconnect_task and not self._internal_reconnect_task.done():
            return

        async def _reconnect_attempts() -> None:
            delay = 5.0
            for attempt in range(1, 4):
                if self.is_connected:
                    logger.info("XMPP: connection already restored, aborting internal reconnect")
                    return
                logger.info(
                    "XMPP: internal reconnect attempt %d/3 after %s in %.0fs",
                    attempt, code, delay,
                )
                await asyncio.sleep(delay)
                if self.is_connected:
                    logger.info("XMPP: connection restored while waiting, aborting internal reconnect")
                    return
                try:
                    success = await self.connect(is_reconnect=True)
                    if success:
                        logger.info("XMPP: internal reconnect succeeded on attempt %d", attempt)
                        return
                except Exception as exc:
                    logger.warning("XMPP: internal reconnect attempt %d failed: %s", attempt, exc)
                delay = min(delay * 2, 60.0)
            logger.error(
                "XMPP: internal reconnect exhausted after %s (%s); escalating to gateway retry",
                code, message,
            )
            self._set_fatal_error(code, message, retryable=True)

        self._internal_reconnect_task = asyncio.create_task(_reconnect_attempts())

    async def _on_disconnected(self, event):
        logger.warning("XMPP: disconnected event received; event=%s", event)
        if self.is_connected:
            self._schedule_internal_reconnect("disconnected", "XMPP stream disconnected")

    async def _on_stream_negotiated(self, event):
        logger.info("XMPP: stream_negotiated event received")

    async def _on_failed_auth(self, event):
        logger.error("XMPP: failed_auth event received; event=%s", event)

    async def disconnect(self) -> None:
        await self._cleanup_client()
        self._mark_disconnected()
        await self._http.aclose()

    async def _cleanup_client(self) -> None:
        """Cancel background tasks and disconnect the current slixmpp client."""
        for task_name in ("_keepalive_task", "_avatar_republish_task", "_internal_reconnect_task"):
            task = getattr(self, task_name, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Cancel any slixmpp connection-future watchers.
        for task in list(self._xmpp_background_tasks):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._xmpp_background_tasks.discard(task)
        if self.client:
            old_client = self.client
            self.client = None
            try:
                await old_client.disconnect()
            except Exception:
                pass

    # -- Sending ---------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        logger.warning("XMPP: send() called chat_id=%s content=%r", chat_id, content[:80])
        if self.client is None:
            logger.error("XMPP: cannot send, client not connected")
            return SendResult(success=False, error="not connected")

        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        # Reply to the exact resource we last saw from this bare JID, if known.
        # This matches how real XMPP clients (Dino, Conversations) route replies.
        cached_resource = self._last_resources.get(str(recipient.bare))
        if cached_resource:
            try:
                recipient = JID(cached_resource)
                logger.warning("XMPP: send() using cached resource %s", cached_resource)
            except Exception as exc:
                logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        text = content
        chat_id_str = str(recipient)
        if chat_id_str in self._voice_reply_chats:
            self._voice_reply_chats.discard(chat_id_str)
            try:
                return await self._send_voice_reply_text(recipient, text)
            except Exception as exc:
                logger.warning("XMPP: TTS voice reply failed (%s); sending text only", exc)
        return await self._send_text(recipient, text)

    async def _send_voice_reply_text(self, recipient: JID, text: str) -> SendResult:
        """Generate TTS audio for the first chunk of text and send as a voice message.

        Falls back to plain text if TTS generation fails.
        """
        from tools.tts_tool import check_tts_requirements, text_to_speech_tool

        if not check_tts_requirements():
            logger.warning("XMPP: TTS requirements not met; sending text only")
            return await self._send_text(recipient, text)

        # Only TTS the first chunk; XMPP voice messages are short.
        tts_text = self.prepare_tts_text(text[:4000])
        if not tts_text:
            return await self._send_text(recipient, text)

        import json as _json
        tts_result_str = await asyncio.to_thread(text_to_speech_tool, text=tts_text)
        tts_data = _json.loads(tts_result_str)
        audio_path = tts_data.get("file_path")

        if audio_path and Path(audio_path).exists():
            logger.info("XMPP: sending TTS voice reply to %s", recipient.bare)
            voice_result = await self.send_voice(str(recipient.bare), audio_path)
            try:
                os.remove(audio_path)
            except OSError:
                pass
            if voice_result.success:
                # Also send the full text response as a follow-up message.
                await self._send_text(recipient, text)
                return voice_result

        logger.warning("XMPP: TTS audio generation failed or empty; sending text only")
        return await self._send_text(recipient, text)

    async def _send_text(self, recipient: JID, text: str) -> SendResult:
        logger.warning("XMPP: _send_text() called for %s: %d chars omemo_chats=%s", recipient.bare, len(text), self._omemo_chats)
        # XMPP servers and clients often choke on very large stanzas.
        # Split response into smaller, manageable chunks.
        chunk_size = 2000
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

        recipient_bare = str(recipient.bare)
        force_omemo = self.omemo_enabled and recipient_bare in self._omemo_chats

        try:
            for i, chunk in enumerate(chunks):
                msg = self.client.make_message(mto=recipient, mtype="chat")
                msg["body"] = chunk
                msg["id"] = self.client.new_id()
                msg["chat_state"] = "active"

                omemo = self._omemo_plugin()
                if omemo is not None and self.omemo_enabled:
                    try:
                        # Match slixmpp-omemo echo client: set explicit to/from on the stanza
                        msg.set_to(recipient)
                        msg.set_from(self.client.boundjid)
                        encrypted, _errors = await omemo.encrypt_message(
                            msg,
                            recipient_jids={recipient},
                            identifier=str(recipient.bare),
                        )
                        logger.warning("XMPP: encrypt_message returned encrypted=%s errors=%s", encrypted is not None, _errors)
                        if encrypted is not None:
                            # `encrypted` is the original Message stanza with its payload
                            # replaced by the OMEMO <encrypted/> element. Try to tag it
                            # with XEP-0380 EME; if that fails, just send it.
                            try:
                                if hasattr(encrypted, "xml"):
                                    ns_eme = "urn:xmpp:eme:0"
                                    eme_el = ET.Element("{" + ns_eme + "}encryption")
                                    eme_el.set("namespace", "eu.siacs.conversations.axolotl")
                                    eme_el.set("name", "OMEMO")
                                    encrypted.xml.append(eme_el)
                                else:
                                    encrypted["eme"]["namespace"] = "eu.siacs.conversations.axolotl"
                                    encrypted["eme"]["name"] = "OMEMO"
                            except Exception as eme_exc:
                                logger.debug("XMPP: failed to set EME namespace: %s", eme_exc)
                            encrypted.send()
                            logger.warning("XMPP: OMEMO message chunk %d/%d sent to %s", i+1, len(chunks), recipient)
                            if i < len(chunks) - 1:
                                await asyncio.sleep(0.2)
                            continue
                        elif force_omemo:
                            logger.error("XMPP: OMEMO encryption required for %s but failed; not falling back to plaintext", recipient)
                            return SendResult(success=False, error="OMEMO encryption required but failed")
                    except Exception as exc:
                        if force_omemo:
                            logger.error("XMPP: OMEMO encryption required for %s but failed: %s", recipient, exc)
                            return SendResult(success=False, error=f"OMEMO encryption required but failed: {exc}")
                        logger.warning("XMPP: OMEMO send failed (%s); falling back to plaintext", exc)

                if force_omemo:
                    # Should have been handled inside the omemo block above.
                    logger.error("XMPP: OMEMO encryption required for %s but no encrypted stanza produced", recipient)
                    return SendResult(success=False, error="OMEMO encryption required but no encrypted stanza produced")

                logger.warning("XMPP: sending plaintext chunk %d/%d to %s", i+1, len(chunks), recipient)
                msg.send()
                logger.warning("XMPP: plaintext message chunk %d/%d sent to %s", i+1, len(chunks), recipient)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.2)

            return SendResult(success=True)
        except Exception as exc:
            logger.exception("XMPP: failed to send message to %s: %s", recipient.bare, exc)
            return SendResult(success=False, error=str(exc))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as a voice/audio message over XMPP.

        Used by the Hermes core auto-TTS path. The file at ``audio_path`` is
        uploaded and delivered with OMEMO/media-sharing metadata when possible.
        """
        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        audio_path_obj = Path(audio_path)
        if not audio_path_obj.exists():
            return SendResult(success=False, error=f"audio file not found: {audio_path}")

        audio_bytes = audio_path_obj.read_bytes()
        ext = audio_path_obj.suffix.lower() or ".m4a"
        content_type = _mime_from_extension(ext)
        filename = f"voice_{uuid.uuid4().hex}{ext}"

        url = await self._upload_encrypted_media(audio_bytes, filename, content_type)
        if not url:
            url = await self._upload_file(audio_bytes, filename, content_type)
        if not url:
            return SendResult(success=False, error="HTTP file upload failed")

        msg = self.client.make_message(mto=recipient, mtype="chat")
        msg["body"] = caption if caption else url
        msg["id"] = self.client.new_id()

        if url.startswith("aesgcm://"):
            try:
                ns_sshare = "urn:xmpp:sfs:0"
                ns_share = "urn:xmpp:share:1"
                ns_oob = "jabber:x:oob"

                sfs = ET.Element("{" + ns_sshare + "}file-sharing")
                file_el = ET.SubElement(sfs, "{" + ns_share + "}file")
                ET.SubElement(file_el, "{" + ns_share + "}name").text = filename
                ET.SubElement(file_el, "{" + ns_share + "}media-type").text = content_type
                ET.SubElement(file_el, "{" + ns_share + "}size").text = str(len(audio_bytes))

                sources = ET.SubElement(sfs, "{" + ns_sshare + "}sources")
                ref = ET.SubElement(sources, "{" + ns_sshare + "}reference")
                ref.set("type", "http")
                ref.set("url", url)

                data_el = ET.SubElement(sfs, "{" + ns_oob + "}data")
                data_el.set("url", url)

                msg.xml.append(sfs)
            except Exception as exc:
                logger.debug("XMPP: could not attach media-sharing metadata: %s", exc)

        omemo = self._omemo_plugin()
        if omemo is not None and self.omemo_enabled:
            try:
                encrypted, _errors = await omemo.encrypt_message(
                    msg,
                    recipient_jids={recipient},
                    identifier=str(recipient),
                )
                if encrypted:
                    encrypted.send()
                    logger.info("XMPP: OMEMO voice message sent to %s", recipient.bare)
                    return SendResult(success=True)
            except Exception as exc:
                logger.warning("XMPP: OMEMO voice send failed (%s); falling back", exc)

        msg.send()
        logger.info("XMPP: voice message sent to %s", recipient.bare)
        return SendResult(success=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image file over XMPP using HTTP File Upload.

        Reuses the same encrypted upload and OMEMO media-sharing metadata as
        send_voice, but sends an image file suitable for inline display.
        """
        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        image_path_obj = Path(image_path)
        if not image_path_obj.exists():
            return SendResult(success=False, error=f"image file not found: {image_path}")

        image_bytes = image_path_obj.read_bytes()
        ext = image_path_obj.suffix.lower() or ".png"
        content_type = _mime_from_extension(ext)
        filename = f"image_{uuid.uuid4().hex}{ext}"

        url = await self._upload_encrypted_media(image_bytes, filename, content_type)
        if not url:
            url = await self._upload_file(image_bytes, filename, content_type)
        if not url:
            return SendResult(success=False, error="HTTP file upload failed")

        msg = self.client.make_message(mto=recipient, mtype="chat")
        msg["body"] = caption if caption else url
        msg["id"] = self.client.new_id()

        if url.startswith("aesgcm://"):
            try:
                ns_sshare = "urn:xmpp:sfs:0"
                ns_share = "urn:xmpp:share:1"
                ns_oob = "jabber:x:oob"

                sfs = ET.Element("{" + ns_sshare + "}file-sharing")
                file_el = ET.SubElement(sfs, "{" + ns_share + "}file")
                ET.SubElement(file_el, "{" + ns_share + "}name").text = filename
                ET.SubElement(file_el, "{" + ns_share + "}media-type").text = content_type
                ET.SubElement(file_el, "{" + ns_share + "}size").text = str(len(image_bytes))

                sources = ET.SubElement(sfs, "{" + ns_sshare + "}sources")
                ref = ET.SubElement(sources, "{" + ns_sshare + "}reference")
                ref.set("type", "http")
                ref.set("url", url)

                data_el = ET.SubElement(sfs, "{" + ns_oob + "}data")
                data_el.set("url", url)

                msg.xml.append(sfs)
            except Exception as exc:
                logger.debug("XMPP: could not attach media-sharing metadata: %s", exc)

        omemo = self._omemo_plugin()
        if omemo is not None and self.omemo_enabled:
            try:
                encrypted, _errors = await omemo.encrypt_message(
                    msg,
                    recipient_jids={recipient},
                    identifier=str(recipient),
                )
                if encrypted:
                    encrypted.send()
                    logger.info("XMPP: OMEMO image sent to %s", recipient.bare)
                    return SendResult(success=True)
            except Exception as exc:
                logger.warning("XMPP: OMEMO image send failed (%s); falling back", exc)

        msg.send()
        logger.info("XMPP: image sent to %s", recipient.bare)
        return SendResult(success=True)



    async def _upload_encrypted_media(self, plaintext: bytes, filename: str, content_type: str) -> Optional[str]:
        """Encrypt plaintext with AES-256-GCM and upload via HTTP File Upload.

        Returns an aesgcm:// URL with the IV+key in the fragment, suitable for
        OMEMO media sharing / Conversations inline playback.
        """
        try:
            upload = self.client.plugin.get("xep_0363", None)
            if upload is None:
                return None

            key = AESGCM.generate_key(bit_length=256)
            iv = os.urandom(12)
            aesgcm = AESGCM(key)
            ciphertext = aesgcm.encrypt(iv, plaintext, None)

            enc_filename = f"{Path(filename).stem}.aesgcm{Path(filename).suffix}"
            get_url = await upload.upload_file(
                filename=enc_filename,
                size=len(ciphertext),
                content_type="application/octet-stream",
                input_file=io.BytesIO(ciphertext),
                domain=JID(self.client.boundjid.bare).domain,
                timeout=60.0,
            )
            if not get_url:
                return None

            # Convert https://upload... to aesgcm://upload... and append IV+key
            fragment = (iv + key).hex()
            aesgcm_url = get_url.replace("https://", "aesgcm://", 1) + "#" + fragment
            return aesgcm_url
        except Exception as exc:
            logger.warning("XMPP: encrypted media upload failed: %s", exc)
            return None

    async def _upload_file(self, data: bytes, filename: str, content_type: str) -> Optional[str]:
        try:
            upload = self.client.plugin.get("xep_0363", None)
            if upload is None:
                logger.warning("XMPP: xep_0363 plugin not available")
                return None

            # Use the helper that handles service discovery and upload.
            get_url = await upload.upload_file(
                filename=filename,
                size=len(data),
                content_type=content_type,
                input_file=io.BytesIO(data),
                domain=JID(self.client.boundjid.bare).domain,
                timeout=60.0,
            )
            return get_url
        except Exception as exc:
            logger.warning("XMPP: file upload failed: %s", exc)
            return None

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if not self.typing_indicator or self.client is None:
            logger.debug("XMPP: send_typing skipped (disabled or no client)")
            return
        try:
            # Use the last known full resource for this chat, otherwise bare JID.
            recipient_str = self._last_resources.get(chat_id, chat_id)
            recipient = JID(recipient_str)
            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["chat_state"] = "composing"
            msg.send()
            logger.warning("XMPP: typing indicator sent to %s", recipient)
        except Exception as exc:
            logger.warning("XMPP: typing indicator send failed: %s", exc)

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        if not self.typing_indicator or self.client is None:
            logger.debug("XMPP: stop_typing skipped (disabled or no client)")
            return
        try:
            recipient_str = self._last_resources.get(chat_id, chat_id)
            recipient = JID(recipient_str)
            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["chat_state"] = "active"
            msg.send()
            logger.warning("XMPP: stop typing sent to %s", recipient)
        except Exception as exc:
            logger.warning("XMPP: stop typing send failed: %s", exc)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # -- Media ---------------------------------------------------------------

    async def _download_url(self, url: str) -> Optional[bytes]:
        try:
            if url.startswith("aesgcm://"):
                return await self._download_aesgcm(url)
            resp = await self._http.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            logger.warning("XMPP: failed to download %s: %s", url, exc)
            return None

    async def _download_aesgcm(self, url: str) -> Optional[bytes]:
        try:
            parsed = urlparse(url)
            fragment = parsed.fragment
            if not fragment or len(fragment) != 88:
                logger.warning("XMPP: invalid aesgcm fragment length")
                return None
            iv = bytes.fromhex(fragment[:24])
            key = bytes.fromhex(fragment[24:])
            https_url = f"https://{parsed.netloc}{parsed.path}"
            resp = await self._http.get(https_url)
            resp.raise_for_status()
            ciphertext = resp.content
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(iv, ciphertext, None)
            return plaintext
        except Exception as exc:
            logger.warning("XMPP: failed to decrypt aesgcm %s: %s", url, exc)
            return None

    def _extract_url(self, text: str) -> Optional[str]:
        match = re.search(r"https?://\S+|aesgcm://\S+", text)
        return match.group(0) if match else None

    # -- Avatar --------------------------------------------------------------

    async def _publish_avatar(self) -> None:
        if not self.avatar_path or self.client is None:
            return
        try:
            path = Path(self.avatar_path)
            if not path.exists():
                logger.warning("XMPP: avatar path does not exist: %s", self.avatar_path)
                return

            img = Image.open(path)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # Avatars should be square. Crop to center square and resize.
            width, height = img.size
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((480, 480), Image.LANCZOS)

            png_buffer = io.BytesIO()
            img.save(png_buffer, format="PNG", optimize=True)
            data = png_buffer.getvalue()

            # Always publish vCard avatar (XEP-0153); most clients use this.
            vcard_avatar = self.client.plugin.get("xep_0153", None)
            if vcard_avatar is not None:
                try:
                    await asyncio.wait_for(
                        vcard_avatar.set_avatar(avatar=data, mtype="image/png"),
                        timeout=15.0,
                    )
                    logger.info("XMPP: published vCard avatar (%d bytes, %dx%d)", len(data), img.width, img.height)
                except Exception as exc:
                    logger.warning("XMPP: vCard avatar publish failed: %s", exc)
            else:
                logger.warning("XMPP: xep_0153 plugin not available")

            # Try PEP/XEP-0084 second (best effort, can hang on some servers).
            pep_avatar = self.client.plugin.get("xep_0084", None)
            if pep_avatar is not None:
                try:
                    avatar_id = pep_avatar.generate_id(data)
                    await asyncio.wait_for(pep_avatar.publish_avatar(data), timeout=5.0)
                    await asyncio.wait_for(pep_avatar.publish_avatar_metadata({
                        "id": avatar_id,
                        "type": "image/png",
                        "bytes": len(data),
                        "width": img.width,
                        "height": img.height,
                    }), timeout=5.0)
                    logger.info("XMPP: published PEP avatar id=%s (%d bytes, %dx%d)", avatar_id[:16], len(data), img.width, img.height)
                except Exception as exc:
                    logger.warning("XMPP: PEP avatar publish failed: %s", exc)
            else:
                logger.debug("XMPP: xep_0084 plugin not available")
        except Exception as exc:
            logger.warning("XMPP: failed to publish avatar: %s", exc, exc_info=True)

    # -- Receiving -----------------------------------------------------------

    async def _session_start(self, event):
        logger.warning("XMPP: session_start handler fired for %s", self.user_jid)
        try:
            if self.client:
                self.client.send_presence()
                self.client.get_roster()
        except Exception:
            pass
        try:
            def _log_sent_xml(stanza):
                logger.warning("XMPP: SENT XML: %s", stanza)
            def _log_recv_xml(stanza):
                logger.warning("XMPP: RECV XML: %s", stanza)
            self.client.add_event_handler("raw_send", _log_sent_xml)
            self.client.add_event_handler("raw_recv", _log_recv_xml)
            logger.warning("XMPP: raw XML logging enabled")
        except Exception as exc:
            logger.warning("XMPP: failed to enable raw XML logging: %s", exc)
        self._session_started_event.set()

    async def _omemo_initialized(self, event=None):
        logger.info("XMPP: OMEMO initialized and device list published")
        self._omemo_ready_event.set()

    async def _on_message(self, msg: Message):
        try:
            if msg["type"] not in ("chat", "normal"):
                return

            logger.warning("XMPP: _on_message fired type=%s from=%s", msg.get("type", ""), msg["from"])
            sender_jid = msg["from"]
            if not sender_jid:
                logger.warning("XMPP: _on_message returning — no sender_jid")
                return
            sender_full = JID(sender_jid)
            sender_bare = str(sender_full.bare)
            self._last_resources[sender_bare] = str(sender_full)
            if sender_bare == JID(self.user_jid).bare:
                logger.warning("XMPP: _on_message returning — self-message from %s", sender_bare)
                return

            body = msg.get("body", "").strip()
            logger.warning("XMPP: _on_message body=%r has_encrypted check next", body)
            encrypted = False

            # Only attempt OMEMO decryption if the stanza actually contains an
            # OMEMO <encrypted> payload. If OMEMO is enabled but the message is
            # plaintext, slixmpp-omemo raises "No supported encrypted content";
            # in that case we keep the plaintext body.
            omemo = self._omemo_plugin()
            has_encrypted = (
                msg.xml.find(".//{eu.siacs.conversations.axolotl}encrypted") is not None
                or msg.xml.find(".//{urn:xmpp:omemo:2}encrypted") is not None
            )
            logger.warning("XMPP: _on_message has_encrypted=%s omemo=%s omemo_enabled=%s", has_encrypted, omemo is not None, self.omemo_enabled)
            if omemo is not None and self.omemo_enabled and has_encrypted:
                try:
                    decrypted, _device_info = await omemo.decrypt_message(msg)
                    body_text = str(decrypted.get("body", "") or "").strip()
                    if body_text and body_text != body:
                        body = body_text
                        encrypted = True
                        logger.warning("XMPP: OMEMO decrypted message from %s: %s chars", sender_bare, len(body))
                except Exception as exc:
                    logger.warning("XMPP: OMEMO decrypt attempt failed: %s", exc, exc_info=True)
                    # Fall back to plaintext body if decryption fails.

            # Remember that this chat is OMEMO-active so all replies are encrypted.
            if encrypted and sender_bare not in self._omemo_chats:
                self._omemo_chats.add(sender_bare)
                logger.warning("XMPP: chat %s added to OMEMO-active set", sender_bare)
            elif not encrypted and sender_bare in self._omemo_chats:
                # If the contact downgrades to plaintext, stop forcing OMEMO.
                self._omemo_chats.discard(sender_bare)
                logger.warning("XMPP: chat %s removed from OMEMO-active set (plaintext received)", sender_bare)

            if not body:
                logger.warning("XMPP: _on_message returning — empty body after decrypt attempt")
                return

            # Send read receipt for messages that request it.
            try:
                markable = msg.xml.find(".//{urn:xmpp:chat-markers:0}markable") is not None
                logger.warning("XMPP: _on_message markable=%s", markable)
                if markable:
                    await self._send_displayed_marker(sender_bare, msg.get("id", self.client.new_id()))
            except Exception as marker_exc:
                logger.warning("XMPP: failed to send displayed marker: %s", marker_exc)

            url: Optional[str] = None
            try:
                oob = msg.xml.find(".//{jabber:x:oob}x")
                if oob is not None:
                    url_el = oob.find("{jabber:x:oob}url")
                    if url_el is not None and url_el.text:
                        url = url_el.text.strip()
            except Exception:
                pass
            if not url:
                url = self._extract_url(body)

            media_path: Optional[str] = None
            msg_type = MessageType.TEXT
            original_msg_type = MessageType.TEXT
            if url:
                logger.debug("XMPP: detected URL in message: %s", url)
                if url.startswith("aesgcm://"):
                    data = await self._download_aesgcm(url)
                else:
                    data = await self._download_url(url)
                logger.debug("XMPP: downloaded %d bytes from %s", len(data) if data else 0, url)
                if data:
                    content_type = _guess_content_type(data)
                    if content_type.startswith("image/"):
                        msg_type = MessageType.PHOTO
                        ext = _guess_extension_from_data(data)
                        media_path = self._cache_media(data, "image", ext=ext)
                    elif content_type.startswith("audio/"):
                        if _guess_audio_is_voice(url, body):
                            msg_type = MessageType.VOICE
                        else:
                            msg_type = MessageType.AUDIO
                        ext = _guess_audio_extension(url, data)
                        media_path = self._cache_media(data, "audio", ext=ext)
                    elif _guess_audio_is_voice(url, body):
                        msg_type = MessageType.VOICE
                        ext = _guess_audio_extension(url, data)
                        media_path = self._cache_media(data, "audio", ext=ext)
                    elif _is_audio_url(url):
                        msg_type = MessageType.AUDIO
                        ext = _guess_audio_extension(url, data)
                        media_path = self._cache_media(data, "audio", ext=ext)
                    else:
                        msg_type = MessageType.PHOTO
                        media_path = self._cache_media(data, "image")
                else:
                    logger.warning("XMPP: failed to download media from %s", url)
                    msg_type = MessageType.TEXT

                # Remember that this was a voice message before we convert it to TEXT
                # after transcription, so the adapter can still queue a TTS reply.
                original_msg_type = msg_type
                logger.debug("XMPP: cached media path=%s", media_path)

            # If media was cached, replace the URL in the body with the local
            # path so downstream tools analyse the actual file, not the link.
            display_text = body
            media_urls: list[str] = []
            media_types: list[str] = []
            if media_path and url:
                if msg_type == MessageType.VOICE:
                    # Voice messages should reach the LLM as plain text, not as a
                    # file attachment. Transcribe locally via Hermes core STT and
                    # replace the message content with the transcript.
                    stripped = body.replace(url, "").strip()
                    try:
                        result = transcribe_audio(media_path)
                        if result.get("success"):
                            display_text = result.get("transcript", "(voice message)").strip() or "(voice message)"
                            logger.info(
                                "XMPP: transcribed voice message: %r",
                                display_text,
                            )
                            # Convert to TEXT after successful transcription so the
                            # gateway core doesn't also generate an auto-TTS reply.
                            # The adapter-level _voice_reply_chats queue handles the
                            # single outbound voice reply below.
                            msg_type = MessageType.TEXT
                        else:
                            error = result.get("error", "unknown error")
                            logger.warning("XMPP: voice transcription failed: %s", error)
                            display_text = stripped or "(voice message could not be transcribed)"
                            msg_type = MessageType.TEXT
                    except Exception as exc:
                        logger.warning("XMPP: voice transcription error: %s", exc)
                        display_text = stripped or "(voice message could not be transcribed)"
                        msg_type = MessageType.TEXT
                else:
                    display_text = body.replace(url, media_path)
                    if display_text == body:
                        # URL not in body (e.g. only in oob); use a direct note.
                        display_text = f"{body}\n[Attached media: {media_path}]".strip()
                    media_urls = [media_path]
                    media_types = [content_type]

            source = self.build_source(
                chat_id=sender_bare,
                chat_name=sender_bare,
                chat_type="dm",
                user_id=sender_bare,
                user_name=sender_bare,
                thread_id=None,
            )

            event = MessageEvent(
                text=display_text,
                message_type=msg_type,
                source=source,
                raw_message=msg,
                media_urls=media_urls,
                media_types=media_types,
                metadata={"encrypted": encrypted, "media_url": url, "media_path": media_path},
            )

            logger.warning("XMPP: about to handle_message event text=%r type=%s", display_text, msg_type)

            # If the global voice.auto_tts default is on, opt this DM chat into
            # auto-TTS replies. Without this, _should_send_voice_reply stays off
            # because XMPP has no /voice command UI to set per-chat voice mode.
            auto_tts_default = getattr(self, "_auto_tts_default", False)
            if auto_tts_default and original_msg_type == MessageType.VOICE:
                self._voice_reply_chats.add(sender_bare)
                logger.info(
                    "XMPP: queued voice reply for chat %s (auto_tts_default=%s)",
                    sender_bare,
                    auto_tts_default,
                )

            await self.handle_message(event)
            logger.warning("XMPP: handle_message completed")
        except Exception:
            logger.exception("XMPP: unhandled error in message handler")

    async def _send_displayed_marker(self, to_jid: str, message_id: str) -> None:
        try:
            marker_plugin = self.client.plugin.get("xep_0333", None)
            if marker_plugin is None:
                logger.debug("XMPP: xep_0333 plugin not available")
                return

            # Determine actual recipient. Prefer the last known full resource for
            # this bare JID; otherwise use the bare JID.
            recipient_str = self._last_resources.get(to_jid, to_jid)
            recipient = JID(recipient_str)

            # XEP-0333 markers are standalone chat markers and should not be
            # encrypted as message bodies. Send them in plaintext so the recipient
            # client displays the proper read receipt (second checkmark) instead of
            # treating the marker as a regular message.
            marker_plugin.send_marker(mto=recipient, id=message_id, marker="displayed", mtype="chat")
            logger.warning("XMPP: sent displayed marker to %s for %s", recipient, message_id)
        except Exception as exc:
            logger.debug("XMPP: failed to send displayed marker: %s", exc)

    def _cache_media(self, data: bytes, kind: str = "image", ext: Optional[str] = None) -> Optional[str]:
        try:
            if ext is None:
                ext = ".ogg" if kind == "audio" else ".png"
            elif not ext:
                ext = ".ogg" if kind == "audio" else ".png"
            mime = "audio/mpeg" if kind == "audio" else "image/png"
            validate_inbound_media_size(len(data), media_type=kind)
            # Use a unique filename to prevent cache collisions
            filename = f"xmpp_{uuid.uuid4().hex}{ext}"
            cached = cache_media_bytes(data, filename=filename, mime_type=mime, default_kind=kind)
            return str(cached.path) if cached and getattr(cached, "path", None) else None
        except Exception as exc:
            logger.warning("XMPP: failed to cache media: %s", exc)
            return None

    # -- Helpers -------------------------------------------------------------

    async def _slixmpp_exception_handler(self, exc):
        logger.exception("XMPP: slixmpp internal exception: %s", exc)


def check_requirements() -> bool:
    try:
        import slixmpp  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (os.getenv("XMPP_USER_JID") or extra.get("user_jid"))
        and (os.getenv("XMPP_PASSWORD") or extra.get("password"))
    )


def is_connected(config) -> bool:
    return validate_config(config)


def interactive_setup() -> None:
    import builtins
    jid = builtins.input("XMPP JID (e.g. hermes@example.com): ").strip()
    password = builtins.input("XMPP password: ").strip()
    if jid and password:
        print(f"Set XMPP_USER_JID={jid} and XMPP_PASSWORD=*** in ~/.hermes/.env")


def _env_enablement() -> Optional[dict]:
    user_jid = os.getenv("XMPP_USER_JID", "").strip()
    password = os.getenv("XMPP_PASSWORD", "").strip()
    server = os.getenv("XMPP_SERVER", "").strip()
    if not user_jid or not password:
        return None
    extra: dict = {"user_jid": user_jid, "password": password}
    if server:
        extra["server"] = server
    return extra


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
) -> dict:
    extra = getattr(pconfig, "extra", {}) or {}
    user_jid = os.getenv("XMPP_USER_JID") or extra.get("user_jid", "")
    password = os.getenv("XMPP_PASSWORD") or extra.get("password", "")
    server = os.getenv("XMPP_SERVER") or extra.get("server", "")
    if not user_jid or not password:
        return {"error": "XMPP_USER_JID and XMPP_PASSWORD must be configured"}

    client = None
    try:
        client = ClientXMPP(user_jid, password)
        client.use_message_ids = True
        session_started = asyncio.Event()
        client.add_event_handler("session_start", lambda _: session_started.set())
        client.connect(host=server or None, port=5222)
        await asyncio.wait_for(session_started.wait(), timeout=30.0)
        msg = client.make_message(mto=JID(chat_id), mtype="chat")
        msg["id"] = client.new_id()
        msg["body"] = message
        msg.send()
        await client.disconnect(wait=True)
        return {"success": True}
    except Exception as e:
        return {"error": f"XMPP standalone send failed: {e}"}
    finally:
        if client:
            try:
                await client.disconnect(wait=True)
            except Exception:
                pass


_XMPP_YAML_KEYS = (
    "user_jid",
    "password",
    "server",
    "port",
    "omemo_enabled",
    "omemo_allow_untrusted",
    "avatar_path",
    "home_channel",
    "allowed_users",
    "allow_all_users",
)


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    seeded = {k: platform_cfg[k] for k in _XMPP_YAML_KEYS if k in platform_cfg}
    return seeded if seeded else None


def register(ctx):
    ctx.register_platform(
        name="xmpp",
        label="XMPP",
        adapter_factory=lambda cfg: XMPPAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["XMPP_USER_JID", "XMPP_PASSWORD"],
        install_hint="pip install slixmpp slixmpp-omemo httpx Pillow cryptography",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="XMPP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="XMPP_ALLOWED_USERS",
        allow_all_env="XMPP_ALLOW_ALL_USERS",
        max_message_length=4096,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via XMPP. Use plain text responses. "
            "XMPP does not render markdown reliably."
        ),
    )
