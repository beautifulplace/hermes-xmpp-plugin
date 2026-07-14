# XMPP Platform Adapter

XMPP gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Connects to an XMPP server using `slixmpp` and routes messages between XMPP users and the agent, with OMEMO end-to-end encryption enabled by default.

## Enabling the Plugin

User-installed plugins are opt-in. Add the plugin to `plugins.enabled` in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - platforms/xmpp
```

Restart the Hermes gateway after enabling it.

## Configuration

The installer writes a default `platforms.xmpp` block in `config.yaml`:

```yaml
platforms:
  xmpp:
    enabled: true
    omemo_enabled: true
    typing_indicator: true
    avatar_path: "/path/to/avatar.png"
    home_channel: ""
    allow_all_users: false
```

For security, the installer stores the JID and password in your Hermes `.env` file instead of `config.yaml`.

### Environment variables

| Variable | Purpose |
|---|---|
| `XMPP_USER_JID` | Bot XMPP address |
| `XMPP_PASSWORD` | Bot account password |
| `XMPP_SERVER` | Server hostname override (optional) |
| `XMPP_PORT` | Server port (default: 5222) |
| `XMPP_OMEMO_ENABLED` | Enable OMEMO (default: true) |
| `XMPP_OMEMO_ALLOW_UNTRUSTED` | Auto-trust new OMEMO devices (default: true) |
| `XMPP_AVATAR_PATH` | Path to an avatar image (optional) |
| `XMPP_HOME_CHANNEL` | Default JID for cron / notifications |
| `XMPP_ALLOW_ALL_USERS` | Allow any user to message the bot (default: false) |

## OMEMO End-to-End Encryption

OMEMO (XEP-0384) is enabled by default so that all messages between the bot and supporting XMPP clients are end-to-end encrypted.

### Requirements

`slixmpp-omemo` is installed automatically by the plugin installer. If you installed manually, install it into the Hermes environment:

```bash
/home/lobot/.hermes/hermes-agent/venv/bin/python -m pip install slixmpp-omemo
```

### Trust model

By default, the bot uses **Blind Trust Before Verification (BTBV)**: new OMEMO devices are automatically trusted so the bot can reply immediately. This is appropriate for a personal bot where you control both endpoints.

To require manual trust before the bot replies to a new device, set:

```yaml
platforms:
  xmpp:
    omemo_enabled: true
    omemo_allow_untrusted: false
```

or

```bash
XMPP_OMEMO_ALLOW_UNTRUSTED=false
```

With manual trust, the bot will warn in the logs and replies to untrusted devices will fail until you approve the device from your XMPP client.

### Key storage

OMEMO identity keys, sessions, device bundles, and trust decisions are stored in a single JSON file created automatically at:

```
~/.hermes/sessions/omemo.json
```

### Disabling OMEMO

Set `omemo_enabled: false` or omit it entirely. The adapter will then use plain-text XMPP messages and continue to work with clients that do not support OMEMO.

## Avatar

The XMPP adapter publishes a profile avatar for the bot account using XEP-0084 (User Avatar) and XEP-0153 (vCard-based Avatars). Most XMPP clients display this as the bot's profile picture.

Provide a PNG or JPEG image. The adapter will automatically:

- Crop the image to a square centered on the center of the frame
- Resize to 480×480 pixels
- Convert to PNG
- Publish via both PEP (XEP-0084) and vCard (XEP-0153) for maximum client compatibility

Configure the avatar in `config.yaml`:

```yaml
platforms:
  xmpp:
    avatar_path: "/path/to/avatar.png"
```

or via the environment variable:

```bash
XMPP_AVATAR_PATH=/path/to/avatar.png
```

The avatar is re-published each time the bot connects, so you can change the file and restart the gateway to update it.

## Voice and Audio

This adapter delegates speech-to-text and text-to-speech to the Hermes core. Configure them in `~/.hermes/config.yaml`:

### Inbound voice-message transcription (STT)

```yaml
stt:
  enabled: true
  provider: local
  local:
    model: medium
```

### Outgoing voice replies (TTS)

```yaml
voice:
  auto_tts: true
tts:
  provider: edge
```

Set `voice.auto_tts: true` to reply with voice to voice messages, or use the chat `/voice on` command. The adapter uploads the generated audio with `aesgcm://` OMEMO media-sharing metadata so it plays inline in supporting clients.

## Read Receipts (Chat Markers)

The adapter supports XEP-0333 Chat Markers. When an incoming message from a client such as Conversations requests delivery confirmation (`<markable/>`), the bot replies with a `displayed` marker after processing the message. This gives you the second checkmark in Conversations, indicating the bot has read the message.

## Inbound Images and Files

The adapter can receive images and other files sent from XMPP clients:

- Plain `https://` URLs in the message body are downloaded directly.
- `aesgcm://` URLs (OMEMO-encrypted media sharing used by Conversations) are downloaded over HTTPS and decrypted with the AES-256-GCM key embedded in the URL fragment.
- Downloaded files are cached in the Hermes image cache and passed to the agent as `media_urls` so tools like `vision_analyze` can inspect them.

No extra configuration is required.

## Typing Indicator

The adapter supports XEP-0085 Chat State Notifications. While the agent is generating a response, your XMPP client should show a "typing" / "composing" state. The indicator disappears when the response is sent. It is enabled by default via `typing_indicator: true`.
