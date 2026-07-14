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

- Copy the plugin to `~/.hermes/plugins/platforms/xmpp/`
- Enable it in `config.yaml`
- Install required Python dependencies into the plugin's own `deps/` directory
- Back up your existing config before editing

Then restart the Hermes gateway:

```bash
hermes gateway restart
```

### Optional: disable OMEMO encryption

OMEMO is enabled by default. To install the plugin without the OMEMO dependency, pass `--only-required-deps`:

```bash
python3 install_xmpp_plugin.py --only-required-deps
```

If the plugin is already installed, edit `~/.hermes/config.yaml` and set:

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

For security, store the JID and password in your Hermes `.env` file instead of `config.yaml`:

```bash
# ~/.hermes/.env
XMPP_USER_JID="hermes@example.com"
XMPP_PASSWORD="your-password"
```

## Voice and audio

The XMPP plugin delegates speech-to-text and text-to-speech to the Hermes core. Configure them in `~/.hermes/config.yaml`:

### Inbound voice-message transcription (STT)

```yaml
stt:
  enabled: true
  provider: local
  local:
    model: medium
```

Hermes uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for local STT. Install the model through the Hermes setup flow or by installing `faster-whisper` into the Hermes environment.

### Outgoing voice replies (TTS)

```yaml
voice:
  auto_tts: true
tts:
  provider: edge
```

Set `voice.auto_tts: true` to reply with voice to voice messages, or use the chat `/voice on` command. Available TTS providers are configured by Hermes (`edge`, `elevenlabs`, `openai`, `minimax`, `mistral`, `gemini`, `xai`, `neutts`, `kittentts`, or custom command providers). Run `hermes setup` or edit `config.yaml` to choose a provider.

## Uninstallation

```bash
python3 uninstall_xmpp_plugin.py
```

This removes the plugin directory and disables it in `config.yaml`. A config backup is created first.

## Development

Run the local tests:

```bash
python3 -m pytest
```

## License

MIT License — see [LICENSE](LICENSE).
