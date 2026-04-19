#!/bin/bash
# Package a signed + notarized NetMon Server.app into a distributable DMG.
#
# Assumes sign-and-notarize.sh already ran; picks up its output from
# /tmp/netmon-mac-signed/. If that directory doesn't exist yet, bails.
#
# Output: /tmp/NetMonServer-<version>.dmg
set -euo pipefail

SIGNED_APP="/tmp/netmon-mac-signed/NetMon Server.app"
if [ ! -d "$SIGNED_APP" ]; then
  echo "ERROR: signed app not found at $SIGNED_APP" >&2
  echo "Run sign-and-notarize.sh first." >&2
  exit 1
fi

# Pull version from Info.plist so DMG name stays in sync with the app
VERSION=$(/usr/libexec/PlistBuddy \
  -c "Print :CFBundleShortVersionString" \
  "$SIGNED_APP/Contents/Info.plist" 2>/dev/null || echo "0.1")

STAGE="/tmp/netmon-dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$SIGNED_APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/README.txt" <<EOF
NetMon Server ${VERSION}

Install:
  1. Drag "NetMon Server" onto the "Applications" shortcut in this
     window.
  2. Open Applications and double-click NetMon Server. A small icon
     appears in your menu bar.
  3. Click the icon → "Run setup…" to pair with the NetMon iPhone app.

The installer is signed + notarized by Apple — no Gatekeeper warnings,
no right-click workaround needed.

Support: https://github.com/gbellas/netmon-server
EOF

DMG="/tmp/NetMonServer-${VERSION}.dmg"
rm -f "$DMG"

hdiutil create \
  -volname "NetMon Server" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$DMG"

# Sign the DMG itself so it also doesn't trigger Gatekeeper when users
# double-click it from a browser download. Uses the same Developer ID
# Application identity as the .app.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
  | grep -m1 "Developer ID Application" \
  | sed -E 's/^ +[0-9]+\) [A-F0-9]+ "([^"]+)".*/\1/')
if [ -n "$IDENTITY" ]; then
  codesign --sign "$IDENTITY" --timestamp "$DMG"
  echo "==> DMG signed."
fi

# Notarize + staple the DMG too. Same API key from .env.fastlane.
FASTLANE_ENV="/Users/gbellas/NetworkMonitor/.env.fastlane"
if [ -z "${ASC_KEY_ID:-}" ] && [ -f "$FASTLANE_ENV" ]; then
  set -a; source "$FASTLANE_ENV"; set +a
fi
if [ -n "${ASC_KEY_ID:-}" ]; then
  echo "==> Notarizing DMG…"
  xcrun notarytool submit "$DMG" \
    --key "$ASC_KEY_PATH" \
    --key-id "$ASC_KEY_ID" \
    --issuer "$ASC_ISSUER_ID" \
    --wait
  xcrun stapler staple "$DMG"
fi

echo ""
ls -lh "$DMG"
echo ""
echo "==> Done: $DMG"
