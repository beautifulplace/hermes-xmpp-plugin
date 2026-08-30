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
            self._write_sync("{}")
        self._save_lock = asyncio.Lock()

    async def _load(self, key: str) -> Maybe[JSONType]:
        if key in self._data:
            return Just(self._data[key])
        return Nothing()

    async def _store(self, key: str, value: JSONType) -> None:
        self._data[key] = value
        await self._save()

    async def _delete(self, key: str) -> None:
        self._data.pop(key, None)
        await self._save()

    def _write_sync(self, data: str) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(self._path)

    async def _save(self) -> None:
        # Serialize under the lock (a consistent snapshot), then offload the
        # blocking disk write to a worker thread so the gateway's event loop
        # never stalls on I/O. The OMEMO state file grows over time (sessions,
        # device keys) and was being rewritten synchronously on every encrypt,
        # blocking the loop long enough to trip the liveness watchdog.
        async with self._save_lock:
            data = json.dumps(self._data, sort_keys=True, indent=2)
            await asyncio.to_thread(self._write_sync, data)

    def sanitize_device_lists(self) -> int:
        """Drop device IDs from device lists whose per-device keys are missing.

        The OMEMO library's SessionManager.create iterates every device ID in
        each /devices/<jid>/list and hard-crashes ("Maybe.fromJust: Nothing")
        on any entry whose /namespaces key does not exist. An earlier version
        of the pruning code removed per-device keys but left the IDs in the
        list, which broke OMEMO startup. This runs synchronously at plugin
        construction (before the session manager builds), repairs such
        corruption, and also drops stray per-device keys that belong to no
        listed device.

        Returns the number of keys removed.
        """
        removed = 0
        for key in list(self._data.keys()):
            if not (key.startswith("/devices/") and key.endswith("/list")):
                continue
            jid = key.split("/")[2]
            device_ids = list(self._data[key] or [])
            if not device_ids:
                continue
            # An entry is valid only if it has at least the /namespaces key,
            # which is the one the library requires with a hard from_just().
            valid = [
                device_id
                for device_id in device_ids
                if f"/devices/{jid}/{device_id}/namespaces" in self._data
            ]
            if len(valid) != len(device_ids):
                dropped = [d for d in device_ids if d not in valid]
                logger.warning(
                    "OMEMO: dropping %d dangling device-list entries for %s "
                    "(missing per-device keys): %s",
                    len(dropped), jid, dropped,
                )
                self._data[key] = valid
                removed += len(dropped)

        # Drop per-device keys that belong to no listed device (orphaned keys).
        listed: dict[str, set[int]] = {}
        for key in list(self._data.keys()):
            if key.startswith("/devices/") and key.endswith("/list"):
                jid = key.split("/")[2]
                listed[jid] = set(self._data[key] or [])
        for key in list(self._data.keys()):
            if not key.startswith("/devices/"):
                continue
            parts = key.split("/")
            # /devices/<jid>/<device_id>/<attr> — the JID itself contains
            # slashes only as a domain separator, so parts[2] is always the
            # full bare JID and parts[3] the device id.
            if len(parts) < 5:
                continue
            jid = parts[2]
            try:
                device_id = int(parts[3])
            except ValueError:
                continue
            if jid in listed and device_id not in listed[jid]:
                del self._data[key]
                removed += 1

        if removed:
            self._write_sync(json.dumps(self._data, sort_keys=True, indent=2))
            logger.info("OMEMO: sanitized device lists, removed %d keys", removed)
        return removed


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

    name = "xep_0384"
    description = "Hermes OMEMO Encryption"

    def __init__(self, xmpp, config: dict):
        super().__init__(xmpp, config)

        from hermes_constants import get_hermes_home

        self._allow_untrusted = bool(config.get("allow_untrusted", True))
        self._storage_path = Path(config.get("storage_path")) or (
            get_hermes_home() / "sessions" / "omemo.json"
        )
        self._storage = JSONFileStorage(self._storage_path)
        # Repair the store before the OMEMO session manager builds: an
        # earlier version of the pruning code removed per-device keys but left
        # their IDs in the device lists, and the library's SessionManager.create
        # hard-crashes ("Maybe.fromJust: Nothing") on any listed device whose
        # /namespaces key is missing. Dropping such dangling entries up front
        # both repairs that corruption and guards against future mismatches.
        self._storage.sanitize_device_lists()
        self._pending_manual_trust: asyncio.Queue[FrozenSet] = asyncio.Queue()

    @property
    def storage(self) -> Storage:
        return self._storage

    @property
    def _btbv_enabled(self) -> bool:
        return self._allow_untrusted

    async def prune_stale_sessions(self) -> int:
        """Remove OMEMO session state for devices that are no longer active.

        The OMEMO store grows without bound: every time we talk to a peer
        device we persist a double-ratchet session, and sessions for devices
        that have since been removed from the peer's device list are never
        cleaned up. Over time this balloons into hundreds of stale keys and
        can leave a desynced ratchet that fails to decrypt.

        This prunes, for every peer we have a device list for:
          - double-ratchet session keys for device IDs no longer in the list
          - the bot's own stale device entries (keep only the active device)

        Returns the number of keys removed.
        """
        storage = self._storage
        removed = 0

        try:
            own_device_id = (await storage.load_primitive("/own_device_id", int)).from_just()
        except Exception:
            own_device_id = None

        # Collect all bare JIDs that have a device list.
        jids = set()
        for key in list(storage._data.keys()):
            if key.startswith("/devices/") and key.endswith("/list"):
                jids.add(key.split("/")[2])

        for jid in jids:
            current = set(storage._data.get(f"/devices/{jid}/list", []) or [])
            # Prune double-ratchet sessions for devices no longer in the list.
            prefix = f"/eu.siacs.conversations.axolotl/{jid}/"
            for key in list(storage._data.keys()):
                if not key.startswith(prefix):
                    continue
                parts = key.split("/")
                if len(parts) < 5:
                    continue
                device_id = parts[4]
                try:
                    device_id = int(device_id)
                except ValueError:
                    continue
                if device_id not in current:
                    del storage._data[key]
                    removed += 1

        # Prune the bot's own stale device entries, keeping only the active one.
        # IMPORTANT: the device ID must also be removed from the device list,
        # not just its per-device keys. The library's SessionManager.create
        # iterates every listed device and requires its /namespaces key; a
        # listed-but-keyless device crashes OMEMO startup with
        # "Maybe.fromJust: Nothing".
        if own_device_id is not None:
            own_jid = None
            for key in list(storage._data.keys()):
                if key.startswith("/devices/") and key.endswith("/list"):
                    if own_device_id in (storage._data.get(key, []) or []):
                        own_jid = key.split("/")[2]
                        break
            if own_jid:
                own_prefix = f"/devices/{own_jid}/"
                for key in list(storage._data.keys()):
                    if not key.startswith(own_prefix):
                        continue
                    parts = key.split("/")
                    if len(parts) < 4:
                        continue
                    device_id = parts[3]
                    if device_id == "list":
                        continue
                    try:
                        device_id = int(device_id)
                    except ValueError:
                        continue
                    if device_id != own_device_id:
                        del storage._data[key]
                        removed += 1
                # Remove the stale IDs from the device list itself.
                own_list_key = f"/devices/{own_jid}/list"
                own_list = storage._data.get(own_list_key, [])
                if own_list:
                    pruned_list = [d for d in own_list if d == own_device_id]
                    if len(pruned_list) != len(own_list):
                        storage._data[own_list_key] = pruned_list
                        removed += len(own_list) - len(pruned_list)

        if removed:
            await storage._save()
            logger.info("OMEMO: pruned %d stale session/device keys", removed)
        return removed

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
