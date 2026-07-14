import asyncio
import io
import logging
import os
import re
import tempfile
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


def _is_audio_url(url: str) -> bool:
    return any(url.lower().endswith(ext) for ext in (
        ".ogg", ".oga", ".mp3", ".m4a", ".webm", ".wav", ".opus", ".mp4"
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
      - Outgoing voice messages via text-to-speech + HTTP File Upload
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
        self.voice_reply = _parse_bool(
            os.getenv("XMPP_VOICE_REPLY") or extra.get("voice_reply"), False
        )
        self.voice_language = (
            os.getenv("XMPP_VOICE_LANGUAGE") or extra.get("voice_language") or "en-US"
        )
        self.voice_tts = (
            os.getenv("XMPP_VOICE_TTS") or extra.get("voice_tts") or "edge"
        ).lower()
        self.voice_model = (
            os.getenv("XMPP_VOICE_MODEL") or extra.get("voice_model") or "en-US-AriaNeural"
        )
        self.avatar_path = os.getenv("XMPP_AVATAR_PATH") or extra.get("avatar_path", "")
        self.voice_format = (
            os.getenv("XMPP_VOICE_FORMAT") or extra.get("voice_format") or "m4a"
        ).lower().lstrip(".")
        self.home_channel = os.getenv("XMPP_HOME_CHANNEL") or extra.get("home_channel", "")

        self._session_started_event = asyncio.Event()
        self.client: Optional[ClientXMPP] = None
        self._http = httpx.AsyncClient(timeout=300.0, follow_redirects=True)
        self._keepalive_task: Optional[asyncio.Task] = None
        self._avatar_republish_task: Optional[asyncio.Task] = None
        self._last_activity: float = 0.0
        self._ping_interval = 30.0
        self._ping_timeout = 10.0

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

            logger.info("XMPP: connecting as %s ...", self.user_jid)

            # slixmpp connect() returns a Future that completes when the
            # connection *ends*; do not await it. Wait for session_start instead.
            self.client.connect(host=self.server or None, port=self.port)

            try:
                await asyncio.wait_for(
                    self._session_started_event.wait(), timeout=30.0
                )
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
                    self._set_fatal_error("ping_failed", str(exc), retryable=True)
                break

    async def _on_disconnected(self, event):
        logger.warning("XMPP: disconnected event received")
        if self.is_connected:
            self._set_fatal_error("disconnected", "XMPP stream disconnected", retryable=True)

    async def disconnect(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        self._mark_disconnected()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        await self._http.aclose()

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

        text = content
        if self.voice_reply and text:
            voice_result = await self._send_voice_reply(recipient, text)
            if voice_result.success:
                if not _parse_bool(
                    os.getenv("XMPP_VOICE_ONLY") or (metadata or {}).get("voice_only"),
                    False,
                ):
                    await self._send_text(recipient, text)
                return voice_result

        return await self._send_text(recipient, text)

    async def _send_text(self, recipient: JID, text: str) -> SendResult:
        # XMPP servers and clients often choke on very large stanzas.
        # Split response into smaller, manageable chunks.
        chunk_size = 2000
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

        try:
            for i, chunk in enumerate(chunks):
                msg = self.client.make_message(mto=recipient, mtype="chat")
                msg["body"] = chunk
                msg["id"] = self.client.new_id()

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
                            logger.info("XMPP: OMEMO message chunk %d/%d sent to %s", i+1, len(chunks), recipient.bare)
                            if i < len(chunks) - 1:
                                await asyncio.sleep(0.2)
                            continue
                    except Exception as exc:
                        logger.warning("XMPP: OMEMO send failed (%s); falling back to plaintext", exc)

                msg.send()
                logger.info("XMPP: plaintext message chunk %d/%d sent to %s", i+1, len(chunks), recipient.bare)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.2)

            return SendResult(success=True)
        except Exception as exc:
            logger.exception("XMPP: failed to send message to %s: %s", recipient.bare, exc)
            return SendResult(success=False, error=str(exc))

    async def _send_voice_reply(self, recipient: JID, text: str) -> SendResult:
        try:
            audio_bytes = await self._synthesize_speech(text)
            if not audio_bytes:
                return SendResult(success=False, error="TTS produced no audio")

            # edge-tts outputs MP3 regardless of filename suffix.
            fmt = (self.voice_format or "m4a").lower().lstrip(".")
            mime_map = {"m4a": "audio/mp4", "mp4": "audio/mp4", "opus": "audio/opus", "ogg": "audio/ogg", "oga": "audio/ogg"}
            ext_map = {"m4a": ".m4a", "mp4": ".m4a", "opus": ".opus", "ogg": ".ogg", "oga": ".oga"}
            content_type = mime_map.get(fmt, "audio/mp4")
            ext = ext_map.get(fmt, ".m4a")
            filename = f"voice_{uuid.uuid4().hex}{ext}"
            # Prefer OMEMO media sharing (aesgcm://) for inline playback in Conversations.
            url = await self._upload_encrypted_media(audio_bytes, filename, content_type)
            logger.debug("XMPP: voice upload url=%s", url)
            if not url:
                # Fallback to plain HTTPS upload.
                url = await self._upload_file(audio_bytes, filename, content_type)
                logger.debug("XMPP: voice fallback url=%s mime=%s", url, content_type)
            if not url:
                return SendResult(success=False, error="HTTP file upload failed")

            msg = self.client.make_message(mto=recipient, mtype="chat")
            msg["body"] = url
            msg["id"] = self.client.new_id()

            # Attach XEP-0385 Stateless File Sharing metadata for inline media.
            if url.startswith("aesgcm://"):
                try:
                    ns_sshare = "urn:xmpp:sfs:0"
                    ns_share  = "urn:xmpp:share:1"
                    ns_oob    = "jabber:x:oob"

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
        except Exception as exc:
            logger.exception("XMPP: failed to send voice reply: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _synthesize_speech(self, text: str) -> Optional[bytes]:
        if self.voice_tts == "melo":
            return await self._synthesize_speech_melo(text)
        return await self._synthesize_speech_edge(text)


    async def _synthesize_speech_edge(self, text: str) -> Optional[bytes]:
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, self.voice_model)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                mp3_path = Path(tmp.name)
            await communicate.save(str(mp3_path))
            return await self._transcode_to_voice_format(mp3_path)
        except Exception as exc:
            logger.warning("XMPP: TTS synthesis failed: %s", exc)
            return None


    async def _synthesize_speech_melo(self, text: str) -> Optional[bytes]:
        try:
            from melo.api import TTS

            loop = asyncio.get_running_loop()
            # MeloTTS is CPU-bound; run it in a thread pool so we don't block the gateway loop.
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                wav_path = tmpdir / "melo.wav"
                model = TTS(language="EN", use_hf=True)
                speaker_ids = model.hps.data.spk2id
                # Default to a neutral English speaker. Use XMPP_VOICE_MODEL env/config if provided.
                speaker = self.voice_model if self.voice_model in speaker_ids else "EN-Default"
                await loop.run_in_executor(
                    None,
                    model.tts_to_file,
                    text,
                    speaker_ids[speaker],
                    str(wav_path),
                    speed=1.0,
                )
                if not wav_path.exists() or wav_path.stat().st_size == 0:
                    logger.warning("XMPP: MeloTTS produced no audio")
                    return None
                return await self._transcode_to_voice_format(wav_path)
        except Exception as exc:
            logger.warning("XMPP: MeloTTS synthesis failed: %s", exc)
            return None


    async def _transcode_to_voice_format(self, source_path: Path) -> Optional[bytes]:
        try:
            fmt = self.voice_format
            if fmt not in ("m4a", "mp4", "opus", "ogg", "oga"):
                fmt = "m4a"

            ext_map = {"m4a": ".m4a", "mp4": ".m4a", "opus": ".opus", "ogg": ".ogg", "oga": ".oga"}
            codec_map = {"m4a": "aac", "mp4": "aac", "opus": "libopus", "ogg": "libopus", "oga": "libopus"}

            ext = ext_map[fmt]
            codec = codec_map[fmt]
            out_path = source_path.with_suffix(ext)

            args = ["ffmpeg", "-y", "-i", str(source_path), "-c:a", codec, "-b:a", "24k"]
            if codec == "libopus":
                args.extend(["-vbr", "on", "-application", "voip"])
            args.append(str(out_path))

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            source_path.unlink(missing_ok=True)

            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                data = out_path.read_bytes()
                out_path.unlink(missing_ok=True)
                return data

            logger.warning("XMPP: ffmpeg %s transcode failed: %s", fmt, stderr.decode().strip()[-200:] if stderr else "unknown")
            return None
        except Exception as exc:
            logger.warning("XMPP: audio transcode failed: %s", exc)
            return None


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
            return
        try:
            msg = self.client.make_message(mto=JID(chat_id), mtype="chat")
            msg["chat_state"] = "composing"
            msg.send()
            logger.debug("XMPP: typing indicator sent to %s", chat_id)
        except Exception as exc:
            logger.debug("XMPP: typing indicator send failed: %s", exc)

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        if not self.typing_indicator or self.client is None:
            return
        try:
            msg = self.client.make_message(mto=JID(chat_id), mtype="chat")
            msg["chat_state"] = "active"
            msg.send()
            logger.debug("XMPP: stop typing sent to %s", chat_id)
        except Exception as exc:
            logger.debug("XMPP: stop typing send failed: %s", exc)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # -- Media ---------------------------------------------------------------

    async def _download_url(self, url: str) -> Optional[bytes]:
        try:
            if url.startswith("aesgcm://"):
                return await self._download_aesgcm(url)
            async with self._http as client:
                resp = await client.get(url)
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
            async with self._http as client:
                resp = await client.get(https_url)
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
        logger.info("XMPP: session started for %s", self.user_jid)
        try:
            if self.client:
                self.client.send_presence()
                self.client.get_roster()
        except Exception:
            pass
        self._session_started_event.set()

    async def _omemo_initialized(self, event=None):
        logger.info("XMPP: OMEMO initialized and device list published")
        self._omemo_ready_event.set()

    async def _on_message(self, msg: Message):
        try:
            if msg["type"] not in ("chat", "normal"):
                return

            sender_jid = msg["from"]
            if not sender_jid:
                return
            sender_bare = str(JID(sender_jid).bare)
            if sender_bare == JID(self.user_jid).bare:
                return

            body = msg.get("body", "").strip()
            encrypted = False

            omemo = self._omemo_plugin()
            if omemo is not None and self.omemo_enabled:
                try:
                    decrypted, _device_info = await omemo.decrypt_message(msg)
                    body_text = str(decrypted.get("body", "") or "").strip()
                    if body_text and body_text != body:
                        body = body_text
                        encrypted = True
                        logger.info("XMPP: OMEMO decrypted message from %s: %s chars", sender_bare, len(body))
                except Exception as exc:
                    logger.warning("XMPP: OMEMO decrypt attempt failed: %s", exc, exc_info=True)

            if not body:
                return

            # Send read receipt for messages that request it.
            try:
                if msg.xml.find(".//{urn:xmpp:chat-markers:0}markable") is not None:
                    await self._send_displayed_marker(sender_bare, msg.get("id", self.client.new_id()))
            except Exception:
                pass

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
            if url:
                logger.debug("XMPP: detected URL in message: %s", url)
                if url.startswith("aesgcm://"):
                    data = await self._download_aesgcm(url)
                else:
                    data = await self._download_url(url)
                logger.debug("XMPP: downloaded %d bytes from %s", len(data) if data else 0, url)
                if data:
                    if _guess_audio_is_voice(url, body):
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

                logger.debug("XMPP: cached media path=%s", media_path)

            # If media was cached, replace the URL in the body with the local
            # path so downstream tools analyse the actual file, not the link.
            display_text = body
            if media_path and url:
                display_text = body.replace(url, media_path)
                if display_text == body:
                    # URL not in body (e.g. only in oob); use a direct note.
                    display_text = f"{body}\n[Attached media: {media_path}]".strip()

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
                metadata={"encrypted": encrypted, "media_url": url, "media_path": media_path},
            )
            await self.handle_message(event)
        except Exception:
            logger.exception("XMPP: unhandled error in message handler")

    async def _send_displayed_marker(self, to_jid: str, message_id: str) -> None:
        try:
            marker_plugin = self.client.plugin.get("xep_0333", None)
            if marker_plugin is None:
                logger.debug("XMPP: xep_0333 plugin not available")
                return
            marker_plugin.send_marker(mto=JID(to_jid), id=message_id, marker="displayed", mtype="chat")
            logger.debug("XMPP: sent displayed marker to %s for %s", to_jid, message_id)
        except Exception as exc:
            logger.debug("XMPP: failed to send displayed marker: %s", exc)

    def _cache_media(self, data: bytes, kind: str = "image", ext: Optional[str] = None) -> Optional[str]:
        try:
            if ext is None:
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
    "typing_indicator",
    "voice_reply",
    "voice_language",
    "voice_model",
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
        install_hint="pip install slixmpp slixmpp-omemo httpx Pillow cryptography edge-tts melotts",
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
