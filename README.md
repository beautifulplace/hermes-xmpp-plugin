# Hermes XMPP Platform Plugin

XMPP gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Connects the agent to an XMPP server, routes messages, supports inbound/outbound media, and optionally uses [OMEMO](https://xmpp.org/extensions/xep-0384.html) end-to-end encryption.

## Features

- Plain-text or OMEMO-encrypted messaging
- XEP-0085 typing indicators
- XEP-0333 read receipts (chat markers)
- Inbound image and file download, including `aesgcm://` OMEMO media sharing
- Outbound voice messages via `edge-tts` + HTTP File Upload (optional)
- XEP-0084 / XEP-0153 avatar publishing (optional)

## Requirements

- Python 3.10+
- Hermes Agent installed
- Core dependencies: `slixmpp`, `httpx`, `Pillow`, `cryptography`
- Optional:
  - `slixmpp-omemo` for OMEMO encryption
  - `edge-tts` and `ffmpeg` for voice replies

## Install

```bash
tar -xzf hermes-xmpp-plugin.tar.gz
cd hermes-xmpp-plugin
python3 install_xmpp_plugin.py
```

The installer will:
1. Copy the plugin into `~/.hermes/plugins/platforms/xmpp`
2. Enable `platforms/xmpp` in `~/.hermes/config.yaml`
3. Install missing Python dependencies into the Hermes virtual environment
4. Back up your config before modifying it

### Install options

```text
python3 install_xmpp_plugin.py --hermes-home /path/to/hermes \
                               --python /path/to/python \
                               --force \
                               --only-required-deps
```

- `--hermes-home`: target a non-default Hermes home (default: `$HERMES_HOME` or `~/.hermes`)
- `--python`: Python interpreter to use for dependency installs (default: Hermes venv python)
- `--force`: overwrite an existing plugin installation
- `--only-required-deps`: skip optional dependencies (`slixmpp-omemo`, `edge-tts`)
- `--no-defaults`: do not add a default `platforms.xmpp` block

## Configure

Edit `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - platforms/xmpp

platforms:
  xmpp:
    enabled: true
    user_jid: "hermes@example.com"
    password: "your-password"
    server: "example.com"          # optional; uses JID domain if omitted
    port: 5222                     # optional
    omemo_enabled: true            # optional; requires slixmpp-omemo
    omemo_allow_untrusted: true  # optional; auto-trust new OMEMO devices
    avatar_path: "/path/to/avatar.png"  # optional
```

Then restart the gateway:

```bash
hermes gateway restart
```

## Uninstall

```bash
python3 uninstall_xmpp_plugin.py
```

This removes the plugin directory, disables it in `config.yaml`, and creates a config backup. Use `--remove-config` to also delete the `platforms.xmpp` block.

## License

MIT
