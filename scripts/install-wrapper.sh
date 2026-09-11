#!/usr/bin/env bash
# Optional convenience wrapper around the native Hermes plugin commands.
#
# Installs ~/.local/bin/xmpp-upgrade, a thin shell script that runs
# `hermes plugins update` and a gateway restart, then reports the version.
# It is a convenience only: upgrades work without it.
#
# Usage: ./scripts/install-wrapper.sh
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/xmpp-upgrade"
PLUGIN_NAME="xmpp-platform"

mkdir -p "${BIN_DIR}"

cat > "${TARGET}" <<'WRAPPER'
#!/usr/bin/env bash
# Convenience wrapper: upgrade the Hermes XMPP plugin and restart the gateway.
# All real work is done by native Hermes commands.
set -euo pipefail

PLUGIN_NAME="xmpp-platform"

if ! command -v hermes >/dev/null 2>&1; then
  echo "error: 'hermes' is not on PATH" >&2
  exit 1
fi

before="$(hermes plugins show "${PLUGIN_NAME}" 2>/dev/null | head -1 || true)"
echo "Upgrading ${PLUGIN_NAME}..."

if ! hermes plugins update "${PLUGIN_NAME}"; then
  echo "" >&2
  echo "Update failed. If this says the plugin has no .git directory or that it" >&2
  echo "is pinned, reinstall it from the repository root:" >&2
  echo "  hermes plugins install beautifulplace/hermes-xmpp-plugin --enable" >&2
  exit 1
fi

echo "Restarting the gateway..."
hermes gateway restart

echo ""
echo "Before: ${before:-unknown}"
echo "After:  $(hermes plugins show "${PLUGIN_NAME}" 2>/dev/null | head -1 || echo unknown)"
WRAPPER

chmod +x "${TARGET}"

echo "Installed ${TARGET}"
echo "Run 'xmpp-upgrade' to upgrade the plugin and restart the gateway."
echo "Ensure ${BIN_DIR} is on your PATH."
