# XMPP Platform Adapter

This is the built-in XMPP gateway adapter for Hermes Agent. It connects to an
XMPP server using `slixmpp` and routes messages between XMPP users and the agent.

## Enable the Plugin

User-installed plugins are **opt-in** — Hermes won't load them unless they're
listed in `plugins.enabled` in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - platforms/xmpp
```

Without this, the gateway starts but XMPP silently doesn't connect.

## OMEMO End-to-End Encryption

OMEMO (XEP-0384) can be enabled so that all messages between the bot and a
supporting XMPP client are end-to-end encrypted.

### Requirements

- `slixmpp-omemo` installed in the Hermes virtualenv:
  ```bash
  /home/lobot/.hermes/hermes-agent/venv/bin/python -m pip install slixmpp-omemo
  ```

### Configuration

Add to `~/.hermes/config.yaml` under the XMPP platform block:

```yaml
platforms:
  xmpp:
    enabled: true
    user_jid: "p4t9@bespin.ca"
    password: "your-password"
    omemo_enabled: true
```

Alternatively, set the environment variable:

```bash
XMPP_OMEMO_ENABLED=true
```

### Trust model

By default, the bot uses **Blind Trust Before Verification (BTBV)**: new OMEMO
devices are automatically trusted so the bot can reply immediately. This is
appropriate for a personal bot where you control both endpoints.

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

With manual trust, the bot will warn in the logs and replies to untrusted
devices will fail until you approve the device from your XMPP client.

### Key storage

OMEMO identity keys, sessions, device bundles, and trust decisions are stored
in a single JSON file at:

```
~/.hermes/sessions/omemo.json
```

This file is created automatically when OMEMO is first enabled.

### Disabling OMEMO

Set `omemo_enabled: false` or omit it entirely. The adapter will then use
plain-text XMPP messages and continue to work with clients that do not support
OMEMO.

## Avatar

The XMPP adapter publishes a profile avatar for the bot account using XEP-0084
(User Avatar) and XEP-0153 (vCard-based Avatars). Most XMPP clients display this
as the bot's profile picture.

### Setting the avatar

Provide a PNG or JPEG image. The adapter will automatically:

- Crop the image to a square centered on the center of the frame
- Resize to 480×480 pixels
- Convert to PNG
- Publish via both PEP (XEP-0084) and vCard (XEP-0153) for maximum client
  compatibility

There are three ways to configure the avatar:

**Option A — config.yaml (recommended):**

```yaml
platforms:
  xmpp:
    avatar_path: /home/lobot/.hermes/cache/images/xmpp_avatar.png
```

**Option B — environment variable:**

```bash
XMPP_AVATAR_PATH=/path/to/avatar.png
```

**Option C — interactive setup wizard:**

The `install.py` wizard prompts for an avatar path and copies the file to
`~/.hermes/cache/images/` so it won't break if the original file moves:

```bash
cd xmpp-plugin-dist && python3 install.py
```

### Tips

- Use a square or nearly-square image for best results.
- PNG with transparency is supported; RGBA images are converted to RGB first.
- The avatar is re-published each time the bot connects to the server, so you
  can change the file and restart the gateway to update it.
- If you skip the avatar during setup, you can add it later by setting
  `avatar_path` in config.yaml and restarting the gateway.


## Voice Replies

When enabled, every text reply is also sent as an inline voice message using
`edge-tts` and HTTP File Upload (XEP-0363). The audio is encrypted with the
same `aesgcm://` OMEMO media sharing format that Conversations uses for
images, so it plays inline in supporting clients.

### Requirements

- `edge-tts` installed in the Hermes virtualenv:
  ```bash
  /home/lobot/.hermes/hermes-agent/venv/bin/python -m pip install edge-tts
  ```
- `ffmpeg` installed system-wide with AAC and Opus encoders.

### Configuration

```yaml
platforms:
  xmpp:
    voice_reply: true
    voice_model: en-GB-SoniaNeural
    voice_format: m4a
```

`voice_model` is any edge-tts short name such as `en-GB-SoniaNeural`,
`en-AU-NatashaNeural`, or `en-US-GuyNeural`.

`voice_format` can be:
- `m4a` — AAC in MP4 container (best Conversations inline playback, default)
- `opus` — Opus in Ogg container
- `ogg` / `oga` — Opus in Ogg container

You can also set environment variables:

```bash
XMPP_VOICE_REPLY=true
XMPP_VOICE_MODEL=en-GB-SoniaNeural
XMPP_VOICE_FORMAT=m4a
```

## Read Receipts (Chat Markers)

The adapter supports XEP-0333 Chat Markers. When an incoming message from a
client such as Conversations requests delivery confirmation (`<markable/>`),
the bot replies with a `displayed` marker after processing the message.
This gives you the second checkmark in Conversations, indicating the bot has
read the message.

## Inbound Images and Files

The adapter can receive images and other files sent from XMPP clients.

- Plain `https://` URLs in the message body are downloaded directly.
- `aesgcm://` URLs (OMEMO-encrypted media sharing used by Conversations) are
  downloaded over HTTPS and decrypted with the AES-256-GCM key embedded in the
  URL fragment before being cached.
- Downloaded files are cached in the Hermes image cache and passed to the agent
  as `media_urls` so tools like `vision_analyze` can inspect them.

No extra configuration is required.

## Typing Indicator

The adapter supports XEP-0085 Chat State Notifications. While the agent is
generating a response, your XMPP client should show a "typing" / "composing"
state. The indicator disappears when the response is sent. No configuration is
required — it is enabled by default via `typing_indicator: true`.
