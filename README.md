# NetMon server

FastAPI + asyncio service that polls network devices (UniFi UDM, Peplink
routers, InControl 2, generic ICMP targets) and streams state to the
NetMon iPhone app over a WebSocket.

This is the **server half**. The iPhone app lives at
[netmon-app](https://github.com/gbellas/netmon-app).

## Status

Pre-v1. The architecture is working for the author's specific setup
(1× UniFi UDM, 1× Peplink Balance 310, 1× Peplink BR1 Pro 5G over
SpeedFusion) but device polling is still device-name-coupled. The
driver-based refactor is in progress — track it under
[Issues](https://github.com/gbellas/netmon-server/issues).

## Quick start

```bash
git clone https://github.com/gbellas/netmon-server.git
cd netmon-server
cp .env.example .env           # fill in API token + device passwords + APNs key
cp config.yaml config.local.yaml   # edit config.local.yaml with YOUR device IPs
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./run.sh                       # listens on :8077
```

On first launch the server generates `NETMON_API_TOKEN` in `.env` and
logs it. Paste it into the iPhone app's Settings > API token.

To install as a launchd agent that restarts at login:

```bash
./scripts/install_launchd.sh
```

## Architecture

- `server.py` — FastAPI app, REST + WebSocket handlers
- `pollers/` — one module per device family; each exposes a `run()` coroutine
- `models.py` — in-memory `AppState` with rolling history buffer
- `ws_manager.py` — broadcast to connected WS clients, idle detection
- `alerts.py` — rule engine (threshold crossing + hysteresis)
- `apns.py` — Apple Push Notifications sender (token-based JWT auth)
- `auth.py` — Bearer-token middleware + WebSocket auth

## Configuration

- `.env` (gitignored) — secrets: API token, device passwords, APNs keys
- `config.local.yaml` (gitignored) — operator's real device inventory
- `config.yaml` — committed example, copied to `config.local.yaml` on setup

## Security

- Every `/api/*` route requires `Authorization: Bearer <token>` except
  `/api/health` (watchdog-friendly)
- WebSocket auth happens before `ws.accept()` to prevent unauth connections
- APNs key (`.p8`) + push-token registry stored in `secrets/` (0600 perms)
- `.env` on disk should be `chmod 600`

## License

MIT — see [LICENSE](LICENSE).
