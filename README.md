# Hermes XMPP Platform Plugin

XMPP gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Connects the agent to an XMPP server, routes messages, supports inbound/outbound media, OMEMO end-to-end encryption by default, and voice/audio messages via the Hermes core TTS/STT configuration.

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
- A running [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway installation
- An XMPP account for the bot

## Installation

Clone the repository:

```bash
git clone https://github.com/beautifulplace/hermes-xmpp-plugin.git
cd hermes-xmpp-plugin
```

Run the installer:

```bash
python3 install_xmpp_plugin.py
```

You will be prompted for your XMPP JID, password, an optional avatar path, and the comma-separated JIDs of users allowed to talk to the bot. The installer writes these to your Hermes `.env` file so reinstalls do not require retyping. The allowed-users list is stored as `XMPP_ALLOWED_USERS`; without it the gateway denies every sender.

1. Copy the plugin to `~/.hermes/plugins/platforms/xmpp/`
2. Enable it in `config.yaml`
3. Install required Python dependencies into the plugin's own `deps/` directory
4. Back up your existing config before editing

Restart the Hermes gateway to load the plugin:

```bash
hermes gateway restart
```

### Installing into a specific profile

If you use Hermes profiles, switch to the target profile and verify it is active:

```bash
hermes profile use my-bot
hermes profile list
```

Then run the installer from inside the cloned repository:

```bash
python3 install_xmpp_plugin.py
```

The installer detects the active profile from `~/.hermes/active_profile` and installs the plugin into that profile's directory (e.g. `~/.hermes/profiles/my-bot/plugins/platforms/xmpp/`).

### Non-interactive installation

For CI or headless setups, pass `--non-interactive` with `--jid` and `--password`:

```bash
python3 install_xmpp_plugin.py \
  --non-interactive \
  --jid "hermes@example.com" \
  --password "hermes-password" \
  --allowed-users "you@example.com"
```

### Advanced: installing with `hermes plugins install`

Hermes core has a generic plugin installer:

```bash
hermes plugins install beautifulplace/hermes-xmpp-plugin/xmpp_plugin_source
hermes plugins enable xmpp-platform
```

This copies the plugin files, runs the security scan, and prompts for
`XMPP_USER_JID` / `XMPP_PASSWORD`. It intentionally does **not** install
Python dependencies or write default config, so finish the setup with the
post-install script shipped inside the plugin:

```bash
python3 ~/.hermes/plugins/xmpp-platform/post_install.py
```

That installs the plugin's Python dependencies into its own `deps/`
directory (never touching externally-managed Pythons), adds the default
`platforms.xmpp` block (OMEMO on) and voice/STT defaults to config.yaml,
prompts for the allowed-users allowlist (deny-all by default; use
`--allow-all-users` to open the bot, or `--allowed-users "jid1,jid2"` to
skip prompts), and seeds the home channel from the first allowed user.
It is safe to re-run. Then restart the gateway:

```bash
hermes gateway restart
```

### Disable OMEMO encryption

If you need to disable OMEMO after installation, edit `~/.hermes/config.yaml` and set:

```yaml
platforms:
  xmpp:
    omemo_enabled: false
```

Then restart the gateway.

## Configuration

The installer writes a minimal `platforms.xmpp` block in `config.yaml`:

```yaml
platforms:
  xmpp:
    enabled: true
    omemo_enabled: true
    omemo_allow_untrusted: true
    allow_all_users: false
```

All install-specific settings (credentials, home channel, and avatar path) are stored in your Hermes `.env` file:

```bash
# ~/.hermes/.env
XMPP_USER_JID="hermes@example.com"
XMPP_PASSWORD="hermes-password"
XMPP_ALLOWED_USERS="you@example.com,friend@example.net"
XMPP_HOME_CHANNEL="you@example.com"
XMPP_AVATAR_PATH="/path/to/avatar.png"
```

### Environment variables

Every `platforms.xmpp` option can also be set via an environment variable:

| Variable | Purpose |
|---|---|
| `XMPP_USER_JID` | Bot XMPP address |
| `XMPP_PASSWORD` | Bot account password |
| `XMPP_OMEMO_ENABLED` | Enable OMEMO (default: true) |
| `XMPP_OMEMO_ALLOW_UNTRUSTED` | Auto-trust new OMEMO devices (default: true) |
| `XMPP_AVATAR_PATH` | Path to an avatar image (optional) |
| `XMPP_HOME_CHANNEL` | Default JID for cron / notifications. **Seeded automatically at install** from the first entry of `XMPP_ALLOWED_USERS`; an existing value (or one set later via `/sethome`) always wins. |
| `XMPP_ALLOWED_USERS` | Comma-separated JIDs allowed to message the bot (default: none, deny all) |
| `XMPP_ALLOW_ALL_USERS` | Allow any user to message the bot (default: false) |

> **Home channel note:** cron delivery and restart notifications need a home
> target. The installer seeds `XMPP_HOME_CHANNEL` in `.env` from the first
> allowlisted JID so no manual step is required. If the allowlist is empty,
> nothing is seeded; use `/sethome` in a chat with the bot, or set the
> variable in `.env` yourself. `/sethome` also records the home channel in
> `config.yaml` (that is core Hermes behavior for every platform); the `.env`
> value is what the installer and the legacy env fallback read.

> **Security note:** if you do not set `XMPP_ALLOWED_USERS`, any user who can
> reach your agent over XMPP will be able to talk to it. To restrict access,
> set `XMPP_ALLOWED_USERS` to a comma-separated allowlist. To explicitly open
> the bot to everyone, set `allow_all_users: true` in `config.yaml` (or
> `XMPP_ALLOW_ALL_USERS=true` in `.env`). The installer prompts for this
> explicitly rather than silently opening the agent to all users.

## Voice and Audio

The installer sets up the default voice and audio configuration automatically:

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

With this default, the adapter transcribes inbound voice messages using Hermes core STT (`faster-whisper`) and replies with both a TTS voice message and the full text response. Text messages receive text-only replies.

If you change `voice.auto_tts` to `true`, **every** reply (voice or text input) will be sent as a TTS voice message in addition to the text response. The adapter-level voice reply to inbound voice messages is independent of this setting.

You can change the STT model or TTS provider by editing the corresponding blocks in `~/.hermes/config.yaml`. Existing settings are never overwritten by the installer.

## Uninstallation

```bash
python3 uninstall_xmpp_plugin.py
```

This removes the plugin directory and disables it in `config.yaml`. A config backup is created first.

## Development

Install development dependencies:

```bash
python3 -m pip install ruff pytest
```

Run the linter:

```bash
ruff check .
```

Run the tests:

```bash
python3 -m pytest
```

## License

MIT License - see [LICENSE](LICENSE).

Copyright (c) 2026 beautifulplace.
