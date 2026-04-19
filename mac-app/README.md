# NetMon Server — Mac menu-bar app

A signed + notarized `.app` that embeds the Python FastAPI server and
runs it in the background with a menu-bar status icon. Lets a Mac user
run their own NetMon server without Terminal / git / pip.

## What's in this directory

```
mac-app/
├── NetMonServer.xcodeproj       # Generated via /tmp/generate_mac_project.rb
├── NetMonServer/
│   ├── NetMonServerApp.swift    # @main + MenuBarExtra + window scenes
│   ├── ServerController.swift   # subprocess lifecycle + token/env management
│   ├── MenuBarView.swift        # dropdown menu
│   ├── SetupView.swift          # first-run wizard with QR code
│   ├── PreferencesView.swift    # status + log tail window
│   ├── Info.plist
│   ├── NetMonServer.entitlements
│   └── Resources/               # populated by prepare-python-bundle.sh
│       ├── python-bundle/       # python-build-standalone 3.13.13
│       └── server-code/         # copy of server.py, pollers/, etc.
└── scripts/
    ├── prepare-python-bundle.sh # downloads Python + pip-installs deps
    ├── sign-and-notarize.sh     # Developer ID sign + Apple notary
    └── build-dmg.sh             # final .dmg with drag-to-Applications UI
```

## Build pipeline

**One-time setup:**

1. Generate a Developer ID Application cert at
   https://developer.apple.com/account/resources/certificates/add
   (type: Developer ID Application; upload a CSR; download + install in
   Keychain). Details in the main repo's DEPLOY.md.
2. Create an App Store Connect API key (same one used for iOS TestFlight
   works — `.env.fastlane` in the repo root has the values).

**Per-release:**

```bash
# 1. Bundle Python + deps into Resources/
./scripts/prepare-python-bundle.sh

# 2. Archive (Xcode builds a Release .xcarchive)
xcodebuild -project NetMonServer.xcodeproj \
  -scheme NetMonServer \
  -configuration Release \
  -archivePath /tmp/netmon-mac-archive/NetMonServer.xcarchive \
  archive

# 3. Sign with Developer ID + notarize (~5 min wait)
./scripts/sign-and-notarize.sh

# 4. Package into a DMG
./scripts/build-dmg.sh
```

Output: `/tmp/NetMonServer-<version>.dmg` — signed, notarized, stapled.
Double-clickable on any Mac with macOS 14+.

## Runtime layout

Inside the installed .app:
```
/Applications/NetMon Server.app/
├── Contents/
│   ├── MacOS/NetMon Server       # Swift menu-bar binary
│   └── Resources/Resources/      # folder ref — nested one level
│       ├── python-bundle/        # full Python runtime (~94 MB)
│       └── server-code/          # server.py + everything it imports
```

On first launch, the Swift app copies `server-code/` out to
`~/Library/Application Support/NetMonServer/` so the Python process has
a writable directory for `.env`, `config.local.yaml`, `alerts_config.json`
and other runtime state. The app bundle itself stays read-only (required
for codesigned apps).

## Logs

The menu-bar app captures the Python subprocess's stdout/stderr in a
rolling 500-line buffer viewable via menu → Preferences → Logs tab. The
subprocess does NOT also write to disk — if you need persistent logs,
open Preferences while the issue is reproducing.

Launch-level logs (e.g. "Python interpreter couldn't start") go to
Console.app under the process name `NetMon Server`.

## Known limitations

- **Signing blocker on first build:** creating a Developer ID Application
  cert is a one-time interactive step. Apple's API key roles we have
  (App Manager) can't auto-create that cert type; upgrading the key role
  to Admin would let `xcodebuild -allowProvisioningUpdates` handle it.
- **Bundle size:** 108 MB installed is dominated by the embedded Python
  (94 MB). Trimming unused standard-library modules could drop that to
  ~70 MB, but introduces failure modes if a dep ever imports a trimmed
  module.
- **Cross-arch:** the bundled Python is native-only (aarch64 on Apple
  Silicon, x86_64 on Intel). Intel support works but requires rebuilding
  with the x86_64 PBS tarball; multi-arch universal2 python-build-
  standalone builds aren't a thing yet.
