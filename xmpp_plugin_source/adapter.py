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
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext.lower(), "application/octet-stream")


def _is_audio_url(url: str) -> bool:
    # Strip query string and fragment so URLs like
    # https://example.com/audio.mp3?token=abc are still recognized as audio.
    try:
        path = urlparse(url).path
    except Exception:
        path = url
    return any(path.lower().endswith(ext) for ext in (
        ".ogg", ".oga", ".mp3", ".m4a", ".webm", ".wav", ".opus"
    ))


def _guess_audio_is_voice(url: str, body: str, data: Optional[bytes] = None) -> bool:
    """Heuristic to decide if an incoming audio URL is a voice message.

    A URL must NOT be treated as voice just because it is short or OMEMO-
    encrypted. OMEMO uses aesgcm:// for images, files, and voice alike, and
    a bare HTTPS URL is a text link unless it actually points to audio.

    The caller only invokes this after confirming the downloaded content is
    audio, so the final branch treats a bare audio URL with no caption as a
    voice message regardless of container (m4a, mp3, wav, ogg, etc.). That is
    intentional: a standalone audio attachment with no accompanying text is the
    signature of a voice message, while shared music/podcast files almost
    always carry a filename or description and stay AUDIO.
    """
    lowered = url.lower()
    # Conversations and similar clients often use voice-message-* filenames.
    if "voice-message" in lowered:
        return True
    # Voice messages are usually short, unadorned clips in these containers.
    if any(lowered.endswith(ext) for ext in (".ogg", ".oga", ".opus", ".webm")):
        return True
    # A bare audio URL with no caption is the signature of a voice message, so
    # treat any audio content as voice here (the caller has already confirmed
    # the content is audio). Shared music/podcast files carry a filename or
    # description, so they do not reach this branch and stay AUDIO.
    stripped = body.strip()
    if (stripped == url or not stripped) and data is not None:
        return _guess_content_type(data).startswith("audio/")
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

