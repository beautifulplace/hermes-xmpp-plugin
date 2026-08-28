# Changelog

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
