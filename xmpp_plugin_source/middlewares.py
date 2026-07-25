"""Inbound message middlewares for the Hermes XMPP platform adapter.

The XMPP adapter uses a lightweight middleware pipeline inspired by the Yuanbao
adapter. Each middleware receives an InboundContext and calls next_fn() to pass
control down the chain. This keeps message handling concerns separated and
testable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gateway.platforms.base import MessageEvent, MessageType
from slixmpp.jid import JID
from slixmpp.stanza import Message
from tools.transcription_tools import transcribe_audio

logger = logging.getLogger(__name__)


@dataclass
class InboundContext:
    """Mutable context flowing through the inbound middleware pipeline."""

    adapter: Any  # XMPPAdapter (forward ref avoids circular import)
    msg: Message

    sender_full: JID = field(default_factory=lambda: JID(""))
    sender_bare: str = ""
    body: str = ""
    is_encrypted: bool = False
    is_voice: bool = False
    media_path: Optional[Any] = None
    auto_sethome_performed: bool = False
    event: Optional[MessageEvent] = None
    reply_text: str = ""  # Optional reply injected by middleware
    markable_requested: bool = False


class InboundMiddleware:
    """Base class for inbound pipeline middlewares."""

    name: str = ""

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        raise NotImplementedError

    async def __call__(self, ctx: InboundContext, next_fn: Callable) -> None:
        await self.handle(ctx, next_fn)


class ValidateMiddleware(InboundMiddleware):
    """Drop non-chat messages and self-messages."""

    name = "validate"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        msg = ctx.msg
        if msg.get("type") not in ("chat", "normal"):
            return
        sender = msg["from"]
        if not sender:
            return
        ctx.sender_full = JID(sender)
        ctx.sender_bare = str(ctx.sender_full.bare)
        ctx.adapter._last_resources[ctx.sender_bare] = str(ctx.sender_full)
        markable_el = msg.xml.find(".//{urn:xmpp:chat-markers:0}markable")
        ctx.markable_requested = markable_el is not None
        if ctx.sender_bare == str(JID(ctx.adapter.user_jid).bare):
            logger.debug("XMPP: ignoring self-message from %s", ctx.sender_bare)
            return
        await next_fn()


class OMEMODecryptMiddleware(InboundMiddleware):
    """Decrypt OMEMO payloads; otherwise keep plaintext body."""

    name = "omemo-decrypt"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        msg = ctx.msg
        ctx.body = str(msg.get("body", "") or "").strip()

        omemo = ctx.adapter._omemo_plugin()
        if omemo is None or not ctx.adapter.omemo_enabled:
            await next_fn()
            return

        namespaces = ("eu.siacs.conversations.axolotl", "urn:xmpp:omemo:2")
        has_encrypted = any(
            msg.xml.find(f".//{{{ns}}}encrypted") is not None for ns in namespaces
        )
        if not has_encrypted:
            await next_fn()
            return

        try:
            decrypted, _device_info = await omemo.decrypt_message(msg)
            decrypted_body = str(decrypted.get("body", "") or "").strip()
            if decrypted_body:
                ctx.body = decrypted_body
                ctx.is_encrypted = True
                logger.info(
                    "XMPP: OMEMO decrypted message from %s: %d chars",
                    ctx.sender_bare,
                    len(ctx.body),
                )
        except Exception as exc:
            logger.warning("XMPP: OMEMO decrypt attempt failed: %s", exc)
            # Fall back to plaintext body (if any).

        await next_fn()


class MediaResolveMiddleware(InboundMiddleware):
    """Download/decrypt inbound media URLs and update the body accordingly."""

    name = "media-resolve"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        from .media import resolve_inbound_media
        from .xmpp_utils import extract_url, is_voice_url

        if not ctx.body:
            await next_fn()
            return

        url = extract_url(ctx.body) or ""
        is_voice = ctx.is_voice or is_voice_url(url, ctx.body)
        kind = "audio" if is_voice else "image"
        clean_body, path = await resolve_inbound_media(ctx.body, ctx.adapter._http, kind=kind)
        ctx.body = clean_body
        ctx.media_path = path
        if path:
            logger.info("XMPP: cached inbound %s media to %s", kind, path)
        await next_fn()


class VoiceDetectMiddleware(InboundMiddleware):
    """Mark audio-only messages as voice messages before media resolution."""

    name = "voice-detect"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        from .xmpp_utils import extract_url, is_voice_url

        if ctx.body:
            url = extract_url(ctx.body) or ""
            ctx.is_voice = is_voice_url(url, ctx.body)
        await next_fn()


class TranscribeVoiceMiddleware(InboundMiddleware):
    """Transcribe inbound voice messages so the LLM receives text."""

    name = "transcribe-voice"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        if not ctx.is_voice or not ctx.media_path:
            await next_fn()
            return

        try:
            result = await asyncio.to_thread(transcribe_audio, str(ctx.media_path))
            if isinstance(result, dict):
                transcript = result.get("transcript", "").strip()
                error = result.get("error", "")
            else:
                transcript = str(result).strip()
                error = ""

            if transcript:
                ctx.body = transcript
                logger.info(
                    "XMPP: transcribed voice message from %s: %r",
                    ctx.sender_bare,
                    transcript,
                )
            else:
                ctx.body = "(voice message could not be transcribed)"
                if error:
                    logger.warning("XMPP: voice transcription failed: %s", error)

            auto_tts_default = getattr(ctx.adapter, "_auto_tts_default", False)
            if auto_tts_default and ctx.sender_bare:
                ctx.adapter._voice_reply_chats.add(ctx.sender_bare)
                logger.info(
                    "XMPP: queued voice reply for chat %s (auto_tts_default=%s)",
                    ctx.sender_bare,
                    auto_tts_default,
                )
        except Exception as exc:
            logger.warning("XMPP: voice transcription error: %s", exc)
            ctx.body = "(voice message could not be transcribed)"

        await next_fn()


class ReadReceiptMiddleware(InboundMiddleware):
    """Send XEP-0333 displayed marker when the stanza requests one."""

    name = "read-receipt"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        await next_fn()
        if not ctx.markable_requested or not ctx.sender_bare or not ctx.msg.get("id"):
            return
        try:
            ctx.adapter._send_displayed_marker(ctx.sender_bare, ctx.msg["id"])
        except Exception as exc:
            logger.debug("XMPP: failed to send displayed marker: %s", exc, exc_info=True)


class AutoSethomeMiddleware(InboundMiddleware):
    """Designate the first authorized contact's bare JID as the XMPP home channel."""

    name = "auto-sethome"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        await next_fn()

        adapter = ctx.adapter
        if ctx.auto_sethome_performed or adapter._auto_sethome_done:
            return
        if not ctx.sender_bare:
            return
        if not adapter._sender_may_designate_home(ctx.sender_bare):
            return

        current_home = (adapter.home_channel or "").strip()
        if current_home:
            return

        try:
            adapter._set_home_channel(ctx.sender_bare)
            adapter._auto_sethome_done = True
            ctx.auto_sethome_performed = True
            ctx.reply_text = (
                f"I've set your JID ({ctx.sender_bare}) as my XMPP home channel. "
                "Future system messages and handoffs will come here. "
                "You can change this anytime with `/sethome` or by editing "
                "`platforms.xmpp.home_channel` in ~/.hermes/config.yaml."
            )
            logger.info("XMPP: auto-sethome designated %s as home channel", ctx.sender_bare)
        except Exception as exc:
            logger.warning("XMPP: auto-sethome failed: %s", exc)


class BuildEventMiddleware(InboundMiddleware):
    """Build the MessageEvent and hand it to the gateway."""

    name = "build-event"

    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        if not ctx.body:
            await next_fn()
            return

        msg_type = MessageType.VOICE if ctx.is_voice else MessageType.TEXT
        event = MessageEvent(
            text=ctx.body,
            message_type=msg_type,
            source=ctx.adapter._build_source(ctx.sender_bare),
            metadata={"resource": str(ctx.sender_full)},
        )
        ctx.event = event

        # If a middleware injected a reply, deliver it before calling the agent.
        if ctx.reply_text:
            await ctx.adapter.send(ctx.sender_bare, ctx.reply_text)

        await ctx.adapter.handle_message(event)
        await next_fn()


class InboundPipeline:
    """Simple onion-style middleware pipeline."""

    def __init__(self, middlewares: list[InboundMiddleware]):
        self._middlewares = middlewares

    async def run(self, ctx: InboundContext) -> None:
        index = 0

        async def next_fn() -> None:
            nonlocal index
            if index < len(self._middlewares):
                mw = self._middlewares[index]
                index += 1
                await mw(ctx, next_fn)

        await next_fn()
