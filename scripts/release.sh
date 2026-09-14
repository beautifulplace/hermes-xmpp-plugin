#!/usr/bin/env bash
# Cut a release: verify versions agree, tag, push the public remote, create the
# GitHub release from the matching CHANGELOG section.
#
# Run this from the PUBLIC clone ONLY. This repository is the public mirror; the
# private working copy is a separate clone with its own remote. Never add a
# public remote to the private clone: pushing one tree to both forges is how
# private-forge metadata and private-only files (AGENTS.md) reached the public
# repo before.
#
# Usage: ./scripts/release.sh 1.3.3
set -euo pipefail

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  echo "usage: $0 <version>   (e.g. $0 1.3.0)" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

echo "Verifying version ${VERSION} in all four locations..."

fail=0
check() {
  if [[ "$2" != *"$VERSION"* ]]; then
    echo "  MISMATCH in $1: expected ${VERSION}, found: $2" >&2
    fail=1
  else
    echo "  ok  $1"
  fi
}

check "pyproject.toml" "$(grep -m1 '^version' pyproject.toml)"
check "plugin.yaml"    "$(grep -m1 '^version' plugin.yaml)"
check "__init__.py"    "$(grep -m1 '__version__' __init__.py)"
if ! grep -q "^## \[${VERSION}\]" CHANGELOG.md; then
  echo "  MISSING CHANGELOG section ## [${VERSION}]" >&2
  fail=1
else
  echo "  ok  CHANGELOG.md"
fi

if [[ "${fail}" -ne 0 ]]; then
  echo "" >&2
  echo "Version bump touches four files: pyproject.toml, plugin.yaml, __init__.py, CHANGELOG.md" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is dirty; commit the bump first." >&2
  exit 1
fi

TAG="v${VERSION}"
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "Tag ${TAG} already exists." >&2
  exit 1
fi

echo "Tagging ${TAG}..."
git tag -a "${TAG}" -m "Hermes XMPP Plugin ${VERSION}"

echo "Pushing origin..."
git push origin main
git push origin "${TAG}"

echo "Extracting release notes from CHANGELOG.md..."
NOTES="$(mktemp)"
awk -v ver="${VERSION}" '
  $0 ~ "^## \\[" ver "\\]" {found=1; next}
  found && /^## \[/ {exit}
  found {print}
' CHANGELOG.md > "${NOTES}"

if [[ ! -s "${NOTES}" ]]; then
  echo "No CHANGELOG body found for ${VERSION}" >&2
  rm -f "${NOTES}"
  exit 1
fi

{
  echo "## Hermes XMPP Plugin ${VERSION}"
  echo ""
  cat "${NOTES}"
  echo ""
  echo "### Install"
  echo ""
  echo '```'
  echo "hermes plugins install rebelcommand/hermes-xmpp-plugin --enable"
  echo "python3 ~/.hermes/plugins/xmpp-platform/post_install.py"
  echo "hermes gateway restart"
  echo '```'
  echo ""
  echo "### Upgrade"
  echo ""
  echo '```'
  echo "hermes plugins update xmpp-platform"
  echo "hermes gateway restart"
  echo '```'
} > "${NOTES}.final"

if command -v gh >/dev/null 2>&1; then
  gh release create "${TAG}" \
    -R rebelcommand/hermes-xmpp-plugin \
    --title "Hermes XMPP Plugin ${VERSION}" \
    --notes-file "${NOTES}.final"
  echo "GitHub release created."
else
  echo "gh not found; release notes are in ${NOTES}.final" >&2
fi

rm -f "${NOTES}" "${NOTES}.final"
echo "Done: ${TAG}"
