# NetMon Server — Release Process

How to ship a new tagged release of the Mac menu-bar app, and how to verify
the built artifact works end-to-end on a clean Mac.

---

## 1. Cutting a release

All releases are driven by a `vX.Y.Z` git tag. Pushing the tag triggers
`.github/workflows/release.yml`, which builds, signs, notarizes, and staples
the DMG, then creates a GitHub Release with `NetMonServer.dmg` attached.

```bash
# 1. Bump the version in the source.
#    This string is what /api/version returns and what the iOS app displays.
$EDITOR version.py          # update __version__ = "X.Y.Z"
git add version.py
git commit -m "release: vX.Y.Z"

# 2. Create an annotated tag.
git tag -a vX.Y.Z -m "NetMon Server vX.Y.Z"

# 3. Push main + the tag. Pushing the tag is what triggers the workflow.
git push origin main
git push origin vX.Y.Z
```

Watch the run at
`https://github.com/gbellas/netmon-server/actions/workflows/release.yml`.
On success the release appears at `/releases/tag/vX.Y.Z` with
`NetMonServer.dmg` attached. The landing page's download button always
points at `/releases/latest/download/NetMonServer.dmg`, so no change is
needed to `docs/site/`.

### Dry run (no tag, no release)

From the Actions tab, choose **Release → Run workflow** on `main`. This
builds + signs + notarizes the DMG and uploads it as a workflow artifact,
but does **not** create a GitHub Release. Use this to validate signing
secrets or script changes without burning a tag.

---

## 2. Seeding the repo secrets

Set these in `Settings → Secrets and variables → Actions → New repository secret`.

| Secret | How to get the value |
| --- | --- |
| `DEVELOPER_ID_CERT_P12_BASE64` | Export your "Developer ID Application" cert from Keychain Access as `.p12`. Then: `base64 -i NetMon-DeveloperID.p12 \| pbcopy`. Paste as the secret value. |
| `DEVELOPER_ID_CERT_PASSWORD` | The export password you chose when creating the `.p12`. |
| `ASC_KEY_BASE64` | Create a new App Store Connect API key at `https://appstoreconnect.apple.com/access/integrations/api` with **Developer** role. Download the `.p8` (you only get it once). Then: `base64 -i AuthKey_XXXXXXXXXX.p8 \| pbcopy`. |
| `ASC_KEY_ID` | The 10-char alphanumeric key ID shown in App Store Connect next to the key you just created. |
| `ASC_ISSUER_ID` | The UUID at the top of the Keys tab in App Store Connect. Same value for every key in your team. |
| `DEV_TEAM_ID` | Your 10-char Apple Developer Team ID. Find it at `https://developer.apple.com/account` → Membership. |

All six are required. The workflow fails fast if any are missing or empty.

---

## 3. Verifying the DMG on a clean Mac

Before announcing a release, smoke-test the artifact as a non-developer would
see it. Use a second Mac, a fresh user account, or (if available) a clean VM.

1. Open the release page in Safari and click **NetMonServer.dmg** — the file
   should download without any "unidentified developer" warning.
2. Double-click the DMG. Gatekeeper should mount it silently (no
   "Apple could not verify..." dialog). If you see that dialog, the DMG
   wasn't stapled — the workflow's `finish-notarization.sh` step failed
   silently. Re-run the workflow.
3. Drag **NetMon Server** to `/Applications`, then launch it from Launchpad
   (not by double-clicking inside the DMG). First launch should prompt once
   for local network permission and nothing else.
4. Verify:
   - The menu-bar icon appears.
   - Clicking it opens the setup window with a QR code.
   - `curl -sS http://localhost:8765/api/version` returns the tag you cut.
5. Pair a phone: open the iOS app, tap **Scan QR**, and confirm devices
   start appearing within ~5 seconds.
6. Quit NetMon Server, delete it from `/Applications`, empty Trash. Confirm
   relaunch from a fresh download still works (shakes out Keychain /
   Application Support leftovers).

Any failure → do **not** delete the tag. Cut `vX.Y.Z+1` with the fix. Tags
are immutable in practice because downstream caches (Homebrew, direct
links) pin to them.

---

## 4. GitHub Pages — one-time manual step

The landing page in `docs/site/` is served by GitHub Pages. The workflow
cannot toggle this; it must be enabled once in the repo settings:

1. Go to `Settings → Pages` on the `netmon-server` repo.
2. Under **Build and deployment**:
   - **Source:** `Deploy from a branch`
   - **Branch:** `main` / folder `/docs/site`
3. Save. The first deploy takes ~60 seconds. The site appears at
   `https://gbellas.github.io/netmon-server/`.

After that, any push to `main` that touches `docs/site/**` redeploys
automatically — no workflow involvement needed.

Drop a real screenshot at `docs/site/hero.png` (the landing page already
references it) whenever it's ready.
