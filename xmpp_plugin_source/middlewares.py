import asyncio
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

from gateway.platforms.base import MessageEvent, MessageType

from .media import cache_media, is_aesgcm_url
from .xmpp_utils import extract_url, is_voice_url

if TYPE_CHECKING:
    from .adapter import XMPPAdapter

logger = logging.getLogger(__name__)


@dataclass
class InboundContext:
    adapter: "XMPPAdapter"
    msg: Any
    sender_bare: str = ""
    sender_full: str = ""
    body: str = ""
    body_plain: str = ""
    is_voice: bool = False
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    markable_requested: bool = False
    event: Optional[MessageEvent] = None


class Middleware:
    async def handle(self, ctx: InboundContext) -> None:
        raise NotImplementedError


class MiddlewarePipeline:
    def __init__(self, middlewares: List[Middleware]):
        self.middlewares = middlewares

    async def run(self, ctx: InboundContext) -> None:
        for mw in self.middlewares:
            try:
                await mw.handle(ctx)
            except Exception as exc:
                logger.exception("XMPP middleware %s failed: %s", mw.__class__.__name__, exc)
                raise

    @classmethod
    def build(cls, adapter: "XMPPAdapter") -> "MiddlewarePipeline":
        return cls([
            ValidateMiddleware(),
            MarkableMiddleware(),
            OMEMODecryptMiddleware(),
            VoiceDetectMiddleware(),
            MediaResolveMiddleware(),
            TranscribeVoiceMiddleware(),
            ReadReceiptMiddleware(),
            AutoSethomeMiddleware(),
            BuildEventMiddleware(),
        ])


class ValidateMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        body = ctx.msg.get("body", "")
        if body is None:
            body = ""
        ctx.body = str(body)
        ctx.body_plain = ctx.body


class MarkableMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        try:
            xml = getattr(ctx.msg, "xml", None)
            if xml is not None:
                ctx.markable_requested = xml.find(".{http://jabber.shiguangqiu.top/ns/markers}markable") is not None
        except Exception as exc:
            logger.debug("XMPP: could not check markable: %s", exc)


class OMEMODecryptMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        if not ctx.adapter.omemo_enabled:
            return
        omemo = ctx.adapter._omemo_plugin()
        if omemo is None:
            return
        try:
            result = await omemo.decrypt_message(ctx.msg)
            if result is not None and hasattr(result, "body") and result.body:
                ctx.body = result.body
                ctx.adapter._omemo_chats.add(ctx.sender_bare)
                logger.info("XMPP: OMEMO decrypted message from %s: %d chars", ctx.sender_bare, len(ctx.body))
            else:
                logger.debug("XMPP: OMEMO decrypt produced no body from %s", ctx.sender_bare)
        except Exception as exc:
            logger.debug("XMPP: OMEMO decrypt failed for %s: %s", ctx.sender_bare, exc)


class VoiceDetectMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        url = extract_url(ctx.body)
        if url and is_voice_url(url):
            ctx.is_voice = True
            logger.debug("XMPP: detected voice URL from %s", ctx.sender_bare)


class MediaResolveMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        url = extract_url(ctx.body)
        if not url or not is_aesgcm_url(url):
            return
        try:
            local_path = await cache_media(url)
            if local_path and Path(local_path).exists():
                ctx.media_urls.append(local_path)
                mime, _ = mimetypes.guess_type(local_path)
                ctx.media_types.append(mime or "application/octet-stream")
                logger.info("XMPP: cached inbound media for %s: %s", ctx.sender_bare, local_path)
        except Exception as exc:
            logger.warning("XMPP: media resolve failed for %s: %s", ctx.sender_bare, exc)


class TranscribeVoiceMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        if not ctx.media_urls:
            return

        # Detect audio by content even if the URL heuristic missed it.
        audio_path = None
        for path, mime in zip(ctx.media_urls, ctx.media_types):
            if mime and mime.startswith("audio/"):
                audio_path = path
                ctx.is_voice = True
                break
            ext = Path(path).suffix.lower()
            if ext in {".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac", ".opus"}:
                audio_path = path
                ctx.is_voice = True
                break

        if not audio_path or not ctx.is_voice:
            return

        if not getattr(ctx.adapter, "_auto_tts_default", True):
            return

        try:
            from tools.transcription_tools import transcribe_audio
            transcript = await asyncio.to_thread(transcribe_audio, audio_path)
            if transcript:
                ctx.body = transcript
                ctx.adapter._voice_reply_chats.add(ctx.sender_bare)
                logger.info("XMPP: transcribed voice message from %s: %r", ctx.sender_bare, transcript)
        except Exception as exc:
            logger.warning("XMPP: voice transcription failed for %s: %s", ctx.sender_bare, exc)


class ReadReceiptMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        if not ctx.markable_requested:
            return
        try:
            await ctx.adapter._send_displayed_marker(ctx.sender_full, ctx.msg.get("id"))
        except Exception as exc:
            logger.debug("XMPP: read receipt send failed: %s", exc)


class AutoSethomeMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        # No-op placeholder; Hermes handles home chat routing elsewhere.
        pass


class BuildEventMiddleware(Middleware):
    async def handle(self, ctx: InboundContext) -> None:
        if ctx.media_urls:
            # If the only content is a media URL, clear body so the gateway inserts placeholders.
            if ctx.body and extract_url(ctx.body) and ctx.body.strip() == ctx.media_urls[0]:
                ctx.body = ""

        message_type = MessageType.TEXT
        if ctx.media_urls:
            first_mime = ctx.media_types[0] if ctx.media_types else ""
            if first_mime.startswith("image/"):
                message_type = MessageType.PHOTO
            elif first_mime.startswith("audio/"):
                message_type = MessageType.VOICE
            elif first_mime.startswith("video/"):
                message_type = MessageType.VIDEO
            else:
                message_type = MessageType.DOCUMENT

        ctx.event = MessageEvent(
            platform="xmpp",
            chat_id=ctx.sender_bare,
            user_id=ctx.sender_bare,
            message_id=ctx.msg.get("id") or "",
            text=ctx.body,
            message_type=message_type,
            media_urls=ctx.media_urls,
            media_types=ctx.media_types,
            raw_message=ctx.msg,
        )
