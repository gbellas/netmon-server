"""NetMon API auth — a single long-lived bearer token shared between server
and all clients.

Why not full OAuth/JWT: this is a LAN tool for one household. A shared bearer
token behind the user's VPN is the right ergonomics. One secret, stored in
.env on the server and in Keychain on the client.

The token is read from NETMON_API_TOKEN at startup. If unset, the server
generates a fresh one, persists it to .env, and logs a warning with the
value so the operator can copy it into the app.

Behaviour:
 - All `/api/*` endpoints require `Authorization: Bearer <token>` OR an
   `Authorization` header in the query string `?token=<token>` (useful for
   WebSocket clients that can't set arbitrary headers).
 - `/api/state` is read-only but still requires the token — it exposes GPS
   and other telemetry; there's no meaningful case where we'd want it open.
 - `/ws` authenticates on handshake via `?token=<token>` query param.
 - `/api/health` is open (no auth) so external watchdog scripts can poll it
   without needing the secret.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, Query, WebSocket

logger = logging.getLogger("netmon.auth")

_TOKEN: Optional[str] = None
_ENV_PATH = Path(__file__).parent / ".env"


def _load_from_env_file() -> Optional[str]:
    if not _ENV_PATH.exists():
        return None
    m = re.search(r"^NETMON_API_TOKEN=([A-Za-z0-9_\-]+)$",
                  _ENV_PATH.read_text(), re.MULTILINE)
    return m.group(1) if m else None


def _persist_to_env_file(token: str) -> None:
    """Append NETMON_API_TOKEN=<token> to .env. Never overwrites an existing
    line; if one exists we short-circuit before calling this."""
    try:
        existing = _ENV_PATH.read_text() if _ENV_PATH.exists() else ""
    except Exception:
        existing = ""
    sep = "" if existing.endswith("\n") or not existing else "\n"
    _ENV_PATH.write_text(existing + sep + f"NETMON_API_TOKEN={token}\n")


def init_token() -> str:
    """Called once at server startup. Returns the active token."""
    global _TOKEN
    # Prefer env var (lets ops override without editing files)
    t = os.environ.get("NETMON_API_TOKEN") or _load_from_env_file()
    if not t:
        # 256 bits of entropy, URL-safe, no padding.
        t = secrets.token_urlsafe(32)
        _persist_to_env_file(t)
        logger.warning(
            "No NETMON_API_TOKEN configured; generated a new one. "
            "Copy this into the NetMon app Settings → API token:\n\n    %s\n",
            t,
        )
    _TOKEN = t
    # Also export to env so subprocesses/tests can see it.
    os.environ["NETMON_API_TOKEN"] = t
    return t


def current_token() -> str:
    """Return the in-memory token. Raises if auth hasn't been initialized."""
    if _TOKEN is None:
        raise RuntimeError("auth.init_token() was not called before handling requests")
    return _TOKEN


# ---- FastAPI dependencies ----

def _extract_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def require_auth(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    """FastAPI dependency: enforces bearer token on REST endpoints.

    Accepts `Authorization: Bearer <t>` or `?token=<t>` query param (the
    latter helps when a client can't set headers — e.g. an `<img>` tag or
    a naive curl test). The token is compared with `secrets.compare_digest`
    to avoid timing side-channels."""
    provided = _extract_bearer(authorization) or token
    if provided is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(provided, current_token()):
        raise HTTPException(status_code=401, detail="invalid bearer token")


async def verify_ws_token(ws: WebSocket) -> bool:
    """WebSocket-flavored auth. Accept if either query `?token=...` or
    `Authorization: Bearer ...` header matches. Returns False on mismatch;
    caller should `await ws.close(code=1008)` on False."""
    q_tok = ws.query_params.get("token")
    h_tok = _extract_bearer(ws.headers.get("authorization"))
    provided = q_tok or h_tok
    if not provided:
        return False
    try:
        return secrets.compare_digest(provided, current_token())
    except RuntimeError:
        return False
