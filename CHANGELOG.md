# Changelog

## [1.2.0] - 2026-09-01

### Added
- **The installer now seeds `XMPP_HOME_CHANNEL` in `.env` from the first
  allowed user**, so cron delivery and restart notifications have a target
  without running `/sethome`. An existing `XMPP_HOME_CHANNEL` (set by a
  previous install or by `/sethome`) is never overwritten; an empty
  allowlist seeds nothing. Documented in both READMEs.

## [1.1.9] - 2026-09-01

### Added
- **Presence subscription automation so fresh installs show online (green).**
  At startup the bot now sends a subscription request (`subscribe`) to every
  JID on the allowlist, so the contact's client shows the standard
  "wants to add you" prompt once; accepting yields a mutual ("both")
  subscription. Inbound subscription requests are auto-approved (with a
  reciprocal `subscribe`) when the sender is allowlisted or allow-all is
  enabled; requests from non-allowlisted senders are silently ignored, the
  same policy as denied messages. When no allowlist is configured there is
  nothing to enumerate, so the proactive pass is a no-op and only the
  auto-approve path applies.

## [1.1.8] - 2026-09-01

### Changed
- **Installer now explains the allowlist security implication and requires an
  explicit opt-in to allow all users.** The allowed-users prompt now states
  that leaving the allowlist empty lets ANY user who can reach the agent over
  XMPP talk to it. When no allowlist is entered, the installer asks a
  yes/no "Allow ALL users to talk to this agent?" question; only an explicit
  "yes" sets `allow_all_users: true` in `config.yaml` and
  `XMPP_ALLOW_ALL_USERS=true` in `.env`. Answering "no" (or leaving it blank)
  keeps the bot deny-all by default. A `--allow-all-users` flag covers
  non-interactive installs.
- **`allow_all_users` / `allowed_users` config keys are now bridged to the
  gateway authorization env vars.** The adapter's `_apply_yaml_config` maps
  `allow_all_users` → `XMPP_ALLOW_ALL_USERS` and `allowed_users` →
  `XMPP_ALLOWED_USERS` (env wins when already set), so `config.yaml` is the
  source of truth for access control.
- **Reinstall clears a stale allow-all flag.** When the user now provides an
  allowlist (or opts out of allow-all), a leftover `XMPP_ALLOW_ALL_USERS=true`
  from a previous install is reset to `false` so the new choice takes effect.

## [1.1.7] - 2026-08-31

### Fixed
- **Uninstaller failed to disable the plugin on configs with duplicate
  `plugins:` blocks.** `disable_plugin()` only inspected the first
  `plugins:` block, so a stale empty `enabled: []` block left by profile
  creation shadowed the installer-written one: the enabled list kept
  `platforms/xmpp` (piling up duplicates across reinstall cycles) even though
  the config looked clean. `enable_plugin()`, `is_plugin_enabled()` and
  `disable_plugin()` now iterate every `plugins:` block: the enabled item is
  added to / removed from the block that actually wins (last-wins), stale
  empty-list blocks are dropped, and uninstall leaves no empty duplicates.

## [1.1.6] - 2026-08-31

### Added
- **Installer now asks for allowed users.** During interactive install, a new
  prompt collects the comma-separated XMPP JIDs allowed to talk to the bot and
  writes them to `XMPP_ALLOWED_USERS` in the profile `.env`. Without this
  variable the gateway denies every sender ("No env user allowlists
  configured" warning). A `--allowed-users` flag covers non-interactive
  installs; an existing `XMPP_ALLOWED_USERS` is offered as the default and
  upserted in place when changed. A warning is printed if the list stays
  empty.

### Fixed
- Installer no longer duplicates the `plugins:` block when the profile config
  uses a flow-style `enabled: []` list (as written by fresh profile creation).
- Installer now flips a pre-existing `platforms.xmpp.enabled: false` to `true`
  instead of leaving the freshly installed plugin disabled.

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
