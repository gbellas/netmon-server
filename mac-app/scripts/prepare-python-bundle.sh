#!/bin/bash
# Prepares the embeddable Python runtime for inclusion in NetMon Server.app.
#
# Downloads a relocatable Python 3.13 build from python-build-standalone,
# pip-installs our server deps into it, and copies the server source
# files. Output lives in mac-app/NetMonServer/Resources/ and is consumed
# by the Xcode "Copy Bundle Resources" phase as a folder reference, so
# everything gets picked up automatically on build.
#
# Run this BEFORE building the Xcode project. Expected wall-clock time:
# ~2-3 minutes (dominated by downloading + pip-installing ~40MB of deps).
#
# Why python-build-standalone: Apple-ships Python frameworks are not
# relocatable — they hard-code install paths in their Mach-O dyld headers.
# python-build-standalone is an upstream project that builds portable
# Pythons explicitly for embedding use cases. Astral (uv, ruff, rye)
# maintains them now.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAC_APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_ROOT="$(cd "$MAC_APP_ROOT/.." && pwd)"
RESOURCES="$MAC_APP_ROOT/NetMonServer/Resources"

# --- Detect architecture ---------------------------------------------------
ARCH=$(uname -m)
case "$ARCH" in
  arm64)  PY_ARCH="aarch64-apple-darwin" ;;
  x86_64) PY_ARCH="x86_64-apple-darwin"  ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

# --- Configurable versions -------------------------------------------------
# Pin the Python version + release tag so rebuilds are deterministic.
# Bump these when Python 3.14 lands or when deps demand a newer toolchain.
PY_VERSION="3.13.13"
# Tag format is the release date. Find latest at:
#   https://github.com/astral-sh/python-build-standalone/releases
# Or: gh api repos/astral-sh/python-build-standalone/releases/latest
PBS_TAG="20260414"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_VERSION}+${PBS_TAG}-${PY_ARCH}-install_only_stripped.tar.gz"

BUNDLE_DIR="$RESOURCES/python-bundle"
SERVER_CODE_DIR="$RESOURCES/server-code"

# --- Clean + prep ----------------------------------------------------------
echo "==> Preparing Resources directory…"
rm -rf "$BUNDLE_DIR" "$SERVER_CODE_DIR"
mkdir -p "$RESOURCES"

# --- Fetch Python ----------------------------------------------------------
TMP_TGZ="/tmp/python-bundle-${PY_VERSION}-${PY_ARCH}.tar.gz"
if [ ! -f "$TMP_TGZ" ]; then
  echo "==> Downloading Python ${PY_VERSION} for ${PY_ARCH}…"
  curl -L --fail -o "$TMP_TGZ" "$PBS_URL"
fi

echo "==> Extracting Python runtime…"
mkdir -p "$BUNDLE_DIR"
# The archive extracts a top-level "python/" directory; strip that so
# bin/ and lib/ live directly inside python-bundle/.
tar -xzf "$TMP_TGZ" -C "$BUNDLE_DIR" --strip-components=1

# --- Install server deps ---------------------------------------------------
echo "==> Installing server dependencies…"
"$BUNDLE_DIR/bin/python3" -m ensurepip --upgrade
"$BUNDLE_DIR/bin/python3" -m pip install --upgrade pip
"$BUNDLE_DIR/bin/python3" -m pip install -r "$SERVER_ROOT/requirements.txt"

# --- Copy server source ----------------------------------------------------
echo "==> Copying server source files…"
mkdir -p "$SERVER_CODE_DIR"
# Explicit list — we don't want tests/, logs/, .venv/, secrets/, etc.
for entry in \
  server.py alerts.py apns.py auth.py bandwidth_meter.py \
  controls.py controls_udm.py models.py scheduled_tasks.py \
  ssh_pause.py ws_manager.py \
  requirements.txt \
  config.yaml \
  pollers static
do
  src="$SERVER_ROOT/$entry"
  if [ -e "$src" ]; then
    cp -R "$src" "$SERVER_CODE_DIR/"
  fi
done

# Remove pycache detritus that pip may have left behind.
find "$BUNDLE_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$SERVER_CODE_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUNDLE_DIR" -name "*.pyc" -delete 2>/dev/null || true

# --- Summary ---------------------------------------------------------------
BUNDLE_SIZE=$(du -sh "$BUNDLE_DIR" | awk '{print $1}')
CODE_SIZE=$(du -sh "$SERVER_CODE_DIR" | awk '{print $1}')
echo ""
echo "== Done =="
echo "  python-bundle : $BUNDLE_SIZE"
echo "  server-code   : $CODE_SIZE"
echo ""
echo "Next: build the Xcode project."
echo "      cd $MAC_APP_ROOT"
echo "      xcodebuild -project NetMonServer.xcodeproj -configuration Release"
