# Hermes XMPP Platform Plugin

XMPP gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Connects the agent to an XMPP server, routes messages, supports inbound/outbound media, OMEMO end-to-end encryption by default, and voice/audio messages via the Hermes core TTS/STT configuration.

This repository IS a Hermes plugin: the plugin manifest (`plugin.yaml`), the adapter, and its dependencies live at the repository root, so Hermes installs and updates it with its own plugin commands. No checkout to maintain and nothing to run by hand.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Upgrading](#upgrading)
- [Configuration](#configuration)
- [OMEMO End-to-End Encryption](#omemo-end-to-end-encryption)
- [Voice and Audio](#voice-and-audio)
- [Avatar](#avatar)
- [Read Receipts (Chat Markers)](#read-receipts-chat-markers)
- [Inbound Images and Files](#inbound-images-and-files)
- [Uninstallation](#uninstallation)
- [Development](#development)
- [License](#license)

## Features

- OMEMO-encrypted messaging (default; plain-text fallback)
- XEP-0085 typing indicators
- XEP-0333 read receipts / chat markers
- XEP-0066 / XEP-0363 inbound images, files, and voice messages
- `aesgcm://` OMEMO media sharing decryption
- XEP-0084 avatar publishing
- Outgoing voice/audio messages via Hermes core TTS
- Inbound voice-message transcription via Hermes core STT

## Requirements

- Python 3.10+
- A running [Hermes Agent](https://github.com/NousResearch/hermes-agent) installation
- An XMPP account for the bot

## Installation

Installation is three steps: install the plugin, run post-install, then restart the gateway.

### 1. Install the plugin

```bash
hermes plugins install rebelcommand/hermes-xmpp-plugin --enable
hermes plugins enable xmpp-platform   # only if you installed without --enable
```

The installer clones the repository into a temporary directory, copies the plugin into your Hermes home (`~/.hermes/plugins/xmpp-platform/`), runs its security scan, and removes the temporary clone. You never keep a working copy of this repository.

### 2. Run post-install

```bash
python3 ~/.hermes/plugins/xmpp-platform/post_install.py
```

`post_install.py` completes what the core installer intentionally leaves alone:

1. Installs the plugin's Python dependencies into the plugin's own `deps/` directory (never into an externally-managed Python), skipping any that are already importable.
2. Adds the default `platforms.xmpp` block (OMEMO on by default) and the voice/STT defaults to `config.yaml`, after backing the file up.
3. Prompts for the bot's XMPP JID and password (existing values are shown as defaults, so press Enter to keep them or type to change them), the allowed-users allowlist (deny-all by default), and an optional avatar path.
4. Writes the credentials, allowlist, home channel, and avatar path to `~/.hermes/.env` (secrets stay out of `config.yaml`).

It is safe to re-run: existing `.env` values win, config defaults are only added when missing, and satisfied dependencies are skipped.

### 3. Restart the gateway

```bash
hermes gateway restart
```

### Installing into a specific profile

Hermes profiles are independent. Install into the profile you want the bot in, running post-install against that profile before restarting:

```bash
hermes -p my-bot plugins install rebelcommand/hermes-xmpp-plugin --enable
python3 ~/.hermes/plugins/xmpp-platform/post_install.py --profile my-bot
hermes -p my-bot gateway restart
```

Post-install reads the active profile's config and `.env`; pass `--profile my-bot` (or `--hermes-home`) if you want it explicit.

### Non-interactive installation

For headless setups, skip every prompt:

```bash
hermes plugins install rebelcommand/hermes-xmpp-plugin --enable
python3 ~/.hermes/plugins/xmpp-platform/post_install.py \
  --non-interactive \
  --allowed-users "you@example.com"
```

Set `XMPP_USER_JID` and `XMPP_PASSWORD` in `~/.hermes/.env` yourself first (or immediately after), since the installer prompts for them only on a terminal.

## Upgrading

```bash
hermes plugins update xmpp-platform
hermes gateway restart
```

That is the whole upgrade. `hermes plugins update` pulls the latest revision from this repository into your Hermes plugin directory, refreshes the installed files, and re-runs the security scan; the gateway restart loads the new code. Check what you are running with:

```bash
hermes plugins list
```

Because this repository is a plugin root, upgrades work natively. If you ever see `was not installed from git (no .git directory)`, you installed a subdirectory path instead of the repository root; reinstall with the command above.

Nothing in your configuration is touched by an upgrade. Settings live in `config.yaml` and credentials in `.env`, both outside the plugin directory, so upgrades never ask you to re-enter anything.

## Configuration

The default `platforms.xmpp` block written by `post_install.py`:

```yaml
platforms:
  xmpp:
    enabled: true
    omemo_enabled: true
    omemo_allow_untrusted: true
    allow_all_users: false
```

All install-specific settings (credentials, allowlist, home channel, avatar path) are stored in your Hermes `.env` file:

```bash
# ~/.hermes/.env
XMPP_USER_JID="hermes@example.com"
XMPP_PASSWORD="hermes-password"
XMPP_ALLOWED_USERS="you@example.com,friend@example.net"
XMPP_HOME_CHANNEL="you@example.com"
XMPP_AVATAR_PATH="/path/to/avatar.png"
```

### Environment variables

| Variable | Purpose |
|---|---|
| `XMPP_USER_JID` | Bot XMPP address |
| `XMPP_PASSWORD` | Bot account password |
| `XMPP_ALLOWED_USERS` | Comma-separated JIDs allowed to message the bot (default: none, deny all) |
| `XMPP_ALLOW_ALL_USERS` | Allow any user to message the bot (default: false) |
| `XMPP_SERVER` | Server hostname override (default: the JID domain) |
| `XMPP_PORT` | Server port (default: 5222) |
| `XMPP_OMEMO_ENABLED` | Enable OMEMO (default: true) |
| `XMPP_OMEMO_ALLOW_UNTRUSTED` | Auto-trust new OMEMO devices (default: true) |
| `XMPP_AVATAR_PATH` | Path to an avatar image (optional) |
| `XMPP_HOME_CHANNEL` | Default JID for cron / notifications. Seeded at install from the first `XMPP_ALLOWED_USERS` entry; an existing value (or one set later with `/sethome`) always wins. |

> **Home channel note:** cron delivery and restart notifications need a home target. `post_install.py` seeds `XMPP_HOME_CHANNEL` in `.env` from the first allowlisted JID so no manual step is required. If the allowlist is empty nothing is seeded; use `/sethome` in a chat with the bot, or set the variable in `.env` yourself. `/sethome` records the home channel in `config.yaml` (core Hermes behavior for every platform); the `.env` value is what post-install and the env fallback read.

> **Security note:** without `XMPP_ALLOWED_USERS` and without `allow_all_users`, the bot denies every sender. The installer prompts for this explicitly rather than silently opening the agent to all users.

## OMEMO End-to-End Encryption

OMEMO (XEP-0384) is enabled by default, so messages between the bot and supporting XMPP clients are end-to-end encrypted.

### Requirements

`slixmpp-omemo` is installed by `post_install.py`. If you set things up by hand, install it where the gateway runs:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install slixmpp-omemo
```

### Trust model

By default the bot uses **Blind Trust Before Verification (BTBV)**: new OMEMO devices are automatically trusted so the bot can reply immediately. This is appropriate for a personal bot where you control both endpoints.

To require manual trust before replying to a new device:

```yaml
platforms:
  xmpp:
    omemo_enabled: true
    omemo_allow_untrusted: false
```

With manual trust, the bot logs a warning and replies to untrusted devices fail until you approve the device from your XMPP client.

### Key storage

OMEMO identity keys, sessions, device bundles, and trust decisions are stored in a single JSON file created automatically at `~/.hermes/sessions/omemo.json`.

### Disabling OMEMO

Set `omemo_enabled: false` (or omit it). The adapter then uses plain-text XMPP messages and keeps working with clients that do not support OMEMO.

## Voice and Audio

`post_install.py` adds the default voice and audio configuration:

```yaml
stt:
  enabled: true
  provider: local
  local:
    model: tiny

voice:
  auto_tts: false

tts:
  provider: edge
  use_gateway: false
```

With this default the adapter transcribes inbound voice messages using Hermes core STT (`faster-whisper`) and replies with both a TTS voice message and the full text response. Text messages receive text-only replies.

If you set `voice.auto_tts` to `true`, **every** reply (voice or text input) is sent as a TTS voice message in addition to the text response. The adapter-level voice reply to inbound voice messages is independent of this setting.

Change the STT model or TTS provider by editing the corresponding blocks in `~/.hermes/config.yaml`. Existing settings are never overwritten.

## Avatar

The adapter publishes a bot avatar using XEP-0084 (User Avatar) and XEP-0153 (vCard-based Avatars). Most XMPP clients show this as the bot's profile picture.

Provide a PNG or JPEG; the adapter crops to a centered square, resizes to 480x480, converts to PNG, and publishes through both PEP (XEP-0084) and vCard (XEP-0153) for maximum client compatibility. Configure it in `config.yaml`:

```yaml
platforms:
  xmpp:
    avatar_path: "/path/to/avatar.png"
```

or with `XMPP_AVATAR_PATH` in `.env`. The avatar is re-published on every connect, so change the file and restart the gateway to update it.

## Read Receipts (Chat Markers)

The adapter supports XEP-0333 Chat Markers. When a client such as Conversations sends a markable message, the bot replies with a `displayed` marker after processing it, which gives you the second checkmark in Conversations.

## Inbound Images and Files

The adapter receives images and other files sent from XMPP clients:

- Plain `https://` URLs in the message body are downloaded directly.
- `aesgcm://` URLs (OMEMO-encrypted media sharing used by Conversations) are downloaded over HTTPS and decrypted with the AES-256-GCM key in the URL fragment.
- Downloaded files are cached in the Hermes image cache and passed to the agent as `media_urls` so tools like `vision_analyze` can inspect them.

No extra configuration is required.

## Typing Indicator

The adapter supports XEP-0085 Chat State Notifications. While the agent generates a response your client shows a composing state; it clears when the response is sent. Enabled by default.

## Uninstallation

```bash
hermes plugins remove xmpp-platform
hermes gateway restart
```

Your `config.yaml` and `.env` entries are left in place; delete the `platforms.xmpp` block and the `XMPP_*` variables if you want them gone.

## Streaming Behavior

XMPP's only edit primitive is XEP-0308 "Last Message Correction", a one-shot replace of the immediately previous message that cannot express the gateway's incremental streamed-edit model (and isn't wired into slixmpp's delivery path). The plugin therefore declares `SUPPORTS_MESSAGE_EDITING = False`, the same gate the WeChat, Signal, BlueBubbles, QQ, Photon, and WeCom adapters use for the same reason. The gateway then delivers each reply as exactly one message instead of attempting streamed edits (which on a platform without an edit API produces a partial preview followed by a duplicate final message). Tool progress, commentary, typing indicators, and voice replies are unaffected; they use separate delivery paths.

## Development

```bash
python3 -m pip install ruff pytest
ruff check .
python3 -m pytest
```

Layout: `plugin.yaml`, `__init__.py`, `adapter.py`, and `omemo_plugin.py` at the repository root are the plugin Hermes installs. `hermes_xmpp_plugin_common.py` holds shared config/`.env` helpers used by `post_install.py` and the tests. `scripts/release.sh` is a maintainer script for cutting releases; it is not part of the installed plugin.

## License

MIT License - see [LICENSE](LICENSE).

Copyright (c) 2026 rebelcommand.
