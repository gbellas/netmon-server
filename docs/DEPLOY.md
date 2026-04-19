# Deploying the NetMon server

NetMon is architected as a separate always-on server that polls your
network devices and streams state to the iPhone app. The server is the
part you run on your own hardware; the app just visualizes what the
server reports.

This guide covers deploying from scratch on a Mac (recommended) or Linux
host. Budget ~15 minutes end to end.

## Table of contents

1. [Prerequisites](#prerequisites)
2. [Install](#install)
3. [First-run configuration](#first-run-configuration)
4. [Running the server](#running-the-server)
5. [Connecting the iPhone app](#connecting-the-iphone-app)
6. [Adding your first device](#adding-your-first-device)
7. [Auto-start on login (macOS)](#auto-start-on-login-macos)
8. [Updating the server](#updating-the-server)
9. [Optional: push notifications](#optional-push-notifications)
10. [Troubleshooting](#troubleshooting)
11. [Hardening for non-LAN exposure](#hardening-for-non-lan-exposure)

## Prerequisites

- **Python 3.11+** — check with `python3 --version`. On macOS the
  simplest path is [Python.org installer](https://www.python.org/downloads/).
  Homebrew's `python@3.13` also works.
- **Git** — `git --version` (preinstalled on macOS with Xcode CLT).
- A machine that stays on. A Mac mini / Mac Studio in a closet is ideal;
  a RasPi 4 works too. Laptops that sleep don't.
- **Network access to your routers** — the server polls them over HTTPS
  (UniFi, Peplink REST) or SSH (Peplink `support ping`), so it must
  be able to reach them at their LAN IPs.
- **For iPhone push notifications (optional):** an Apple Developer
  account and a generated APNs auth key. See
  [Optional: push notifications](#optional-push-notifications).

## Install

```bash
git clone https://github.com/gbellas/netmon-server.git ~/NetworkMonitor
cd ~/NetworkMonitor
cp .env.example .env
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

The venv is self-contained — uninstalling is `rm -rf ~/NetworkMonitor`.

## First-run configuration

### 1. Generate the API token

Start the server once to generate your API bearer token:

```bash
./run.sh
```

On first start the server writes a fresh `NETMON_API_TOKEN=...` line to
`.env` and logs it. Copy the token — the iPhone app needs it.

**If you accidentally miss the log line:**

```bash
grep NETMON_API_TOKEN .env
```

### 2. Lock down the `.env` file

```bash
chmod 600 .env
```

The file contains passwords for your network devices. Anyone who can
read it gets admin on your network.

### 3. (Optional) Start from the example config

`config.yaml` ships in the repo as a working example with 3 placeholder
devices (gateway, balance, truck). Your real setup goes in
**`config.local.yaml`** — the server prefers it over `config.yaml` when
present, and `.gitignore` excludes it so your IPs never land in git.

```bash
cp config.yaml config.local.yaml
chmod 600 config.local.yaml
```

You can hand-edit this file, **but usually you don't need to** — once the
server is running and the iPhone is connected, add devices through the
app's Settings → Devices → **+** button.

## Running the server

### Foreground (development)

```bash
./run.sh
```

Listens on `0.0.0.0:8077`. Visit `http://localhost:8077/api/health` to
confirm — should return `{"ok":true, ...}` without needing auth.

### Background (daemon)

- **macOS:** see [Auto-start on login](#auto-start-on-login-macos)
- **Linux:** a systemd unit file isn't shipped yet but is trivial; open
  an issue if you need one.

## Connecting the iPhone app

1. Install the app from TestFlight.
2. Open it → tap the gear icon → **Server URL** = the Mac's IP + port,
   e.g. `http://192.168.1.10:8077`. Use the server machine's LAN IP,
   not `localhost`. If you've set the server's `.local` hostname (e.g.
   via System Settings → Sharing), `http://netmon.local:8077` works too.
3. **API token** = paste the string from the server's `.env` file.
4. Tap **Save**. The dashboard should populate within ~5s.

If the dashboard stays empty, see [Troubleshooting](#troubleshooting).

## Adding your first device

Two paths:

### A. Through the app (recommended)

Settings → **Devices** → **+** button. Pick a kind, fill in host +
username + password, save. The server persists the change to
`config.local.yaml` and starts polling immediately — no restart needed.

Supported kinds:

| Kind | For |
|---|---|
| `peplink_router` | BR1/MAX/MBX/Balance series routers (REST + optional SSH) |
| `unifi_network`  | UDM, UDM Pro, UDM SE, Cloud Gateway Ultra, Dream Machine |
| `icmp_ping`      | A named bundle of ICMP targets for LAN reachability checks |

### B. By hand-editing `config.local.yaml`

Add to the `devices:` map, following the format in `config.yaml`.
Restart the server with:

```bash
launchctl kickstart -k gui/$(id -u)/com.gbellas.netmon   # macOS launchd
# or Ctrl-C + ./run.sh if running in foreground
```

## Auto-start on login (macOS)

```bash
./scripts/install_launchd.sh
```

This renders the included `launchd/com.gbellas.netmon.plist` for your
home directory, drops it in `~/Library/LaunchAgents/`, and loads it.
The server will now start automatically when you log in and restart if
it crashes.

Logs: `~/NetworkMonitor/logs/netmon.{out,err}`

Stop / disable:

```bash
launchctl unload ~/Library/LaunchAgents/com.gbellas.netmon.plist
```

## Updating the server

```bash
cd ~/NetworkMonitor
git pull
./.venv/bin/pip install -r requirements.txt   # only if requirements changed
launchctl kickstart -k gui/$(id -u)/com.gbellas.netmon   # if under launchd
```

Your `.env` + `config.local.yaml` + `secrets/` are gitignored and
survive `git pull` untouched.

## Optional: push notifications

Lock-screen notifications ("truck offline", "high packet loss", etc.)
require the server to be authorized to send APNs pushes to your iPhone.

1. Generate a key at https://developer.apple.com → Certificates,
   Identifiers & Profiles → **Keys** → **+**
2. Enable **Apple Push Notifications service (APNs)**, choose
   **Sandbox & Production** environment, **Team Scoped**
3. Download the `.p8` file → save as
   `~/NetworkMonitor/secrets/AuthKey_<KEY-ID>.p8`
4. `chmod 600 secrets/*.p8`
5. Edit `.env`:
   ```
   APNS_KEY_PATH=/Users/<you>/NetworkMonitor/secrets/AuthKey_<KEY-ID>.p8
   APNS_KEY_ID=<KEY-ID>
   APNS_TEAM_ID=<your 10-char team ID from developer.apple.com>
   APNS_BUNDLE_ID=com.gbellas.netmon
   APNS_ENV=sandbox    # use "production" for TestFlight/App Store builds
   ```
6. Restart the server
7. On the iPhone: Settings → Notifications → **Enable** (grants iOS
   permission + triggers device-token registration with your server)

When an alert fires you'll see a push on your lock screen.

## Troubleshooting

### `/api/health` returns 200 but the iPhone dashboard is empty

- Token mismatch. In the app: Settings → paste the token from `.env`
  exactly.
- Server URL unreachable. Try `curl http://<server-ip>:8077/api/health`
  from another device on the same network. If that fails, macOS firewall
  is likely blocking — System Settings → Network → Firewall → Options →
  allow Python or disable the firewall for the moment.

### Balance 310 / BR1 SSH fails with `Permission denied`

- Double-check `NETMON_<DEVICE_ID>_PASSWORD` in `.env` matches the
  actual Peplink admin password. The env var name is derived from the
  device's `id:` in config, uppercased: id `br1` → `NETMON_BR1_PASSWORD`.

### Server won't start under launchd but `./run.sh` works

- Launchd has a sparse environment. The installer script fixes this by
  running `run.sh` (which sources `.env`) instead of `uvicorn` directly.
  If you installed the plist manually, make sure its `ProgramArguments`
  points at `run.sh`, not `uvicorn`.

### Devices added via the app disappear after server restart

- Check that `config.local.yaml` was written and is readable:
  ```bash
  ls -la ~/NetworkMonitor/config.local.yaml
  ```
  Expected: `-rw------- 1 <you> staff ...`. If it's missing, the write
  failed — check `logs/netmon.err`.

### iPhone app shows "legacy" chip on all devices

- Your `config.local.yaml` is pre-v1.0 format. Editing any device via
  the app rewrites its entry in the new `kind:`-based format. The
  chip will disappear on next refresh.

## Hardening for non-LAN exposure

The default setup assumes the server is behind your LAN / VPN. If you
want to reach it from outside (e.g. from a phone on cellular), do
**one** of:

- **Tailscale** (recommended — free, 5 min setup):
  install on the server + on your phone, use the server's Tailscale IP
  as the server URL in the iPhone app.
- **WireGuard** or **OpenVPN** to your home network, same pattern.
- **Cloudflare Tunnel** for public HTTPS without opening ports.

**Do not** simply port-forward `:8077` to the internet. The server
speaks plain HTTP; without a TLS terminator in front of it, your API
token would travel in cleartext. Tailscale / WireGuard handle encryption
transparently so the server itself stays simple.

If you do need direct public HTTPS, run nginx or Caddy in front of it:

```
# Caddyfile
netmon.example.com {
    reverse_proxy localhost:8077
}
```

Caddy auto-provisions a Let's Encrypt cert.
