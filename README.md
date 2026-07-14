# Hermes XMPP Platform Plugin

XMPP gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Connects the agent to an XMPP server, routes messages, supports inbound/outbound media, OMEMO end-to-end encryption by default, and voice-message transcription.

## Features

- OMEMO-encrypted messaging (default; plain-text fallback)
- XEP-0085 typing indicators
- XEP-0333 read receipts / chat markers
- XEP-0066 / XEP-0363 inbound images, files, and voice messages
- `aesgcm://` OMEMO media sharing decryption
- XEP-0084 avatar publishing
- Outgoing voice messages via text-to-speech (TTS)
- Inbound voice-message transcription via local faster-whisper

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

### Optional: enable high-quality local TTS for voice replies

By default, outgoing voice replies use `edge-tts` (cloud voices from Microsoft Edge). For a local, higher-quality alternative, install [MeloTTS](https://github.com/myshell-ai/MeloTTS):

```bash
python3 install_xmpp_plugin.py --with-melotts
```

The installer will:

- Install `MeloTTS` into the plugin `deps/` directory
- Pre-download the English model
- Set `voice_tts: melo` and `voice_model: EN-Default` in `config.yaml`

You can also combine faster-whisper and MeloTTS:

```bash
python3 install_xmpp_plugin.py --with-whisper base --with-melotts
```

MeloTTS speaker choices include: `EN-Default`, `EN-US`, `EN-BR`, `EN-AU`, `EN-IN`. Change `voice_model` in `config.yaml` or pass `--voice-model` at install time.

Note: the first model download is from Hugging Face. For faster downloads, provide an HF_TOKEN when prompted.

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
| medium | 769M | ~1.5 GB | ~2.5 GB | Desktop/laptop CPU (practical CPU limit) |
| large-v1 | 1.55B | ~3 GB | ~3–4 GB | Desktop CPU with 8 GB+ RAM, or 4 GB+ VRAM GPU. Very slow on CPU; use medium or smaller for CPU-only real-time. |
| large-v2 | 1.55B | ~3 GB | ~3–4 GB | Desktop CPU with 8 GB+ RAM, or 4 GB+ VRAM GPU. Very slow on CPU; use medium or smaller for CPU-only real-time. |
| large-v3 | 1.55B | ~3 GB | ~3–4 GB | Desktop CPU with 8 GB+ RAM, or 4 GB+ VRAM GPU. Very slow on CPU; use medium or smaller for CPU-only real-time. |

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
    voice_tts: edge
    voice_model: EN-Default
    voice_format: m4a
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