def _is_media_url(url: str) -> bool:
    """Return True if the URL path has a known media file extension.

    Query strings and fragments are stripped before the extension check so
    URLs like https://example.com/photo.jpg?size=large or
    https://example.com/audio.mp3#frag are still recognized as media.
    """
    try:
        path = urlparse(url).path
    except Exception:
        path = url
    lowered = path.lower()
    return any(lowered.endswith(ext) for ext in (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
        ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".webm",
        ".mp4", ".mov", ".mkv", ".avi",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z",
    ))


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

        self.typing_indicator = _parse_bool(
            os.getenv("XMPP_TYPING_INDICATOR") or extra.get("typing_indicator"), True
        )
        # XEP-0085 typing indicators are always enabled; do not allow config
        # to disable them. This also prevents an accidental null/false value
        # in config.yaml from breaking the composing indicator.
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
        # can reply with TTS audio at the end of the agent turn, not on the first
        # intermediate message. The simple "next non-tool message wins" approach
        # caused short acknowledgments to steal the voice reply before the real
        # response was ready.
        self._voice_reply_chats: Dict[str, Dict[str, Any]] = {}
        # Per-chat debounce tasks. A single global task would let a second
        # chat's voice message cancel the first chat's pending reply (leaving
        # the first entry stuck in _voice_reply_chats forever), so each chat
        # owns its own debounce timer.
        self._voice_reply_debounce_tasks: Dict[str, asyncio.Task] = {}
        self._voice_reply_debounce_delay: float = 2.0
        self._last_resources: Dict[str, str] = {}
        # Track bare JIDs that have sent us OMEMO-encrypted messages so replies
        # to those chats are always encrypted rather than falling back to plaintext.
        self._omemo_chats: set[str] = set()
        # Anti-race typing state: stop_typing sets a cooldown so that stray
        # refresh ticks from the base adapter's _keep_typing loop (which may
        # outlive the adapter's own stop_typing call after /new or media sends)
        # cannot resurrect the composing indicator.
        self._typing_state: Dict[str, str] = {}
        self._typing_stop_until: Dict[str, float] = {}
        # Per-chat background refresh tasks owned by the XMPP adapter.  The base
        # adapter's _keep_typing loop can get orphaned across /new because the
        # session guard is swapped and the old task's stop_event is no longer the
        # one in _active_sessions.  By tracking our own task we can kill it
        # deterministically from send()/stop_typing() without relying on the base
        # adapter to cancel it for us.
        self._typing_refresh_tasks: Dict[str, asyncio.Task] = {}

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
            self.client.add_event_handler("presence_subscribe", self._on_presence_subscribe)
            self.client.add_event_handler("exception", self._slixmpp_exception_handler)
            self.client.add_event_handler("disconnected", self._on_disconnected)
            self.client.add_event_handler("stream_negotiated", self._on_stream_negotiated)
            self.client.add_event_handler("failed_auth", self._on_failed_auth)

            logger.info("XMPP: connecting as %s to %s:%s ...", self.user_jid, self.server or "(auto)", self.port)

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
                logger.info("XMPP: session_start event received and awaited")
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
            logger.error("XMPP: failed to connect as %s - %s", self.user_jid, e)
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
            # Prune stale OMEMO sessions/devices so the store does not balloon
            # and desynced ratchets do not accumulate.
            try:
                omemo = self._omemo_plugin()
                if omemo is not None and hasattr(omemo, "prune_stale_sessions"):
                    removed = await omemo.prune_stale_sessions()
                    if removed:
                        logger.info("XMPP: pruned %d stale OMEMO session/device keys", removed)
            except Exception as exc:
                logger.warning("XMPP: OMEMO session pruning failed: %s", exc)
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
                # A single ping timeout is NOT fatal: on a slow link a ping can
                # exceed the timeout while the stream is still healthy. Fall back
                # to a whitespace keepalive and keep the loop alive. Only a real
                # stream drop (the slixmpp "disconnected" event) should trigger
                # a reconnect.
                logger.warning("XMPP: keepalive ping failed (%s); using whitespace keepalive", exc)
                try:
                    if self.client is not None:
                        self.client.send_raw(" ")
                        self._last_activity = asyncio.get_event_loop().time()
                except Exception:
                    pass

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
        self._mark_disconnected()
        # Do not schedule reconnect if we are already trying, or if the
        # disconnect was caused by a deliberate shutdown/cleanup.
        if self.client is None:
            return
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
        # Cancel per-chat typing refresh loops and pending voice-reply debounce
        # timers so they do not outlive the client on a reconnect/shutdown.
        for task in list(self._typing_refresh_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._typing_refresh_tasks.clear()
        for task in list(self._voice_reply_debounce_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._voice_reply_debounce_tasks.clear()
        self._voice_reply_chats.clear()
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
        logger.debug("XMPP: send() called chat_id=%s content=%r", chat_id, content[:80])
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
        # EXCEPTION: for OMEMO-active chats we send to the BARE JID so
        # slixmpp-omemo encrypts for every published device and all the user's
        # clients receive the reply. Sending to a single cached resource can
        # deliver only to the one device that last messaged the bot.
        recipient_bare = str(recipient.bare)
        if recipient_bare in self._omemo_chats:
            recipient = JID(recipient_bare)
            logger.debug("XMPP: send() using bare JID for OMEMO chat %s", recipient_bare)
        else:
            cached_resource = self._last_resources.get(recipient_bare)
            if cached_resource:
                try:
                    recipient = JID(cached_resource)
                    logger.debug("XMPP: send() using cached resource %s", cached_resource)
                except Exception as exc:
                    logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        text = content
        is_tool_progress = self._is_tool_progress_message(content)

        # If this chat has a pending voice reply, update the buffered text
        # instead of sending immediately. Tool progress messages are ignored;
        # every other message restarts the debounce timer so the voice reply
        # fires only after the agent turn has settled.
        if recipient_bare in self._voice_reply_chats:
            if is_tool_progress:
                return await self._send_text(recipient, text)
            self._voice_reply_chats[recipient_bare]["text"] = text
            self._schedule_voice_reply(recipient_bare, recipient)
            return await self._send_text(recipient, text)

        return await self._send_text(recipient, text)

    @staticmethod
    def _is_tool_progress_message(content: str) -> bool:
        """Return True if the message is a gateway tool-progress indicator.

        Tool progress messages are ephemeral updates like "💻 Running...",
        "📖 Reading...", or "🐍 Running code..." that should not consume the
        pending voice reply. The gateway prefixes every tool-progress message
        with an emoji, so matching the first character (non-alphanumeric,
        non-space) is simpler and more robust than a fixed verb list, which
        missed most verbs (Searching, Browsing, Writing, Editing, Generating,
        Delegating, Scheduling, Asking, Updating, Listing, Clicking, Typing,
        etc.). A real reply that happens to start with an emoji is an
        acceptable edge case versus reading every tool call aloud.
        """
        if not content:
            return False
        first = content[0]
        return not first.isalnum() and not first.isspace()

    def _schedule_voice_reply(self, recipient_bare: str, recipient: JID) -> None:
        """Restart the debounce timer for a pending voice reply (per chat)."""
        existing = self._voice_reply_debounce_tasks.get(recipient_bare)
        if existing is not None and not existing.done():
            existing.cancel()
            self._voice_reply_debounce_tasks.pop(recipient_bare, None)

        async def _debounced_send() -> None:
            try:
                await asyncio.sleep(self._voice_reply_debounce_delay)
            except asyncio.CancelledError:
                return
            self._voice_reply_debounce_tasks.pop(recipient_bare, None)
            pending = self._voice_reply_chats.pop(recipient_bare, None)
            if pending is None or not pending.get("text"):
                return
            try:
                await self._send_voice_reply_text(recipient, pending["text"])
            except Exception as exc:
                logger.warning("XMPP: TTS voice reply failed (%s); text already sent", exc)

        self._voice_reply_debounce_tasks[recipient_bare] = asyncio.create_task(_debounced_send())

    async def _send_voice_reply_text(self, recipient: JID, text: str) -> SendResult:
        """Generate TTS audio for the first chunk of text and send as a voice message.

        The full text response has already been delivered by send(); this only
        adds the audio reply. Falls back to doing nothing if TTS fails.
        """
        from tools.tts_tool import check_tts_requirements, text_to_speech_tool

        if not check_tts_requirements():
            logger.warning("XMPP: TTS requirements not met; skipping voice reply")
            return SendResult(success=False, error="TTS requirements not met")

        # Only TTS the first chunk; XMPP voice messages are short.
        tts_text = self.prepare_tts_text(text[:4000])
        if not tts_text:
            logger.warning("XMPP: no TTS text after cleanup; skipping voice reply")
            return SendResult(success=False, error="empty TTS text")

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
            return voice_result

        logger.warning("XMPP: TTS audio generation failed or empty; skipping voice reply")
        return SendResult(success=False, error="TTS audio generation failed")

    async def _send_text(self, recipient: JID, text: str) -> SendResult:
        logger.debug("XMPP: _send_text() called for %s: %d chars omemo_chats=%s", recipient.bare, len(text), self._omemo_chats)
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
                # NOTE: no chat-state is set on the outgoing chunks here, and no
                # trailing <active/> stanza is sent. If a client shows a stuck
                # "composing" indicator, that is a known gap (see the follow-up
                # comment at the end of this method).

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
                        logger.debug("XMPP: encrypt_message returned encrypted=%s errors=%s", encrypted is not None, _errors)
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
                            logger.debug("XMPP: OMEMO message chunk %d/%d sent to %s", i+1, len(chunks), recipient)
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

                logger.debug("XMPP: sending plaintext chunk %d/%d to %s", i+1, len(chunks), recipient)
                msg.send()
                logger.debug("XMPP: plaintext message chunk %d/%d sent to %s", i+1, len(chunks), recipient)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.2)

            # NOTE: no standalone <active/> chat-state stanza is sent after the
            # chunks here, and the chunks do not set msg["chat_state"]. Sending
            # <active/> after EVERY send would prematurely end the agent's turn
            # for a streaming client like EchoTalk. The end-of-turn <active/> is
            # instead sent once by stop_typing() at the true end of the turn
            # (to the bare JID so every resource sees it).
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
        ext = audio_path_obj.suffix.lower() or ".mp3"
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

    async def _keep_typing(self, chat_id: str, interval: float = 2.0, metadata=None, stop_event: asyncio.Event | None = None) -> None:
        """
        XMPP-owned typing lifecycle.

        The base adapter's generic refresh loop passes an ``interrupt_event``
        that gets swapped out by ``/new`` and other session-reset commands, so
        the base loop can become orphaned and keep calling ``send_typing()``
        forever.  For XMPP we ignore that loop and drive typing state directly:
        this method resets the anti-resurrection cooldown for a fresh turn and
        sends one ``<composing/>`` stanza, which starts a self-owned 2-second
        refresh task that we cancel deterministically from ``stop_typing()``.
        """
        if not self.typing_indicator or self.client is None:
            return
        try:
            recipient_str = self._last_resources.get(chat_id, chat_id)
            chat_key = str(JID(recipient_str).bare)
        except Exception:
            chat_key = chat_id
        # A fresh turn is starting; clear any stale stop cooldown so the user
        # sees the composing indicator for this new genuine turn.
        self._typing_stop_until.pop(chat_key, None)
        self._typing_state.pop(chat_key, None)
        await self.send_typing(chat_id, metadata=metadata)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if not self.typing_indicator or self.client is None:
            logger.debug("XMPP: send_typing skipped (disabled or no client)")
            return
        try:
            # Use the bare JID as the canonical chat key for state tracking.
            # Send chat-state notifications to the bare JID so every resource
            # (phone, desktop, web) sees the composing indicator.
            recipient_str = self._last_resources.get(chat_id, chat_id)
            recipient_full = JID(recipient_str)
            recipient_bare = JID(recipient_full.bare)
            chat_key = str(recipient_bare)
            now = asyncio.get_event_loop().time()
            # If we recently stopped typing for this chat, ignore stray refresh
            # ticks from any orphaned loop.
            if self._typing_stop_until.get(chat_key, 0.0) > now:
                logger.debug("XMPP: send_typing suppressed for %s (cooldown active)", chat_key)
                return
            # Don't resurrect composing if we already deliberately stopped.
            if self._typing_state.get(chat_key) == "active":
                logger.debug("XMPP: send_typing suppressed for %s (state is active)", chat_key)
                return
            msg = self.client.make_message(mto=recipient_bare, mtype="chat")
            msg["chat_state"] = "composing"
            msg.send()
            self._typing_state[chat_key] = "composing"
            logger.debug("XMPP: typing indicator sent to %s", recipient_bare)
            # Start a self-owned refresh loop for XEP-0085 chat states.  This is
            # the only refresh loop we rely on; the base adapter's generic loop
            # is overridden above to a single send + this refresh.
            self._ensure_typing_refresh_task(chat_key, recipient_bare)
        except Exception as exc:
            logger.warning("XMPP: typing indicator send failed: %s", exc)

    def _ensure_typing_refresh_task(self, chat_key: str, recipient: JID) -> None:
        """Start a 2s refresh loop for <composing/> that we can kill locally."""
        task = self._typing_refresh_tasks.get(chat_key)
        if task is not None and not task.done():
            return

        async def _refresh() -> None:
            try:
                while True:
                    await asyncio.sleep(2.0)
                    try:
                        await self.send_typing(str(recipient.bare))
                    except Exception:
                        break
            except asyncio.CancelledError:
                pass

        self._typing_refresh_tasks[chat_key] = asyncio.create_task(_refresh())

    def _cancel_typing_refresh_task(self, chat_key: str) -> None:
        """Cancel the self-owned composing refresh loop, if running."""
        task = self._typing_refresh_tasks.pop(chat_key, None)
        if task is not None and not task.done():
            task.cancel()
        # Once we cancel, reset state so a future genuine turn can type again.
        # We deliberately do NOT clear _typing_stop_until here; that cooldown is
        # managed by stop_typing() and protects against orphaned ticks.
        if self._typing_state.get(chat_key) == "composing":
            self._typing_state.pop(chat_key, None)

    async def _send_active_to_bare(self, recipient_bare: str) -> None:
        """Send a standalone <active/> chat-state stanza to a bare JID.

        Sent to the BARE JID (not a specific resource) so every connected
        resource - including EchoTalk's /EchoTalk and desktop clients like
        Gajim - observes the composing->active transition and clears its
        "thinking" indicator. A redundant <active/> is harmless (chat-state is
        idempotent), so this is safe even when the base adapter also sends one.
        """
        if self.client is None:
            return
        try:
            msg = self.client.make_message(mto=JID(recipient_bare), mtype="chat")
            msg["chat_state"] = "active"
            msg.send()
            logger.debug("XMPP: sent standalone <active/> to %s", recipient_bare)
        except Exception as exc:
            logger.warning("XMPP: failed to send <active/> to %s: %s", recipient_bare, exc)

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        if not self.typing_indicator or self.client is None:
            logger.debug("XMPP: stop_typing skipped (disabled or no client)")
            return
        try:
            recipient_str = self._last_resources.get(chat_id, chat_id)
            recipient = JID(recipient_str)
            chat_key = str(recipient.bare)
            # Kill our own refresh loop first so it cannot resurrect composing
            # while we are sending the active stanza or right after.
            self._cancel_typing_refresh_task(chat_key)
            # Send the end-of-turn <active/> to the BARE JID (not the last-seen
            # resource) so every connected resource - including EchoTalk's
            # /EchoTalk and desktop clients like Gajim - observes the
            # composing->active transition. The base adapter calls stop_typing()
            # once at the true end of the agent turn, which is the correct
            # timing for a streaming client: composing stays true through the
            # turn and flips to active exactly once at the end.
            await self._send_active_to_bare(str(recipient.bare))
            self._typing_state[chat_key] = "active"
            # Cooldown: suppress composing refreshes for 3s after a deliberate stop.
            # The base adapter's _keep_typing loop refreshes every 2s and may
            # outlive the stop signal on the /new path, so this prevents the
            # composing bubble from popping back up.
            self._typing_stop_until[chat_key] = asyncio.get_event_loop().time() + 3.0
            logger.debug("XMPP: stop typing sent to %s", recipient.bare)
        except Exception as exc:
            logger.warning("XMPP: stop typing send failed: %s", exc)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """XMPP does not support message editing.

        Returning ``success=False`` lets the gateway fall back to sending each
        tool-progress update as a separate message, so XMPP behaves like
        Mattermost for live tool status.
        """
        return SendResult(success=False, error="not supported")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

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
        # Match a URL, then strip trailing punctuation (.,;:!?) and closing
        # brackets/quotes that are not part of the URL. The period is kept in
        # the match because it is a legitimate URL character (domain names and
        # file extensions); only a trailing period is removed afterward.
        match = re.search(r"https?://[^\s<>\"')\]}]+|aesgcm://[^\s<>\"')\]}]+", text)
        if not match:
            return None
        url = match.group(0)
        return url.rstrip(".,;:!?")

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
            try:
                img = img.resize((480, 480), Image.Resampling.LANCZOS)
            except AttributeError:
                # Pillow < 9.1 uses the Image.LANCZOS constant.
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
        logger.info("XMPP: session_start handler fired for %s", self.user_jid)
        try:
            if self.client:
                self.client.send_presence()
                self.client.get_roster()
        except Exception:
            pass
        self._session_started_event.set()
        # Proactively request presence subscriptions from allowlisted users so
        # fresh installs show online (green) in the contact's client without a
        # manual re-add. No allowlist configured -> nothing to enumerate.
        asyncio.create_task(self._send_subscription_requests())

    def _allowed_users_set(self) -> set[str]:
        """Bare-JID allowlist set from env/config (empty when none configured)."""
        raw = os.getenv("XMPP_ALLOWED_USERS")
        if not raw:
            extra = getattr(getattr(self, "config", None), "extra", None) or {}
            value = extra.get("allowed_users", "")
            raw = value if isinstance(value, str) else ""
        entries = raw.split(",") if isinstance(raw, str) else list(raw or [])
        return {e.strip().lower() for e in entries if e and e.strip()}

    def _allow_all_users(self) -> bool:
        return os.getenv("XMPP_ALLOW_ALL_USERS", "").strip().lower() in {
            "true", "1", "yes",
        }

    async def _send_subscription_requests(self) -> None:
        """Option 2: proactively ask each allowlisted JID to add the bot.

        Skipped entirely when no allowlist is configured (allow-all or deny-all):
        there is no one to invite. Errors are logged, never fatal.
        """
        try:
            allowed = self._allowed_users_set()
            if not allowed:
                return
            client = self.client
            if client is None:
                return
            await asyncio.wait_for(
                self._session_started_event.wait(), timeout=5.0
            )
            for bare in sorted(allowed):
                if bare == JID(self.user_jid).bare.lower():
                    continue  # never subscribe to ourselves
                try:
                    client.send_presence(pto=bare, ptype="subscribe")
                    logger.info(
                        "XMPP: sent presence subscription request to %s", bare
                    )
                except Exception as exc:
                    logger.warning(
                        "XMPP: subscription request to %s failed: %s", bare, exc
                    )
        except Exception:
            logger.exception("XMPP: subscription-request pass failed")

    def _sender_authorized(self, bare_jid: str) -> bool:
        """True when a bare JID may interact (allowlist or allow-all)."""
        if self._allow_all_users():
            return True
        return bare_jid in self._allowed_users_set()

    async def _on_presence_subscribe(self, presence):
        """Option 1: auto-approve subscription requests from allowed senders.

        Allowed senders get an immediate `subscribed` plus our own `subscribe`
        so the roster ends up mutual ("both") and the bot shows green.
        Requests from anyone else are silently ignored (same policy as denied
        messages). Runs on the event loop; handler bodies must not block.
        """
        try:
            from_jid = presence["from"]
            bare = str(from_jid.bare).lower()
            if not self._sender_authorized(bare):
                logger.info(
                    "XMPP: ignoring presence subscription request from non-allowlisted %s",
                    bare,
                )
                return
            client = self.client
            if client is None:
                return
            client.send_presence(pto=str(from_jid.bare), ptype="subscribed")
            client.send_presence(pto=str(from_jid.bare), ptype="subscribe")
            logger.info("XMPP: approved presence subscription for %s", bare)
        except Exception:
            logger.exception("XMPP: presence_subscribe handling failed")

    async def _omemo_initialized(self, event=None):
        logger.info("XMPP: OMEMO initialized and device list published")
        self._omemo_ready_event.set()

    async def _on_message(self, msg: Message):
        try:
            if msg["type"] not in ("chat", "normal"):
                return

            logger.debug("XMPP: _on_message fired type=%s from=%s", msg.get("type", ""), msg["from"])
            sender_jid = msg["from"]
            if not sender_jid:
                logger.debug("XMPP: _on_message returning - no sender_jid")
                return
            sender_full = JID(sender_jid)
            sender_bare = str(sender_full.bare)
            self._last_resources[sender_bare] = str(sender_full)
            if sender_bare == JID(self.user_jid).bare:
                logger.debug("XMPP: _on_message returning - self-message from %s", sender_bare)
                return

            body = msg.get("body", "").strip()
            logger.debug("XMPP: _on_message body=%r has_encrypted check next", body)
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
            logger.debug("XMPP: _on_message has_encrypted=%s omemo=%s omemo_enabled=%s", has_encrypted, omemo is not None, self.omemo_enabled)
            if omemo is not None and self.omemo_enabled and has_encrypted:
                try:
                    decrypted, _device_info = await omemo.decrypt_message(msg)
                    body_text = str(decrypted.get("body", "") or "").strip()
                    if body_text and body_text != body:
                        body = body_text
                        encrypted = True
                        logger.debug("XMPP: OMEMO decrypted message from %s: %s chars", sender_bare, len(body))
                except Exception as exc:
                    logger.warning("XMPP: OMEMO decrypt attempt failed: %s", exc, exc_info=True)
                    # Fall back to plaintext body if decryption fails.

            # Remember that this chat is OMEMO-active so all replies are encrypted.
            if encrypted and sender_bare not in self._omemo_chats:
                self._omemo_chats.add(sender_bare)
                logger.debug("XMPP: chat %s added to OMEMO-active set", sender_bare)
            elif not encrypted and sender_bare in self._omemo_chats:
                # If the contact downgrades to plaintext, stop forcing OMEMO.
                self._omemo_chats.discard(sender_bare)
                logger.debug("XMPP: chat %s removed from OMEMO-active set (plaintext received)", sender_bare)

            # NOTE: do NOT return early on an empty body here. Voice and image
            # messages are often sent as standalone media with an empty text
            # body; the URL lives in <oob>/<file-sharing>, which is extracted
            # below. We only drop the message later if there is no body AND no
            # media URL.

            # Send read receipt for messages that request it.
            try:
                markable = msg.xml.find(".//{urn:xmpp:chat-markers:0}markable") is not None
                logger.debug("XMPP: _on_message markable=%s", markable)
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
                # XEP-0363 HTTP File Upload and XEP-0447 Stateless File Sharing
                # place the URL inside <file-sharing>/<file>/<uri> or
                # <file-sharing>/<file>/<desc> plus a source <url>. Clients like
                # Beagle, Dino, or newer versions of Conversations may use these
                # instead of the older OOB extension.
                #
                # Namespace split (per XEP-0447): <file-sharing> is in
                # urn:xmpp:sfs:0, but its <file> child is in urn:xmpp:share:1.
                # The outbound side (send_voice/send_image_file) uses this same
                # split, so the inbound parser must too.
                try:
                    ns_share = "urn:xmpp:share:1"
                    ns_sshare = "urn:xmpp:sfs:0"
                    for sfs in msg.xml.findall(f".//{{{ns_sshare}}}file-sharing"):
                        file_el = sfs.find(f"{{{ns_share}}}file")
                        if file_el is None:
                            continue
                        url_el = file_el.find(f"{{{ns_share}}}uri") or file_el.find(f"{{{ns_share}}}url")
                        if url_el is not None and url_el.text:
                            url = url_el.text.strip()
                            break
                        # Some clients put the URL in a <sources>/<url> child.
                        sources = file_el.find(f"{{{ns_share}}}sources") or sfs.find(f"{{{ns_sshare}}}sources")
                        if sources is not None:
                            for source_url in sources.findall(f".//{{{ns_share}}}url"):
                                if source_url is not None and source_url.text:
                                    url = source_url.text.strip()
                                    break
                        if url:
                            break
                except Exception:
                    pass
            if not url:
                url = self._extract_url(body)

            # Drop only when there is no text AND no media URL. A standalone
            # media message (voice/image) has an empty body but a URL, so it
            # must not be dropped here.
            if not body and not url:
                logger.debug("XMPP: _on_message returning - empty body and no media URL")
                return

            media_path: Optional[str] = None
            msg_type = MessageType.TEXT
            original_msg_type = MessageType.TEXT
            if url:
                logger.debug("XMPP: detected URL in message: %s", url)
                if url.startswith("aesgcm://") or _is_media_url(url):
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
                            if _guess_audio_is_voice(url, body, data):
                                msg_type = MessageType.VOICE
                            else:
                                msg_type = MessageType.AUDIO
                            ext = _guess_audio_extension(url, data)
                            media_path = self._cache_media(data, "audio", ext=ext)
                        elif _is_audio_url(url):
                            # URL claims to be audio but content could not be sniffed;
                            # trust the URL extension and treat as plain audio.
                            msg_type = MessageType.AUDIO
                            ext = _guess_audio_extension(url, data)
                            media_path = self._cache_media(data, "audio", ext=ext)
                        else:
                            # Fallback for any other downloaded binary blob.
                            msg_type = MessageType.PHOTO
                            media_path = self._cache_media(data, "image")
                    else:
                        logger.warning("XMPP: failed to download media from %s", url)
                        msg_type = MessageType.TEXT
                else:
                    # Plain hyperlink, not media; leave as text.
                    logger.debug("XMPP: treating URL as text link: %s", url)
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
                            # single outbound voice reply at turn end.
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

            logger.debug("XMPP: about to handle_message event text=%r type=%s", display_text, msg_type)

            # If the inbound message was a voice message, opt this DM chat into
            # the adapter-level voice reply queue so we reply with TTS audio plus
            # the full text response. This is independent of the global
            # voice.auto_tts setting, which controls auto-TTS for *all* replies.
            if original_msg_type == MessageType.VOICE:
                self._voice_reply_chats[sender_bare] = {"text": ""}
                logger.info(
                    "XMPP: queued voice reply for chat %s (voice input)",
                    sender_bare,
                )

            await self.handle_message(event)
            logger.debug("XMPP: handle_message completed")
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
            logger.debug("XMPP: sent displayed marker to %s for %s", recipient, message_id)
        except Exception as exc:
            logger.debug("XMPP: failed to send displayed marker: %s", exc)

    def _cache_media(self, data: bytes, kind: str = "image", ext: Optional[str] = None) -> Optional[str]:
        try:
            if ext is None:
                ext = ".ogg" if kind == "audio" else ".png"
            elif not ext:
                ext = ".ogg" if kind == "audio" else ".png"
            # Derive the MIME type from the actual extension so the cached file
            # is tagged correctly (e.g. .ogg -> audio/ogg, not audio/mpeg).
            mime = _mime_from_extension(ext)
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
    # Bridge the allowlist / allow-all config keys to the env vars the gateway
    # authorization layer reads (XMPP_ALLOWED_USERS / XMPP_ALLOW_ALL_USERS), so
    # config.yaml is the source of truth for access control. Env vars win when
    # already set (env > YAML precedence).
    if "allow_all_users" in platform_cfg and not os.getenv("XMPP_ALLOW_ALL_USERS"):
        os.environ["XMPP_ALLOW_ALL_USERS"] = str(platform_cfg["allow_all_users"]).lower()
    if "allowed_users" in platform_cfg and not os.getenv("XMPP_ALLOWED_USERS"):
        os.environ["XMPP_ALLOWED_USERS"] = str(platform_cfg["allowed_users"])
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
        install_hint="Talk to Hermes over XMPP",
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
