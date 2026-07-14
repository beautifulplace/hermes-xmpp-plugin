# Hermes XMPP Platform Plugin

XMPP gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Connects the agent to an XMPP server, routes messages, supports inbound/outbound media, optional OMEMO end-to-end encryption, and voice-message transcription.

## Features

- Plain-text or OMEMO-encrypted messaging
- XEP-0085 typing indicators
- XEP-0333 read receipts / chat markers
- XEP-0066 / XEP-0363 inbound images, files, and voice messages
- `aesgcm://` OMEMO media sharing decryption
- XEP-0084 avatar publishing
- Outgoing voice messages via text-to-speech (TTS)
- Inbound voice-message transcription via local faster-whisper

## Installation

Download the plugin tarball and extract it:

```bash
tar -xzf hermes-xmpp-plugin.tar.gz
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

### Optional: enable OMEMO encryption

```bash
python3 install_xmpp_plugin.py --only-required-deps
```

By default the installer installs `slixmpp-omemo` as an optional dependency. If you do not need OMEMO, pass `--only-required-deps`.

### Optional: enable local voice-message transcription

To install [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and configure Hermes to use it for inbound XMPP voice messages:

```bash
python3 install_xmpp_plugin.py --with-whisper
```

This defaults to the `tiny` model. You can choose a different model:

```bash
python3 install_xmpp_plugin.py --with-whisper base
```

Available models: `tiny`, `base`, `small`, `medium`, `large-v1`, `large-v2`, `large-v3`.

The installer will:

- Install `faster-whisper` into the plugin `deps/` directory
- Pre-download the requested model so first-use transcription is fast
- Set `stt.enabled: true`, `stt.provider: local`, and `stt.local.model: <model>` in `config.yaml`

For a Raspberry Pi 4 with 8 GB RAM, `tiny` is recommended. Larger models are slower and use more RAM.

### Model sizes and hardware requirements

| Model | Params | Disk | RAM (int8) | Recommended hardware |
|---|---|---|---|---|
| tiny | 39M | ~75 MB | ~300 MB | Raspberry Pi 4, low-power devices |
| base | 74M | ~150 MB | ~500 MB | Raspberry Pi 4, entry-level CPUs |
| small | 244M | ~500 MB | ~1 GB | Modern ARM boards, desktops |
| medium | 769M | ~1.5 GB | ~2.5 GB | Desktop/laptop CPU |
| large-v1 | 1.55B | ~3 GB | ~3–4 GB | Desktop CPU with 8 GB+ RAM, or 4 GB+ VRAM GPU |
| large-v2 | 1.55B | ~3 GB | ~3–4 GB | Desktop CPU with 8 GB+ RAM, or 4 GB+ VRAM GPU |
| large-v3 | 1.55B | ~3 GB | ~3–4 GB | Desktop CPU with 8 GB+ RAM, or 4 GB+ VRAM GPU |

The large models may load on an 8 GB Raspberry Pi with int8 quantization, but transcription will be very slow and may run out of memory if other services are active.

### Non-interactive installation

For CI or headless setups:

```bash
python3 install_xmpp_plugin.py \
  --non-interactive \
  --jid "hermes@example.com" \
  --password "your-password" \
  --with-whisper tiny \
  --force
```

## Configuration

The installer writes a default `platforms.xmpp` block in `config.yaml`. Key settings:

```yaml
platforms:
  xmpp:
    enabled: true
    user_jid: "hermes@example.com"
    password: "your-password"
    omemo_enabled: true
    omemo_allow_untrusted: true
    typing_indicator: true
    voice_reply: false
    voice_model: en-GB-SoniaNeural
    voice_format: m4a
    avatar_path: "/path/to/avatar.png"
    home_channel: ""
    allow_all_users: false
```

For security, store the password in your Hermes `.env` file instead:

```bash
# ~/.hermes/.env
XMPP_USER_JID="hermes@example.com"
XMPP_PASSWORD="your-password"
```

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
