"""
OMEMO plugin subclass for the Hermes XMPP platform adapter.

This file provides a concrete implementation of slixmpp-omemo's XEP_0384
plugin, adapted for a server-side bot that is expected to run unattended.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import FrozenSet, Optional

from omemo.storage import JSONType, Just, Maybe, Nothing, Storage
from slixmpp_omemo import XEP_0384, TrustLevel

logger = logging.getLogger(__name__)


class JSONFileStorage(Storage):
    """
    OMEMO Storage implementation backed by a single JSON file in the Hermes home.

    The OMEMO library reads/writes small JSON blobs for identity keys, sessions,
    device lists, and trust decisions. We keep a simple in-memory cache with a
    synchronous atomic file write so that a gateway restart doesn't lose state.
    """

    def __init__(self, path: Path):
        # Caching is disabled because the underlying file is the source of truth
        # and we don't want stale in-memory values across code reloads / restarts.
        super().__init__(disable_cache=True)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}")
        try:
            self._data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            self._data = {}
            self._save()

    async def _load(self, key: str) -> Maybe[JSONType]:
        if key in self._data:
            return Just(self._data[key])
        return Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self._data[key] = value
        self._save()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._save()

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True, indent=2))
        tmp.replace(self._path)


class HermesOMEMO(XEP_0384):
    """
    Concrete OMEMO plugin for the Hermes XMPP adapter.

    Trust model:
      - XMPP_OMEMO_ALLOW_UNTRUSTED=true (default for BTBV): new devices are
        "blindly trusted" automatically. This is appropriate for a personal bot
        where you control both endpoints.
      - XMPP_OMEMO_ALLOW_UNTRUSTED=false: new devices must be manually approved.
        For an unattended server bot, this means replies to unknown devices will
        fail until you trust them. A tool/UI for managing trust is outside the
        scope of this adapter.
    """

    name = "hermes_omemo"
    description = "Hermes OMEMO Encryption"

    def __init__(self, xmpp, config: dict):
        super().__init__(xmpp, config)

        from hermes_constants import get_hermes_home

        self._allow_untrusted = bool(config.get("allow_untrusted", True))
        self._storage_path = Path(config.get("storage_path")) or (
            get_hermes_home() / "sessions" / "omemo.json"
        )
        self._storage = JSONFileStorage(self._storage_path)
        self._pending_manual_trust: asyncio.Queue[FrozenSet] = asyncio.Queue()

    @property
    def storage(self) -> Storage:
        return self._storage

    @property
    def _btbv_enabled(self) -> bool:
        return self._allow_untrusted

    async def _devices_blindly_trusted(
        self,
        blindly_trusted: FrozenSet,
        identifier: Optional[str] = None,
    ) -> None:
        """
        BTBV just accepted some devices. Log them so the operator can audit.
        """
        for device in blindly_trusted:
            logger.info(
                "OMEMO: device %s/%s blindly trusted (BTBV)",
                device.bare_jid,
                device.device_id,
            )

    async def _prompt_manual_trust(
        self,
        manually_trusted: FrozenSet,
        identifier: Optional[str] = None,
    ) -> None:
        """
        Manual trust fallback. For an unattended bot we cannot ask the user,
        so we either:
          - blindly trust if allow_untrusted is enabled, or
          - queue and warn so the operator knows replies to this device failed.
        """
        session_manager = await self.get_session_manager()
        for device in manually_trusted:
            if self._allow_untrusted:
                logger.info(
                    "OMEMO: auto-trusting %s/%s because XMPP_OMEMO_ALLOW_UNTRUSTED=true",
                    device.bare_jid,
                    device.device_id,
                )
                await session_manager.set_trust(
                    device.bare_jid,
                    device.device_id,
                    TrustLevel.BLINDLY_TRUSTED.value,
                )
            else:
                logger.warning(
                    "OMEMO: untrusted device %s/%s needs manual approval; "
                    "messages to it will fail until trusted",
                    device.bare_jid,
                    device.device_id,
                )
                await session_manager.set_trust(
                    device.bare_jid,
                    device.device_id,
                    TrustLevel.UNDECIDED.value,
                )
