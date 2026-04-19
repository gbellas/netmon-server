# NetMon Server — Mac installation

## Install (signed + notarized DMG)

1. **Double-click `NetMonServer.dmg`** — the installer window opens.
2. **Drag `NetMon Server.app` onto the `Applications` shortcut** inside
   the installer window.
3. **Open Applications** → double-click **NetMon Server**. A small
   network icon appears in your menu bar.
4. **Grant permissions on first run.** macOS may ask for local
   network / notifications access. Click *Allow*.
5. Click the menu-bar icon → **Run setup…** A setup sheet shows a QR
   code and a short pairing token.
6. **In the NetMon iPhone app**, tap *Add server* → *Scan QR* (or
   paste the token). The phone + Mac pair over your LAN; push
   notifications start flowing.

The DMG is signed with *Developer ID Application: GABRIEL JOSE BELLAS
(9ZB72X2V54)* and notarized by Apple, so Gatekeeper lets it through
with no right-click-to-open workaround.

## First-run config

The bundled server ships with `config.example.yaml` as a starting
point. On first launch the app writes a real `config.yaml` to
`~/Library/Application Support/NetMon Server/` and opens it for you to
edit. Fill in your routers, set passwords via `NETMON_*` env vars (see
the `.env` template in the same directory), then flip the menu-bar
toggle to **Start server**.

## Logs

- Menu-bar icon → *Show logs…* tails the live server log.
- On-disk: `~/Library/Logs/NetMon Server/server.log`.

## Uninstall

Drag `NetMon Server.app` from Applications to the Trash. To remove all
state as well:

```
rm -rf ~/Library/Application\ Support/NetMon\ Server
rm -rf ~/Library/Logs/NetMon\ Server
```
