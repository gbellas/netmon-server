#!/bin/bash
# Finish notarization once Apple's backlog clears.
#
# Background: on 2026-04-19 we signed the .app + DMG cleanly but Apple's
# notary service was stuck at "In Progress" for hours on every submission.
# When it finally processes, run this script to staple + re-deploy the DMG.

set -euo pipefail
APP="/Users/gbellas/NetworkMonitor/mac-app/build/export/NetMon Server.app"
DMG="/Users/gbellas/NetworkMonitor/mac-app/build/NetMonServer.dmg"
DESKTOP_DMG="$HOME/Desktop/NetMonServer.dmg"
KEY=/Users/gbellas/NetworkMonitor/secrets/AuthKey_2FVB9S49G2.p8
KEY_ID=2FVB9S49G2
ISSUER=b25087e1-d9d0-4b2e-9edd-2c4dac4a432a

APP_SUB=19643f17-7952-4d20-9a9b-17f1ff62292d
DMG_SUB=763da32f-bba5-4eaa-a360-d5ca2e4ebf26

echo "== app submission =="
xcrun notarytool info "$APP_SUB" --key "$KEY" --key-id "$KEY_ID" --issuer "$ISSUER"
echo
echo "== dmg submission =="
xcrun notarytool info "$DMG_SUB" --key "$KEY" --key-id "$KEY_ID" --issuer "$ISSUER"

echo
echo "If both say 'Accepted', run:"
echo "  xcrun stapler staple \"$APP\""
echo "  xcrun stapler staple \"$DMG\""
echo "  cp \"$DMG\" \"$DESKTOP_DMG\""
echo "  xcrun stapler validate \"$DESKTOP_DMG\""
echo "  shasum -a 256 \"$DESKTOP_DMG\""
