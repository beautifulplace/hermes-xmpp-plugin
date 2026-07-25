"""XMPP platform adapter for Hermes Agent.

This adapter connects to an XMPP server, receives messages via a lightweight
middleware pipeline, and sends replies back. It supports plaintext and OMEMO
encryption, inbound/outbound media, typing indicators, read receipts, and
avatars.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)
from PIL import Image
from slixmpp import JID, ClientXMPP
from slixmpp.plugins.base import register_plugin
from slixmpp.stanza import Message

from .middlewares import (
    AutoSethomeMiddleware,
    BuildEventMiddleware,
    InboundContext,
    InboundPipeline,
    MediaResolveMiddleware,
    OMEMODecryptMiddleware,
    ReadReceiptMiddleware,
    TranscribeVoiceMiddleware,
    ValidateMiddleware,
    VoiceDetectMiddleware,
)
from .xmpp_utils import guess_content_type, mime_from_extension, parse_bool, set_nested_config_value

logger = logging.getLogger(__name__)


def _omemo_available() -> bool:
    try:
        import slixmpp_omemo  # noqa: F401
        return True
    except Exception:
        return False


class XMPPAdapter(BasePlatformAdapter):
    """XMPP gateway adapter for Hermes."""

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

        self.omemo_enabled = parse_bool(
            os.getenv("XMPP_OMEMO_ENABLED") or extra.get("omemo_enabled"), True
        )
        self.omemo_allow_untrusted = parse_bool(
            os.getenv("XMPP_OMEMO_ALLOW_UNTRUSTED")
            or os.getenv("XMPP_OTR_ALLOW_UNTRUSTED")
            or extra.get("omemo_allow_untrusted"),
            True,
        )
        self.omemo_plugin_name = "xep_0384"
        self.omemo_storage_path: Optional[Path] = None
        self._omemo_ready_event = asyncio.Event()

        self.typing_indicator = parse_bool(
            os.getenv("XMPP_TYPING_INDICATOR") or extra.get("typing_indicator"), True
        )
        self.avatar_path = os.getenv("XMPP_AVATAR_PATH") or extra.get("avatar_path", "")
        self.home_channel = os.getenv("XMPP_HOME_CHANNEL") or extra.get("home_channel", "")

        _allow_all_env = os.getenv("XMPP_ALLOW_ALL_USERS", "").strip().lower()
        self.allow_all_users = (
            _allow_all_env in {"true", "1", "yes"}
            if _allow_all_env
            else parse_bool(extra.get("allow_all_users"), False)
        )
        _allowed_env = os.getenv("XMPP_ALLOWED_USERS", "").strip()
        if _allowed_env:
            self.allowed_users = [u.strip() for u in _allowed_env.split(",") if u.strip()]
        else:
            raw_allowed = extra.get("allowed_users")
            self.allowed_users = (
                list(raw_allowed)
                if isinstance(raw_allowed, (list, tuple))
                else ([raw_allowed.strip()] if isinstance(raw_allowed, str) and raw_allowed.strip() else [])
            )

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

        self._voice_reply_chats: set[str] = set()
        self._last_resources: Dict[str, str] = {}
        self._omemo_chats: set[str] = set()
        self._auto_sethome_done = False

        # Outbound messages sent to a bare JID before we know a full resource
        # (e.g. gateway restart notifications) are queued and flushed once an
        # inbound message from that JID gives us a usable resource.
        self._pending_messages: Dict[str, list[str]] = {}

        self._inbound_pipeline = InboundPipeline([
            ValidateMiddleware(),
            OMEMODecryptMiddleware(),
            VoiceDetectMiddleware(),
            MediaResolveMiddleware(),
            TranscribeVoiceMiddleware(),
            ReadReceiptMiddleware(),
            AutoSethomeMiddleware(),
            BuildEventMiddleware(),
        ])

    @property
    def name(self) -> str:
        return "XMPP"

    # -- OMEMO helpers -------------------------------------------------------

    _omemo_registered: bool = False

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
            if not XMPPAdapter._omemo_registered:
                from .omemo_plugin import HermesOMEMO

                register_plugin(HermesOMEMO, name=self.omemo_plugin_name)
                XMPPAdapter._omemo_registered = True
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
            self._set_fatal_error("missing_credentials", "XMPP credentials missing", retryable=False)
            return False

        if is_reconnect:
            logger.info("XMPP: tearing down old client before reconnect")
            await self._cleanup_client()

        self._session_started_event.clear()
        self._omemo_ready_event.clear()

        try:
            jid_str = self.user_jid
            if "/" not in jid_str:
                jid_str = f"{self.user_jid}/Hermes"

            self.client = ClientXMPP(jid_str, self.password)
            self.client.use_message_ids = True
            self.client.register_plugin("xep_0030")
            try:
                disco = self.client.plugin.get("xep_0030")
                if disco is not None:
                    disco.add_identity(category="client", itype="pc", name="Hermes")
            except Exception as exc:
                logger.debug("XMPP: could not set disco identity: %s", exc)

            self.client.register_plugin("xep_0004")  # Data Forms
            self.client.register_plugin("xep_0060")  # PubSub
            self.client.register_plugin("xep_0066")  # Out of Band Data
            self.client.register_plugin("xep_0054")  # vCard-temp
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
                self.client.add_event_handler("omemo_initialized", self._omemo_initialized)
                if not self._configure_omemo():
                    logger.warning("XMPP: OMEMO requested but could not be enabled")

            self.client.add_event_handler("session_start", self._session_start)
            self.client.add_event_handler("message", self._on_message)
            self.client.add_event_handler("exception", self._slixmpp_exception_handler)
            self.client.add_event_handler("disconnected", self._on_disconnected)

            logger.info("XMPP: connecting as %s to %s:%s ...", self.user_jid, self.server or "(auto)", self.port)

            connect_future = self.client.connect(host=self.server or None, port=self.port)
            if connect_future is not None:
                self._xmpp_background_tasks.add(
                    asyncio.create_task(self._watch_client_future(connect_future))
                )

            try:
                await asyncio.wait_for(self._session_started_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("XMPP: session_start did not arrive within 30s")
                self._set_fatal_error("connect_timeout", "session_start timed out", retryable=True)
                return False

            self._mark_connected()
            self._last_activity = asyncio.get_event_loop().time()
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            asyncio.create_task(self._finish_setup())
            return True
        except Exception as e:
            logger.error("XMPP: failed to connect as %s — %s", self.user_jid, e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

    async def _watch_client_future(self, future) -> None:
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
        if self._avatar_republish_task and not self._avatar_republish_task.done():
            return

        async def _republish_after_delay():
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
                    self.client.send_raw(" ")
                    self._last_activity = asyncio.get_event_loop().time()
            except Exception as exc:
                logger.warning("XMPP: keepalive ping failed: %s", exc)
                if self.is_connected:
                    self._schedule_internal_reconnect("ping_failed", str(exc))
                break

    def _schedule_internal_reconnect(self, code: str, message: str) -> None:
        if self._internal_reconnect_task and not self._internal_reconnect_task.done():
            return
        if not self.is_connected:
            return

        async def _reconnect_loop():
            logger.info("XMPP: starting internal reconnect after %s: %s", code, message)
            delays = [5.0, 10.0, 20.0]
            for attempt, delay in enumerate(delays, 1):
                await asyncio.sleep(delay)
                if not self.is_connected:
                    logger.info("XMPP: connection restored before reconnect attempt %d", attempt)
                    return
                logger.info("XMPP: internal reconnect attempt %d/%d", attempt, len(delays))
                try:
                    result = await self.connect(is_reconnect=True)
                    if result:
                        logger.info("XMPP: internal reconnect succeeded on attempt %d", attempt)
                        return
                except Exception as exc:
                    logger.warning("XMPP: internal reconnect attempt %d failed: %s", attempt, exc)
            logger.warning("XMPP: internal reconnect exhausted; escalating to gateway")
            self._mark_disconnected(code=code, message=message)

        self._internal_reconnect_task = asyncio.create_task(_reconnect_loop())

    async def _on_disconnected(self, event):
        logger.warning("XMPP: disconnected event received; event=%s", event)
        if self.is_connected:
            self._schedule_internal_reconnect("disconnected", str(event))

    async def disconnect(self) -> None:
        await self._cleanup_client()
        self._mark_disconnected()

    async def _cleanup_client(self) -> None:
        for task in list(self._xmpp_background_tasks):
            if not task.done():
                task.cancel()
        self._xmpp_background_tasks.clear()

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

        if self._internal_reconnect_task and not self._internal_reconnect_task.done():
            self._internal_reconnect_task.cancel()
        self._internal_reconnect_task = None

        if self._avatar_republish_task and not self._avatar_republish_task.done():
            self._avatar_republish_task.cancel()
        self._avatar_republish_task = None

        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.client.wait_until("disconnected"), timeout=5.0)
            except Exception:
                pass
            self.client = None

    # -- Sending -------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self.client is None:
            logger.error("XMPP: cannot send, client not connected")
            return SendResult(success=False, error="not connected")

        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        cached_resource = self._last_resources.get(str(recipient.bare))
        if cached_resource:
            try:
                recipient = JID(cached_resource)
            except Exception as exc:
                logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        chat_id_str = str(recipient.bare)
        # Note: auto-TTS voice replies are handled by the gateway calling
        # send_voice() directly; send() delivers the matching text response.
        if chat_id_str in self._voice_reply_chats:
            self._voice_reply_chats.discard(chat_id_str)

        # If we do not yet have a resource for this contact and OMEMO is
        # enabled, queue the message. It will be flushed once an inbound
        # message arrives with a usable resource. This fixes restart
        # notifications that are sent before the contact's device list is known.
        if self.omemo_enabled and not cached_resource:
            self._pending_messages.setdefault(chat_id_str, []).append(content)
            logger.info(
                "XMPP: queued message for %s until a resource is known (pending: %d)",
                chat_id_str,
                len(self._pending_messages[chat_id_str]),
            )
            return SendResult(success=True)

        return await self._send_text(recipient, content)

    async def _send_voice_reply_text(self, recipient: JID, text: str) -> SendResult:
        from tools.tts_tool import check_tts_requirements, text_to_speech_tool

        if not check_tts_requirements():
            return await self._send_text(recipient, text)

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
                await self._send_text(recipient, text)
                return voice_result

        logger.warning("XMPP: TTS audio generation failed or empty; sending text only")
        return await self._send_text(recipient, text)

    async def _send_text(self, recipient: JID, text: str) -> SendResult:
        chunk_size = 2000
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

        recipient_bare = str(recipient.bare)
        force_omemo = self.omemo_enabled and recipient_bare in self._omemo_chats
        if force_omemo:
            logger.info("XMPP: forcing OMEMO for %s", recipient_bare)

        try:
            for i, chunk in enumerate(chunks):
                msg = self.client.make_message(mto=recipient, mtype="chat")
                msg["body"] = chunk
                msg["id"] = self.client.new_id()
                msg["chat_state"] = "active"

                omemo = self._omemo_plugin()
                if omemo is not None and self.omemo_enabled:
                    try:
                        msg.set_to(recipient)
                        msg.set_from(self.client.boundjid)
                        encrypted, _errors = await omemo.encrypt_message(
                            msg,
                            recipient_jids={recipient},
                            identifier=str(recipient.bare),
                        )
                        if encrypted is not None:
                            try:
                                encrypted["eme"]["namespace"] = "eu.siacs.conversations.axolotl"
                                encrypted["eme"]["name"] = "OMEMO"
                            except Exception as eme_exc:
                                logger.debug("XMPP: failed to set EME namespace: %s", eme_exc)
                            encrypted.send()
                            if i < len(chunks) - 1:
                                await asyncio.sleep(0.2)
                            continue
                    except Exception as exc:
                        if force_omemo:
                            logger.error("XMPP: OMEMO encryption required but failed: %s", exc)
                            return SendResult(success=False, error=f"OMEMO encryption required but failed: {exc}")
                        logger.warning("XMPP: OMEMO send failed (%s); falling back to plaintext", exc)
                elif force_omemo:
                    logger.error("XMPP: OMEMO encryption required but OMEMO plugin unavailable")
                    return SendResult(success=False, error="OMEMO plugin unavailable")

                msg.send()
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
        """Send an audio file as a voice/audio message over XMPP."""
        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        # Use the last known full resource for OMEMO routing and receipts.
        cached_resource = self._last_resources.get(str(recipient.bare))
        if cached_resource:
            try:
                recipient = JID(cached_resource)
            except Exception as exc:
                logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        audio_path_obj = Path(audio_path)
        if not audio_path_obj.exists():
            return SendResult(success=False, error=f"audio file not found: {audio_path}")

        audio_bytes = audio_path_obj.read_bytes()
        ext = audio_path_obj.suffix.lower() or ".m4a"
        converted_created = False

        # Conversations records/expects .m4a for voice messages; the TTS tool
        # emits .mp3 by default, so convert before sending.
        if ext == ".mp3":
            try:
                import tempfile
                converted = Path(tempfile.gettempdir()) / f"voice_{uuid.uuid4().hex}.m4a"
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(audio_path_obj), "-c:a", "aac", "-b:a", "32k", str(converted),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0 and converted.exists():
                    logger.info("XMPP: converted TTS mp3 to m4a: %s", converted)
                    audio_path_obj = converted
                    audio_bytes = audio_path_obj.read_bytes()
                    ext = ".m4a"
                    converted_created = True
                else:
                    logger.warning("XMPP: ffmpeg mp3->m4a failed (rc=%s): %s", proc.returncode, stderr.decode()[:200])
            except Exception as exc:
                logger.warning("XMPP: could not convert mp3 to m4a: %s", exc)

        content_type = mime_from_extension(ext)
        filename = f"voice_{uuid.uuid4().hex}{ext}"

        url = await self._upload_encrypted_media(audio_bytes, filename, content_type)
        if not url:
            url = await self._upload_file(audio_bytes, filename, content_type)
        if not url:
            return SendResult(success=False, error="HTTP file upload failed")

        # Clean up any temporary converted audio file we created.
        if converted_created:
            try:
                audio_path_obj.unlink(missing_ok=True)
            except OSError:
                pass

        logger.info("XMPP: voice upload URL for %s: %s", recipient, url)

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
                msg.set_to(recipient)
                msg.set_from(self.client.boundjid)
                encrypted, _errors = await omemo.encrypt_message(
                    msg,
                    recipient_jids={recipient},
                    identifier=str(recipient.bare),
                )
                if encrypted is not None:
                    try:
                        encrypted["eme"]["namespace"] = "eu.siacs.conversations.axolotl"
                        encrypted["eme"]["name"] = "OMEMO"
                    except Exception as eme_exc:
                        logger.debug("XMPP: failed to set EME namespace: %s", eme_exc)
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
        caption: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image file over XMPP using HTTP File Upload."""
        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        # Use the last known full resource for OMEMO routing and receipts.
        cached_resource = self._last_resources.get(str(recipient.bare))
        if cached_resource:
            try:
                recipient = JID(cached_resource)
            except Exception as exc:
                logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        image_path_obj = Path(image_path)
        if not image_path_obj.exists():
            return SendResult(success=False, error=f"image file not found: {image_path}")

        image_bytes = image_path_obj.read_bytes()
        ext = image_path_obj.suffix.lower() or ".png"
        content_type = mime_from_extension(ext)
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
                msg.set_to(recipient)
                msg.set_from(self.client.boundjid)
                encrypted, _errors = await omemo.encrypt_message(
                    msg,
                    recipient_jids={recipient},
                    identifier=str(recipient.bare),
                )
                if encrypted is not None:
                    try:
                        encrypted["eme"]["namespace"] = "eu.siacs.conversations.axolotl"
                        encrypted["eme"]["name"] = "OMEMO"
                    except Exception as eme_exc:
                        logger.debug("XMPP: failed to set EME namespace: %s", eme_exc)
                    encrypted.send()
                    logger.info("XMPP: OMEMO image sent to %s", recipient.bare)
                    return SendResult(success=True)
            except Exception as exc:
                logger.warning("XMPP: OMEMO image send failed (%s); falling back", exc)

        msg.send()
        logger.info("XMPP: image sent to %s", recipient.bare)
        return SendResult(success=True)

    async def _send_file_by_upload(
        self,
        chat_id: str,
        file_path: str,
        content_type_hint: str,
        caption: str = "",
    ) -> SendResult:
        if self.client is None:
            return SendResult(success=False, error="not connected")

        path = Path(file_path)
        if not path.exists():
            return SendResult(success=False, error=f"file not found: {file_path}")

        try:
            data = path.read_bytes()
            content_type = guess_content_type(data)
            if content_type == "unknown":
                content_type = content_type_hint
            upload_plugin = self.client.plugin.get("xep_0363", None)
            if upload_plugin is None:
                return SendResult(success=False, error="HTTP upload plugin not available")

            upload_url = await upload_plugin.request_upload_slot(
                filename=path.name,
                size=len(data),
                content_type=content_type,
            )
            if not upload_url:
                return SendResult(success=False, error="failed to request upload slot")

            async with httpx.AsyncClient() as client:
                response = await client.put(upload_url["put_url"], content=data, headers={"Content-Type": content_type})
                response.raise_for_status()

            get_url = upload_url.get("get_url", upload_url.get("url", ""))
            if not get_url:
                return SendResult(success=False, error="upload completed but no get_url returned")

            recipient = JID(chat_id)
            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["body"] = caption or get_url
            if caption:
                msg["oob"]["url"] = get_url
            msg.send()
            return SendResult(success=True)
        except Exception as exc:
            logger.exception("XMPP: file upload/send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _upload_encrypted_media(self, plaintext: bytes, filename: str, content_type: str) -> Optional[str]:
        """Encrypt plaintext with AES-256-GCM and upload via HTTP File Upload."""
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
            return
        try:
            recipient_str = self._last_resources.get(chat_id, chat_id)
            recipient = JID(recipient_str)
            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["chat_state"] = "composing"
            msg.send()
        except Exception as exc:
            logger.debug("XMPP: typing indicator send failed: %s", exc)

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        if not self.typing_indicator or self.client is None:
            return
        try:
            recipient_str = self._last_resources.get(chat_id, chat_id)
            recipient = JID(recipient_str)
            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["chat_state"] = "active"
            msg.send()
        except Exception as exc:
            logger.debug("XMPP: stop typing send failed: %s", exc)

    # -- Inbound handling ----------------------------------------------------

    async def _session_start(self, event):
        logger.info("XMPP: session started for %s", self.user_jid)
        try:
            self.client.send_presence()
            self.client.get_roster()
        except Exception as exc:
            logger.warning("XMPP: session start presence/roster error: %s", exc)
        self._session_started_event.set()

    async def _omemo_initialized(self, event=None):
        logger.info("XMPP: OMEMO initialized and device list published")
        self._omemo_ready_event.set()

    async def _on_message(self, msg: Message):
        ctx = InboundContext(adapter=self, msg=msg)
        await self._inbound_pipeline.run(ctx)

        # After handling the inbound message we have a resource for the sender;
        # flush any messages that were queued before a resource was known.
        sender_bare = str(JID(msg["from"]).bare) if msg.get("from") else ""
        if sender_bare:
            await self._flush_pending_messages(sender_bare)

    async def _flush_pending_messages(self, sender_bare: str) -> None:
        """Send any messages queued for *sender_bare* now that a resource is known."""
        pending = self._pending_messages.pop(sender_bare, [])
        if not pending:
            return
        logger.info("XMPP: flushing %d pending message(s) to %s", len(pending), sender_bare)
        try:
            recipient = JID(sender_bare)
        except Exception as exc:
            logger.warning("XMPP: cannot flush pending messages to invalid JID %s: %s", sender_bare, exc)
            return

        for text in pending:
            try:
                result = await self._send_text(recipient, text)
                if result.success:
                    logger.info("XMPP: flushed queued message to %s", sender_bare)
                else:
                    logger.warning("XMPP: flushed queued message to %s failed: %s", sender_bare, result.error)
            except Exception as exc:
                logger.exception("XMPP: flushed queued message to %s raised: %s", sender_bare, exc)

    def _build_source(self, sender_bare: str) -> Any:
        from gateway.session import SessionSource

        return SessionSource(
            platform=self.platform,
            chat_id=sender_bare,
            user_id=sender_bare,
            chat_name=sender_bare,
            user_name=sender_bare,
            chat_type="dm",
        )

    def _send_displayed_marker(self, to_jid: str, message_id: str) -> None:
        if self.client is None:
            return
        marker_plugin = self.client.plugin.get("xep_0333", None)
        if marker_plugin is None:
            return
        recipient = self._last_resources.get(to_jid, to_jid)
        marker_plugin.send_marker(mto=JID(recipient), id=message_id, marker="displayed", mtype="chat")
        logger.info("XMPP: sent displayed marker to %s for %s", recipient, message_id)

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

            width, height = img.size
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((480, 480), Image.LANCZOS)

            png_buffer = io.BytesIO()
            img.save(png_buffer, format="PNG", optimize=True)
            data = png_buffer.getvalue()

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

            pep_avatar = self.client.plugin.get("xep_0084", None)
            if pep_avatar is not None:
                try:
                    avatar_id = pep_avatar.generate_id(data)
                    await asyncio.wait_for(pep_avatar.publish_avatar(data), timeout=5.0)
                    await asyncio.wait_for(
                        pep_avatar.publish_avatar_metadata({
                            "id": avatar_id,
                            "type": "image/png",
                            "bytes": len(data),
                            "width": img.width,
                            "height": img.height,
                        }),
                        timeout=5.0,
                    )
                    logger.info("XMPP: published PEP avatar id=%s (%d bytes, %dx%d)", avatar_id[:16], len(data), img.width, img.height)
                except Exception as exc:
                    logger.warning("XMPP: PEP avatar publish failed: %s", exc)
        except Exception as exc:
            logger.warning("XMPP: failed to publish avatar: %s", exc, exc_info=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic chat info for XMPP DMs."""
        return {
            "name": chat_id,
            "type": "dm",
            "platform": "xmpp",
            "chat_id": chat_id,
        }

    # -- Auto-sethome helpers ------------------------------------------------

    def _sender_may_designate_home(self, sender_bare: str) -> bool:
        """Return True if the sender is authorized to become the home channel."""
        if self.allow_all_users:
            return True
        allowed = getattr(self, "allowed_users", None)
        if allowed and sender_bare in (allowed if isinstance(allowed, (list, tuple, set)) else [allowed]):
            return True
        return False

    def _set_home_channel(self, jid: str) -> None:
        """Persist a JID as platforms.xmpp.home_channel in config.yaml."""
        from hermes_constants import get_hermes_home

        config_path = get_hermes_home() / "config.yaml"
        if not config_path.exists():
            return
        original = config_path.read_text()
        updated = set_nested_config_value(original, "platforms", "xmpp", "home_channel", jid)
        config_path.write_text(updated)
        self.home_channel = jid
        os.environ["XMPP_HOME_CHANNEL"] = jid

    # -- Error handling ------------------------------------------------------

    async def _slixmpp_exception_handler(self, exc) -> None:
        logger.error("XMPP: slixmpp exception: %s", exc, exc_info=True)


# -- Registration

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
