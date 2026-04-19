#!/bin/bash
# Sign the Release build with Developer ID Application and notarize it.
#
# Prereqs:
#  - Developer ID Application cert installed in the Login keychain
#    (check: `security find-identity -v -p codesigning`)
#  - App Store Connect API key at /Users/<you>/NetworkMonitor/secrets/AuthKey_*.p8
#  - ASC_KEY_ID + ASC_ISSUER_ID set (or hardcode below)
#  - The .xcarchive already produced by `xcodebuild archive` (this script
#    does NOT re-run the archive — it starts from an existing one)
#
# Usage:
#   ./sign-and-notarize.sh [ARCHIVE_PATH]
#
# Output: a notarized + stapled .app at /tmp/netmon-mac-signed/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAC_APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_ROOT="$(cd "$MAC_APP_ROOT/.." && pwd)"

ARCHIVE="${1:-/tmp/netmon-mac-archive/NetMonServer.xcarchive}"
if [ ! -d "$ARCHIVE" ]; then
  echo "ERROR: archive not found at $ARCHIVE" >&2
  echo "Run xcodebuild archive first (see README)." >&2
  exit 1
fi

SRC_APP="$ARCHIVE/Products/Applications/NetMon Server.app"
OUT_DIR="/tmp/netmon-mac-signed"
OUT_APP="$OUT_DIR/NetMon Server.app"
mkdir -p "$OUT_DIR"
rm -rf "$OUT_APP"
cp -R "$SRC_APP" "$OUT_APP"

# Identity: match by "Developer ID Application" prefix so this script
# keeps working if the team name changes. Error cleanly on zero matches.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
  | grep -m1 "Developer ID Application" \
  | sed -E 's/^ +[0-9]+\) [A-F0-9]+ "([^"]+)".*/\1/')
if [ -z "$IDENTITY" ]; then
  echo "ERROR: no 'Developer ID Application' cert in your keychain." >&2
  echo "Create one at https://developer.apple.com/account/resources/certificates/add" >&2
  exit 1
fi
echo "==> Signing with: $IDENTITY"

ENTITLEMENTS="$MAC_APP_ROOT/NetMonServer/NetMonServer.entitlements"
if [ ! -f "$ENTITLEMENTS" ]; then
  echo "ERROR: entitlements not found at $ENTITLEMENTS" >&2
  exit 1
fi

# Step 1: sign the nested .so / .dylib / main python3 binary BEFORE
# the outer bundle. codesign's --deep does this too, but explicit
# inside-out signing gives much better error messages when one
# nested file can't be signed.
echo "==> Signing nested Python extensions…"
find "$OUT_APP/Contents/Resources/Resources/python-bundle" \
  \( -name "*.dylib" -o -name "*.so" \) \
  -exec codesign --force --timestamp --options=runtime \
    --sign "$IDENTITY" {} \; >/dev/null 2>&1

echo "==> Signing main Python interpreter…"
codesign --force --timestamp --options=runtime \
  --sign "$IDENTITY" \
  "$OUT_APP/Contents/Resources/Resources/python-bundle/bin/python3.13" >/dev/null

echo "==> Signing outer app bundle with entitlements…"
codesign --force --timestamp --options=runtime \
  --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" \
  "$OUT_APP" >/dev/null

# Step 2: notarize. Zips with ditto (preserves metadata/xattrs).
echo "==> Preparing notarization zip…"
NOTARIZE_ZIP="$OUT_DIR/NetMonServer-notarize.zip"
rm -f "$NOTARIZE_ZIP"
cd "$OUT_DIR" && /usr/bin/ditto -c -k --keepParent "NetMon Server.app" "$NOTARIZE_ZIP"

# Load ASC creds from .env.fastlane (created during iOS TF setup) if the
# shell hasn't already exported them.
FASTLANE_ENV="$SERVER_ROOT/.env.fastlane"
if [ -z "${ASC_KEY_ID:-}" ] && [ -f "$FASTLANE_ENV" ]; then
  set -a; source "$FASTLANE_ENV"; set +a
fi
: "${ASC_KEY_ID:?ASC_KEY_ID not set (see .env.fastlane)}"
: "${ASC_ISSUER_ID:?ASC_ISSUER_ID not set (see .env.fastlane)}"
: "${ASC_KEY_PATH:?ASC_KEY_PATH not set (see .env.fastlane)}"

echo "==> Submitting to Apple notary…"
xcrun notarytool submit "$NOTARIZE_ZIP" \
  --key "$ASC_KEY_PATH" \
  --key-id "$ASC_KEY_ID" \
  --issuer "$ASC_ISSUER_ID" \
  --wait

# Step 3: staple the notarization ticket to the .app so Gatekeeper
# doesn't need to phone home on every launch.
echo "==> Stapling notarization ticket…"
xcrun stapler staple "$OUT_APP"
xcrun stapler validate "$OUT_APP"

echo ""
echo "==> Done. Signed + notarized app at:"
echo "    $OUT_APP"
