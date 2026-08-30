# Changelog

## [1.1.5] - 2026-08-29

### Fixed
- **OMEMO startup crash caused by the 1.1.2 pruning fix.** The pruning added
  in 1.1.2 removed the bot's stale own-device *keys* but left their IDs in the
  device list. The OMEMO library's `SessionManager.create` iterates every
  listed device and requires its `/namespaces` key, so any listed-but-keyless
  device crashed OMEMO startup with `NothingException: Maybe.fromJust: Nothing`
  — after which every encrypt/decrypt in that gateway process re-raised the
  same cached failure and replies silently fell back to plaintext.
  - `prune_stale_sessions()` now also removes stale IDs from the device list,
    keeping the list and per-device keys consistent.
  - New `sanitize_device_lists()` repair runs at plugin construction, before
    the session manager builds: it drops dangling device-list entries that are
    missing their per-device keys (repairing stores corrupted by 1.1.2-1.1.4)
    and removes orphaned per-device keys that belong to no listed device.
    Verified against a copy of the real corrupted store; idempotent.

## [1.1.4] - 2026-08-27

### Fixed
- **Keepalive ping timeout no longer triggers a reconnect.** A single XEP-0199
  ping that exceeds the timeout on a slow link is no longer treated as fatal.
  The adapter now falls back to a whitespace keepalive and keeps the loop
  alive; only a real stream drop (the slixmpp "disconnected" event) triggers a
  reconnect.
- **Background tasks cancelled on client cleanup.** `_cleanup_client()` now
  cancels per-chat typing refresh loops and pending voice-reply debounce
  timers (and clears the voice-reply queue) so they do not outlive the client
  on a reconnect or shutdown.
- **Removed dead `typing_indicator` config bridge.** `typing_indicator` was
  still listed in `_XMPP_YAML_KEYS` and bridged by `_apply_yaml_config`, but
  the adapter hardcodes it to `True` and ignores config. The dead bridge is
  removed.

## [1.1.3] - 2026-08-27

### Fixed
- **Media URL detection with query strings/fragments.** `_is_media_url()` and
  `_is_audio_url()` now strip the query string and fragment before the
  extension check, so URLs like `photo.jpg?size=large` or `audio.mp3#frag` are
  recognized as media instead of being treated as plain text links.
- **OMEMO replies delivered to all devices.** `send()` now routes OMEMO-active
  chats to the bare JID so slixmpp-omemo encrypts for every published device
  and all the user's clients receive the reply, instead of only the single
  cached resource that last messaged the bot.
- **Removed dead ffmpeg dependency.** The unused `SYSTEM_DEPENDENCIES` ffmpeg
  dict and its stale "convert MP3 to M4A" comment were removed from the
  installer (MP3 voice replies work directly; ffmpeg is not used).

## [1.1.2] - 2026-08-27

### Fixed
- **OMEMO session/device accumulation.** The OMEMO store grew without bound:
  every exchange with a peer device persisted a double-ratchet session, and
  sessions for devices removed from the peer's device list were never cleaned
  up. Over time this ballooned into hundreds of stale keys and could leave a
  desynced ratchet that failed to decrypt ("Authentication tags do not
  match"). Added `HermesOMEMO.prune_stale_sessions()`, called after OMEMO is
  ready at startup, which removes double-ratchet sessions for peer devices no
  longer in the current device list and cleans up the bot's own stale device
  entries (keeping only the active one).

## [1.1.1] - 2026-08-27

### Changed
- **Dashboard card text.** The plugin `install_hint` — shown as the channel description on
  the Hermes dashboard Channels page — is now the user-facing sentence "Talk to Hermes
  over XMPP" instead of the pip dependency command (changed in `plugin.yaml` and the
  `PlatformEntry` registration in `adapter.py`).
- **Logging cleanup.** Reduced verbosity of routine XMPP connection, send, and chat-state
  log lines in `adapter.py`.
- **OMEMO state persistence.** OMEMO `JSONFileStorage` now writes asynchronously under a
  lock and offloads disk I/O to a worker thread to avoid blocking the gateway event loop.

## [1.1.0] - 2026-08-20

### Added
- **Versioning.** The plugin now carries a version number (`__version__` in
  `xmpp_plugin_source/__init__.py`, mirrored in `pyproject.toml` and
  `plugin.yaml`) so the installed build can be identified.

### Fixed
- **Standalone voice/image messages were dropped.** Messages with an empty text
  body (the normal case for voice messages) were discarded before the media URL
  was extracted from `<oob>`/`<file-sharing>`. The adapter now only drops a
  message when there is no body AND no media URL.
- **XEP-0447 file-sharing namespace.** The `<file>` child is in
  `urn:xmpp:share:1`, not `urn:xmpp:sfs:0` (matching the outbound side), so
  inbound file-sharing URLs are now extracted correctly.
- **Concurrent voice replies.** Replaced the single global debounce task with
  per-chat tasks, so a second chat's voice message no longer cancels the first
  chat's pending reply.
- **Tool-progress detection.** Now matches any emoji-first message instead of a
  narrow verb list (running/reading/executing) that missed most gateway verbs.
- **Removed raw XML logging at WARNING level** (leftover debug instrumentation
  that logged every stanza including plaintext bodies).
- **Cached-media MIME type** is now derived from the actual file extension
  instead of being hardcoded to `audio/mpeg`.
- **URL extraction** now strips trailing punctuation instead of greedily
  matching it into the URL.
- **Pillow deprecation.** Uses `Image.Resampling.LANCZOS` with a fallback for
  Pillow < 9.1.

### Changed
- **Dependencies reconciled.** `slixmpp-omemo` is now a required dependency in
  `pyproject.toml` (matching `requirements.txt` and the installer), and the
  forbidden `edge-tts` TTS extra was removed.
- **Dead code removed.** `media.py`, `xmpp_utils.py`, `_is_voice_url`,
  `_python_env`, `_ensure_stt_config`, and the unused `required` field in the
  installer's `DEPENDENCIES` list.
- **Corrected `_guess_audio_is_voice` docstring** to match the code (a bare
  audio URL with no caption is treated as voice regardless of container).
