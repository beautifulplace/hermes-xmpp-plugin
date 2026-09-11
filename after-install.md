# XMPP plugin installed

Two more steps, then restart the gateway.

1. Finish setup (dependencies, default config, allowlist, home channel):

   ```
   python3 ~/.hermes/plugins/xmpp-platform/post_install.py
   ```

2. Restart the gateway:

   ```
   hermes gateway restart
   ```

Upgrades later: `hermes plugins update xmpp-platform` then `hermes gateway restart`.
