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
- A running [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway
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

You will be prompted for your XMPP JID, password, and an optional avatar path. The installer will:

1. Copy the plugin to `~/.hermes/plugins/platforms/xmpp/`
2. Enable it in `config.yaml`
3. Install required Python dependencies into the plugin's own `deps/` directory
4. Back up your existing config before editing

Restart the Hermes gateway to load the plugin:

```bash
hermes gateway restart
```

### Non-interactive installation

For CI or headless setups, pass `--non-interactive` with `--jid` and `--password`:

```bash
python3 install_xmpp_plugin.py \
  --non-interactive \
  --jid "hermes@example.com" \
  --password "your-password"
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

The installer writes a default `platforms.xmpp` block in `config.yaml`:

```yaml
platforms:
  xmpp:
    enabled: true
    omemo_enabled: true
    omemo_allow_untrusted: true
    typing_indicator: true
    avatar_path: "/path/to/avatar.png"
    home_channel: ""
    allow_all_users: false
```

For security, the installer stores the JID and password in your Hermes `.env` file instead of `config.yaml`:

```bash
# ~/.hermes/.env
XMPP_USER_JID="hermes@example.com"
XMPP_PASSWORD="your-password"
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
| `XMPP_HOME_CHANNEL` | Default JID for cron / notifications |
| `XMPP_ALLOW_ALL_USERS` | Allow any user to message the bot (default: false) |

## Voice and audio

The installer sets up the default voice and audio configuration automatically:

```yaml
stt:
  enabled: true
  provider: local
  local:
    model: tiny

voice:
  auto_tts: true

tts:
  provider: edge
  use_gateway: false
```

With this default, the adapter transcribes inbound voice messages using Hermes core STT (`faster-whisper`) and replies with both a TTS voice message and the full text response. Generated audio is uploaded with `aesgcm://` OMEMO media-sharing metadata so it plays inline in supporting clients.

You can change the STT model or TTS provider by editing the corresponding blocks in `~/.hermes/config.yaml`.

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

MIT License — see [LICENSE](LICENSE).

Copyright (c) 2026 beautifulplace and contributors.
