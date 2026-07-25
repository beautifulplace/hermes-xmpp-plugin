"""XMPP platform adapter for Hermes Agent.

Provides OMEMO-encrypted messaging, inbound/outbound media, typing indicators,
read receipts, and gateway restart notifications.
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from hermes_constants import get_hermes_home
from slixmpp import JID, ClientXMPP, Message

from .middlewares import InboundContext, MiddlewarePipeline
from .xmpp_utils import mime_from_extension

logger = logging.getLogger(__name__)


@dataclass
class XMPPConfig:
    user_jid: str
    password: str
    server: Optional[str] = None
    port: int = 5222
    omemo_enabled: bool = True
    avatar_path: Optional[str] = None
    allowed_contacts: Optional[List[str]] = None
    rooms: Optional[List[str]] = None


class XMPPAdapter(BasePlatformAdapter):
    platform_name = "xmpp"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.cfg = cfg
        xmpp_cfg = cfg.get("xmpp", {})
        extra = cfg.get("extra", {})

        self.user_jid: Optional[str] = xmpp_cfg.get("user_jid") or extra.get("user_jid")
        self.password: Optional[str] = os.getenv("XMPP_PASSWORD") or xmpp_cfg.get("password") or extra.get("password")
        self.server: Optional[str] = xmpp_cfg.get("server") or extra.get("server")
        self.port: int = xmpp_cfg.get("port") or extra.get("port") or 5222
        self.omemo_enabled = parse_bool(
            os.getenv("XMPP_OMEMO_ENABLED") or xmpp_cfg.get("omemo_enabled"), True
        )
        self.avatar_path: Optional[str] = xmpp_cfg.get("avatar_path") or extra.get("avatar_path")
        self.rooms: List[str] = self._split_list(xmpp_cfg.get("rooms", []))

        raw_allowed = xmpp_cfg.get("allowed_contacts") or extra.get("allowed_contacts")
        self.allowed_contacts: Set[str] = set(
            raw_allowed
            if isinstance(raw_allowed, (list, tuple))
            else ([raw_allowed.strip()] if isinstance(raw_allowed, str) and raw_allowed.strip() else [])
        )

        self.client: Optional[ClientXMPP] = None
        self._http = httpx.AsyncClient(timeout=300.0, follow_redirects=True)
        self._session_started_event = asyncio.Event()
        self._omemo_ready_event = asyncio.Event()
        self._shutting_down = False
        self._active_client: Optional[Any] = None
        self._xmpp_background_tasks: Set[asyncio.Task] = set()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._avatar_republish_task: Optional[asyncio.Task] = None
        self._internal_reconnect_task: Optional[asyncio.Task] = None
        self._last_activity: float = 0.0
        self._ping_interval = 60.0
        self._ping_timeout = 30.0

        self._voice_reply_chats: Set[str] = set()
        self._last_resources: Dict[str, str] = {}
        self._omemo_chats: Set[str] = set()
        self._auto_tts_default = parse_bool(cfg.get("voice", {}).get("auto_tts"), True)

        self._inbound_pipeline = MiddlewarePipeline.build(self)
        self.omemo_storage_path: Optional[Path] = None

    @staticmethod
    def _split_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(v).strip() for v in value if str(v).strip()]

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.user_jid or not self.password:
            logger.error("XMPP: user_jid and password are required")
            return False

        if is_reconnect and self.client is not None:
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
            self._active_client = self.client

            self.client.register_plugin("xep_0030")
            self.client.register_plugin("xep_0004")
            self.client.register_plugin("xep_0060")
            self.client.register_plugin("xep_0066")
            self.client.register_plugin("xep_0054")
            self.client.register_plugin("xep_0084")
            self.client.register_plugin("xep_0153")
            self.client.register_plugin("xep_0085")
            self.client.register_plugin("xep_0163")
            self.client.register_plugin("xep_0280")
            self.client.register_plugin("xep_0333")
            self.client.register_plugin("xep_0334")
            self.client.register_plugin("xep_0363")
            self.client.register_plugin("xep_0199")

            if self.omemo_enabled:
                self.omemo_storage_path = get_hermes_home() / "sessions" / "omemo.json"
                self.client.add_event_handler("omemo_initialized", self._omemo_initialized)
                self._configure_omemo()

            self.client.add_event_handler("session_start", self._session_start)
            self.client.add_event_handler("message", self._on_message)
            self.client.add_event_handler("presence", self._on_presence)
            self.client.add_event_handler("exception", self._slixmpp_exception_handler)
            self._current_disconnected_handler = self._make_disconnected_handler(self.client)
            self.client.add_event_handler("disconnected", self._current_disconnected_handler)

            logger.info("XMPP: connecting as %s to %s:%s ...", self.user_jid, self.server or "(auto)", self.port)
            connect_future = self.client.connect(host=self.server or None, port=self.port)
            if connect_future is not None:
                self._xmpp_background_tasks.add(asyncio.create_task(self._watch_client_future(connect_future)))

            await asyncio.wait_for(self._session_started_event.wait(), timeout=30.0)
            self._mark_connected()
            self._last_activity = asyncio.get_event_loop().time()
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            asyncio.create_task(self._finish_setup())
            return True
        except asyncio.TimeoutError:
            logger.error("XMPP: session_start did not arrive within 30s")
            return False
        except Exception as e:
            logger.error("XMPP: failed to connect as %s — %s", self.user_jid, e)
            return False

    def _configure_omemo(self) -> bool:
        try:
            sys.path.insert(0, str(Path(__file__).parent / "deps"))
            from slixmpp_omemo import Plugin as OMEMOPlugin
            from slixmpp_omemo import omemo

            self._omemo = omemo
            self.client.register_plugin(
                OMEMOPlugin,
                {
                    "storage": self.omemo_storage_path,
                    "cache_own_devices": True,
                    "clear_on_lost_session": False,
                },
            )
            logger.info("XMPP: OMEMO plugin enabled")
            return True
        except Exception as e:
            logger.error("XMPP: OMEMO plugin failed to load: %s", e)
            return False

    def _omemo_plugin(self):
        try:
            return self.client.plugin.get("omemo", None) if self.client else None
        except Exception:
            return None

    async def _omemo_initialized(self, _event=None):
        logger.info("XMPP: OMEMO initialized and device list published")
        self._omemo_ready_event.set()

    async def _session_start(self, _event=None):
        logger.info("XMPP: session started for %s", self.user_jid)
        self._session_started_event.set()
        try:
            self.client.send_presence()
            self.client.get_roster()
        except Exception as exc:
            logger.debug("XMPP: session start presence/roster error: %s", exc)

    async def _finish_setup(self):
        if self.omemo_enabled:
            try:
                await asyncio.wait_for(self._omemo_ready_event.wait(), timeout=30.0)
                logger.info("XMPP: OMEMO ready")
            except asyncio.TimeoutError:
                logger.warning("XMPP: OMEMO did not signal readiness within 30s")
        if self.avatar_path:
            logger.info("XMPP: publishing avatar from %s", self.avatar_path)
            try:
                await self._publish_avatar()
            except Exception as exc:
                logger.debug("XMPP: initial avatar publish failed: %s", exc)
            self._schedule_avatar_republish()

    def _schedule_avatar_republish(self):
        async def _republish():
            await asyncio.sleep(300.0)
            if self.is_connected and self.avatar_path:
                try:
                    await self._publish_avatar()
                except Exception as exc:
                    logger.debug("XMPP: avatar republish failed: %s", exc)
        self._avatar_republish_task = asyncio.create_task(_republish())

    async def _publish_avatar(self):
        # Stub: implement vCard/PEP avatar publishing if needed.
        pass

    async def _keepalive_loop(self) -> None:
        while self.is_connected:
            await asyncio.sleep(self._ping_interval)
            if not self.is_connected or self.client is None:
                break
            try:
                self.client.send_raw(" ")
                self._last_activity = asyncio.get_event_loop().time()
                ping = self.client.plugin.get("xep_0199", None)
                if ping is not None:
                    try:
                        await asyncio.wait_for(ping.send_ping(jid=self.client.boundjid.bare), timeout=self._ping_timeout)
                        self._last_activity = asyncio.get_event_loop().time()
                    except asyncio.TimeoutError:
                        logger.debug("XMPP: keepalive ping timed out; not treating as disconnect")
                    except Exception as exc:
                        logger.debug("XMPP: keepalive ping failed: %s", exc)
            except Exception as exc:
                logger.warning("XMPP: keepalive send failed: %s", exc)

    async def _watch_client_future(self, future) -> None:
        try:
            await future
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("XMPP: client future ended with error: %s", exc)
        if self.is_connected:
            logger.warning("XMPP: client future ended while still marked connected")
            await self._mark_and_recover("client_future_done", "slixmpp connection future ended")

    def _make_disconnected_handler(self, client: Any):
        async def handler(event):
            if self.client is not client:
                return
            logger.warning("XMPP: disconnected event received; event=%s", event)
            if self._shutting_down:
                return
            if self.is_connected:
                await self._mark_and_recover("disconnected", str(event))
        return handler

    async def _mark_and_recover(self, code: str, message: str) -> None:
        if not self.is_connected:
            return
        logger.warning("XMPP: connection lost (%s: %s); scheduling reconnect", code, message)
        self._mark_disconnected(code=code, message=message)
        if self._internal_reconnect_task and not self._internal_reconnect_task.done():
            return
        self._internal_reconnect_task = asyncio.create_task(self._recover_connection())

    async def _recover_connection(self) -> None:
        delays = [5.0, 10.0, 20.0]
        for attempt, delay in enumerate(delays, 1):
            await asyncio.sleep(delay)
            if self._shutting_down or self.is_connected:
                return
            logger.info("XMPP: reconnect attempt %d/%d", attempt, len(delays))
            try:
                await self._cleanup_client()
                result = await self.connect(is_reconnect=True)
                if result:
                    logger.info("XMPP: reconnect succeeded")
                    return
            except Exception as exc:
                logger.warning("XMPP: reconnect attempt %d failed: %s", attempt, exc)
        logger.warning("XMPP: reconnect exhausted; adapter will remain disconnected")

    async def disconnect(self) -> None:
        self._shutting_down = True
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

        client = self.client
        self.client = None
        self._active_client = None
        if client is None:
            return

        try:
            client.del_event_handler("session_start", self._session_start)
            client.del_event_handler("message", self._on_message)
            client.del_event_handler("presence", self._on_presence)
            client.del_event_handler("exception", self._slixmpp_exception_handler)
            if hasattr(self, "_current_disconnected_handler"):
                client.del_event_handler("disconnected", self._current_disconnected_handler)
            client.del_event_handler("omemo_initialized", self._omemo_initialized)
        except Exception as exc:
            logger.debug("XMPP: error removing event handlers: %s", exc)

        try:
            client.disconnect(wait=False)
        except Exception:
            pass

    async def _slixmpp_exception_handler(self, event):
        logger.warning("XMPP: slixmpp exception: %s", event)

    async def _on_presence(self, presence):
        if presence.get_type() in (None, "available") and presence.get_from():
            jid = presence.get_from()
            bare = str(jid.bare)
            full = str(jid)
            if bare in self.allowed_contacts or not self.allowed_contacts:
                self._last_resources[bare] = full
                logger.debug("XMPP: cached resource for %s: %s", bare, full)

    async def _on_message(self, msg: Message):
        if msg["type"] not in ("chat", "normal"):
            return
        sender = msg.get_from()
        if not sender:
            return
        bare = str(sender.bare)
        if self.allowed_contacts and bare not in self.allowed_contacts:
            logger.debug("XMPP: ignoring message from %s", bare)
            return

        self._last_resources[bare] = str(sender)
        ctx = InboundContext(adapter=self, msg=msg, sender_bare=bare, sender_full=str(sender))
        try:
            await self._inbound_pipeline.run(ctx)
        except Exception as exc:
            logger.exception("XMPP: inbound pipeline failed: %s", exc)
            return

        if ctx.event is not None:
            self._emit_event(ctx.event)

    def _emit_event(self, event: MessageEvent):
        for handler in self._message_handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.exception("XMPP: message handler error: %s", exc)

    # -- Sending -------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self.client is None or not self.is_connected:
            logger.error("XMPP: cannot send, not connected")
            return SendResult(success=False, error="not connected")

        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        recipient_bare = str(recipient.bare)
        if recipient_bare in self._voice_reply_chats:
            self._voice_reply_chats.discard(recipient_bare)

        force_omemo = self.omemo_enabled and recipient_bare in self._omemo_chats
        if force_omemo:
            return await self._send_text(recipient.bare, content)

        cached_resource = self._last_resources.get(recipient_bare)
        if cached_resource:
            try:
                recipient = JID(cached_resource)
            except Exception as exc:
                logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        return await self._send_text(recipient, content)

    async def _send_text(self, recipient: JID, text: str) -> SendResult:
        chunk_size = 2000
        chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
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
                        msg.set_to(recipient)
                        msg.set_from(self.client.boundjid)
                        encrypted, _errors = await omemo.encrypt_message(
                            msg,
                            recipient_jids={recipient},
                            identifier=recipient_bare,
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


    async def _send_displayed_marker(self, recipient: str, message_id: str) -> None:
        if self.client is None or not self.is_connected:
            return
        try:
            jid = JID(recipient)
            marker_plugin = self.client.plugin.get("xep_0333", None)
            if marker_plugin is not None:
                marker_plugin.send_marker(jid, message_id, "displayed")
                logger.info("XMPP: sent displayed marker to %s for %s", recipient, message_id)
        except Exception as exc:
            logger.debug("XMPP: displayed marker send failed: %s", exc)

    async def send_typing(self, chat_id: str) -> None:
        await self._send_chat_state(chat_id, "composing")

    async def stop_typing(self, chat_id: str) -> None:
        await self._send_chat_state(chat_id, "active")

    async def _send_chat_state(self, chat_id: str, state: str) -> None:
        if self.client is None or not self.is_connected:
            return
        try:
            recipient = JID(chat_id)
            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["chat_state"] = state
            msg.send()
        except Exception as exc:
            logger.debug("XMPP: chat state send failed: %s", exc)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        recipient_bare = str(recipient.bare)
        cached_resource = self._last_resources.get(recipient_bare)
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

        if ext == ".mp3":
            try:
                converted = Path(tempfile.gettempdir()) / f"voice_{uuid.uuid4().hex}.m4a"
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(audio_path_obj), "-c:a", "aac", "-b:a", "32k", str(converted),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0 and converted.exists():
                    audio_path_obj = converted
                    audio_bytes = audio_path_obj.read_bytes()
                    ext = ".m4a"
                    converted_created = True
                else:
                    logger.warning("XMPP: ffmpeg mp3->m4a failed: %s", stderr.decode()[:200])
            except Exception as exc:
                logger.warning("XMPP: could not convert mp3 to m4a: %s", exc)

        content_type = mime_from_extension(ext)
        filename = f"voice_{uuid.uuid4().hex}{ext}"
        url = await self._upload_encrypted_media(audio_bytes, filename, content_type)
        if not url:
            url = await self._upload_file(audio_bytes, filename, content_type)
        if not url:
            return SendResult(success=False, error="HTTP file upload failed")

        if converted_created:
            try:
                audio_path_obj.unlink(missing_ok=True)
            except OSError:
                pass

        return await self._send_media_message(recipient, url, audio_bytes, filename, content_type, caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            recipient = JID(chat_id)
        except Exception as exc:
            logger.error("XMPP: invalid recipient JID %s: %s", chat_id, exc)
            return SendResult(success=False, error="invalid recipient jid")

        recipient_bare = str(recipient.bare)
        cached_resource = self._last_resources.get(recipient_bare)
        if cached_resource:
            try:
                recipient = JID(cached_resource)
            except Exception as exc:
                logger.warning("XMPP: could not use cached resource %s: %s", cached_resource, exc)

        image_path_obj = Path(image_path)
        if not image_path_obj.exists():
            return SendResult(success=False, error=f"image file not found: {image_path}")

        image_bytes = image_path_obj.read_bytes()
        ext = image_path_obj.suffix.lower() or ".jpg"
        content_type = mime_from_extension(ext)
        filename = f"image_{uuid.uuid4().hex}{ext}"
        url = await self._upload_encrypted_media(image_bytes, filename, content_type)
        if not url:
            url = await self._upload_file(image_bytes, filename, content_type)
        if not url:
            return SendResult(success=False, error="HTTP file upload failed")

        return await self._send_media_message(recipient, url, image_bytes, filename, content_type, caption)

    async def _send_media_message(
        self,
        recipient: JID,
        url: str,
        data: bytes,
        filename: str,
        content_type: str,
        caption: Optional[str],
    ) -> SendResult:
        msg = self.client.make_message(mto=recipient, mtype="chat")
        msg["body"] = caption if caption else url
        msg["id"] = self.client.new_id()

        if url.startswith("aesgcm://"):
            try:
                ns_share = "urn:xmpp:share:1"
                ns_sshare = "urn:xmpp:sfs:0"
                ns_oob = "jabber:x:oob"
                sfs = ET.Element("{" + ns_sshare + "}file-sharing")
                file_el = ET.SubElement(sfs, "{" + ns_share + "}file")
                ET.SubElement(file_el, "{" + ns_share + "}name").text = filename
                ET.SubElement(file_el, "{" + ns_share + "}media-type").text = content_type
                ET.SubElement(file_el, "{" + ns_share + "}size").text = str(len(data))
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
                    logger.info("XMPP: OMEMO media message sent to %s", recipient.bare)
                    return SendResult(success=True)
            except Exception as exc:
                logger.warning("XMPP: OMEMO media send failed (%s); falling back", exc)

        msg.send()
        logger.info("XMPP: media message sent to %s", recipient.bare)
        return SendResult(success=True)

    async def _upload_encrypted_media(self, data: bytes, filename: str, content_type: str) -> Optional[str]:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            key = AESGCM.generate_key(bit_length=256)
            iv = os.urandom(12)
            aesgcm = AESGCM(key)
            encrypted = aesgcm.encrypt(iv, data, None)

            url = await self._upload_file(encrypted, filename, "application/octet-stream")
            if url:
                hex_fragment = (iv + key).hex()
                return f"aesgcm://{url.replace('https://', '').replace('http://', '')}#{hex_fragment}"
        except Exception as exc:
            logger.warning("XMPP: encrypted media upload failed: %s", exc)
        return None

    async def _upload_file(self, data: bytes, filename: str, content_type: str) -> Optional[str]:
        try:
            upload_plugin = self.client.plugin.get("xep_0363", None)
            if upload_plugin is None:
                logger.warning("XMPP: HTTP file upload plugin not available")
                return None
            url = await upload_plugin.upload_file(data, filename=filename, content_type=content_type)
            return url
        except Exception as exc:
            logger.warning("XMPP: file upload failed: %s", exc)
            return None


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def check_requirements() -> bool:
    import importlib.util
    return all(
        importlib.util.find_spec(pkg) is not None
        for pkg in ("slixmpp", "httpx", "cryptography")
    )


def is_connected(adapter) -> bool:
    return getattr(adapter, "is_connected", False)


def validate_config(cfg: dict) -> list[str]:
    errors = []
    xmpp = cfg.get("xmpp", {})
    extra = cfg.get("extra", {})
    if not (xmpp.get("user_jid") or extra.get("user_jid")):
        errors.append("XMPP user_jid is required")
    return errors


def interactive_setup(cfg: dict) -> dict:
    return cfg


def _env_enablement(env: dict) -> bool:
    return bool(env.get("XMPP_USER_JID") and env.get("XMPP_PASSWORD"))


def _apply_yaml_config(cfg: dict, xmpp_cfg: dict) -> None:
    cfg["xmpp"] = xmpp_cfg


def _standalone_send(*args, **kwargs):
    raise NotImplementedError("standalone send not implemented")


def register(ctx):
    ctx.register_platform(
        name="xmpp",
        label="XMPP",
        adapter_factory=lambda cfg: XMPPAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["XMPP_USER_JID", "XMPP_PASSWORD"],
        install_hint="pip install slixmpp slixmpp-omemo httpx cryptography",
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
        platform_hint="You are chatting via XMPP. Use plain text responses.",
    )
