# Changelog

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
