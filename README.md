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
2. Install missing Python dependencies into a `deps` subdirectory under the plugin (no changes to your system or Hermes virtual environment)
3. Back up your config before modifying it
4. Prompt you for your XMPP JID, password, and optional avatar image path
5. Enable `platforms/xmpp` in `~/.hermes/config.yaml`
6. Add a default `platforms.xmpp` block populated with your credentials
7. Append `XMPP_USER_JID` and `XMPP_PASSWORD` to your Hermes `.env` file

You can also pass credentials on the command line for non-interactive installs:

```bash
python3 install_xmpp_plugin.py --non-interactive \
                               --jid hermes@example.com \
                               --password your-password \
                               --avatar-path /path/to/avatar.png
```

### Install options

```text
python3 install_xmpp_plugin.py --hermes-home /path/to/hermes \
                               --python /path/to/python \
                               --force \
                               --only-required-deps
```

- `--hermes-home`: target a non-default Hermes home (default: `$HERMES_HOME` or `~/.hermes`)
- `--python`: Python interpreter to use for dependency detection/installs (default: Hermes venv python, then current interpreter)
- `--force`: overwrite an existing plugin installation
- `--only-required-deps`: skip optional dependencies (`slixmpp-omemo`, `edge-tts`)
- `--no-defaults`: do not add a default `platforms.xmpp` block
- `--non-interactive`: skip prompts; requires `--jid` and `--password` unless `--no-defaults` is used
- `--jid`: XMPP JID
- `--password`: XMPP password
- `--avatar-path`: path to an avatar image (optional; at least 480x480 recommended)

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
    omemo_enabled: true            # optional; requires slixmpp-omemo
    omemo_allow_untrusted: true    # optional; auto-trust new OMEMO devices
    avatar_path: "/path/to/avatar.png"  # optional
```

The JID domain is used automatically to determine the XMPP server, so `server` and `port` are normally not needed.

Then restart the gateway:

```bash
hermes gateway restart
```

## Uninstall

```bash
python3 uninstall_xmpp_plugin.py
```

This removes the plugin directory, disables `platforms/xmpp` in `config.yaml`, and creates a config backup. By default it interactively asks whether to remove the `platforms.xmpp` block as well.

- `--keep-config`: keep the `platforms.xmpp` block without prompting
- `--non-interactive`: skip the prompt and remove the `platforms.xmpp` block

## License

MIT
