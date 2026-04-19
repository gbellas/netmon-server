"""APNs (Apple Push Notifications) sender + device token registry.

Uses Apple's token-based provider auth (APNs JWT), not certificate auth:
 - Generate an ES256 JWT signed with our .p8 key
 - Reuse it for up to 1 hour (Apple rejects >1h old; we rotate at 50m)
 - Send to api.sandbox.push.apple.com or api.push.apple.com over HTTP/2

Environment selection (`APNS_ENV`):
 - "sandbox"    → api.sandbox.push.apple.com      (Xcode-installed dev builds)
 - "production" → api.push.apple.com              (TestFlight / App Store)

Device tokens are stored in-memory + persisted to `secrets/push_tokens.json`.
Small scale (single user, handful of devices) so a real DB is overkill.

This module is deliberately quiet on success and verbose on any non-200
response — during the first device-token exchange we'll need every shred
of Apple's error payload to debug misconfigured bundles or environments.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import jwt
import httpx

logger = logging.getLogger("netmon.apns")


class APNsClient:
    """HTTP/2 APNs client with token-based auth and JWT caching."""

    # Apple rotates the JWT every hour; we rotate a bit early to be safe.
    _JWT_LIFETIME_SEC = 55 * 60

    def __init__(self) -> None:
        self._key_path = os.getenv("APNS_KEY_PATH", "")
        self._key_id = os.getenv("APNS_KEY_ID", "")
        self._team_id = os.getenv("APNS_TEAM_ID", "")
        self._bundle = os.getenv("APNS_BUNDLE_ID", "com.gbellas.netmon")
        env = os.getenv("APNS_ENV", "sandbox").lower()
        self._host = (
            "api.sandbox.push.apple.com"
            if env == "sandbox"
            else "api.push.apple.com"
        )
        self._env_label = env
        self._client: httpx.AsyncClient | None = None
        # Cached signed provider token — regenerate when `_jwt_issued_at`
        # drifts past _JWT_LIFETIME_SEC so we don't recompute per-send.
        self._jwt: str | None = None
        self._jwt_issued_at: float = 0.0
        self._key_pem: str | None = None

    @property
    def is_configured(self) -> bool:
        """True when all required .env entries + the .p8 file are present.
        We silently no-op sends if not configured — avoids the server
        dying on boot when the user hasn't set APNs up yet."""
        return (
            self._key_path
            and self._key_id
            and self._team_id
            and Path(self._key_path).is_file()
        )

    def _load_key(self) -> str:
        if self._key_pem is None:
            self._key_pem = Path(self._key_path).read_text()
        return self._key_pem

    def _provider_token(self) -> str:
        """ES256-signed JWT for the Authorization: bearer header. Apple
        accepts the same token for all requests to a given team for up
        to an hour — caching it saves a ~2ms signature per send."""
        now = time.time()
        if self._jwt and (now - self._jwt_issued_at) < self._JWT_LIFETIME_SEC:
            return self._jwt
        headers = {"alg": "ES256", "kid": self._key_id}
        claims = {"iss": self._team_id, "iat": int(now)}
        self._jwt = jwt.encode(
            claims, self._load_key(), algorithm="ES256", headers=headers
        )
        self._jwt_issued_at = now
        return self._jwt

    async def _get_http(self) -> httpx.AsyncClient:
        if self._client is None:
            # HTTP/2 is mandatory for APNs. certifi bundle handles the
            # SSL chain. Reasonable timeouts — a stuck push shouldn't
            # block the alerts engine tick.
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return self._client

    async def send(
        self,
        device_token: str,
        title: str,
        body: str,
        sound: str = "default",
        severity: str = "active",
        rule_id: str | None = None,
    ) -> bool:
        """Send one push to one device. Returns True on 200, False
        otherwise. Non-fatal — we log and keep running."""
        if not self.is_configured:
            logger.warning("APNs not configured; skipping send")
            return False
        if not device_token:
            return False
        client = await self._get_http()
        url = f"https://{self._host}/3/device/{device_token}"
        payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": sound,
                # `time-sensitive` bypasses Focus modes; `active` obeys.
                "interruption-level": (
                    "time-sensitive" if severity == "critical" else "active"
                ),
                "relevance-score": 1.0 if severity == "critical" else 0.5,
            }
        }
        if rule_id:
            payload["rule_id"] = rule_id
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self._bundle,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "content-type": "application/json",
        }
        try:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
        except httpx.HTTPError as e:
            logger.warning(f"APNs send to {device_token[:8]}…: {e}")
            return False
        if resp.status_code == 200:
            return True
        # Apple returns JSON reasons in the body — log them so misconfig
        # is obvious. The apns-id header lets Apple Developer support
        # trace a specific send if it ever comes to that.
        apns_id = resp.headers.get("apns-id", "")
        body_txt = resp.text[:500]
        logger.warning(
            f"APNs {resp.status_code} token={device_token[:8]}… "
            f"apns-id={apns_id} body={body_txt}"
        )
        # BadDeviceToken / Unregistered → tell caller to purge the token.
        return False

    async def send_to_all(
        self,
        tokens: list[str],
        title: str,
        body: str,
        **kwargs,
    ) -> dict[str, int]:
        """Fan-out helper. Returns {"sent": N, "failed": M}."""
        if not tokens:
            return {"sent": 0, "failed": 0}
        results = await asyncio.gather(
            *[self.send(t, title, body, **kwargs) for t in tokens],
            return_exceptions=False,
        )
        sent = sum(1 for r in results if r is True)
        return {"sent": sent, "failed": len(results) - sent}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class DeviceTokenRegistry:
    """Keeps the set of device tokens that have registered for push.

    Single-user app, so we don't bother with per-user partitioning —
    every fired alert fans out to every registered token. If/when this
    becomes multi-user, add user_id to the schema.

    Persisted to `secrets/push_tokens.json` so restarts don't lose the
    set. Tokens expire on reinstall (Apple issues a new one) — we purge
    on any APNs response that reports BadDeviceToken/Unregistered.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._tokens: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                if isinstance(data, list):
                    self._tokens = set(data)
            except Exception as e:
                logger.warning(f"token registry load failed: {e}")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._tokens)))
        tmp.replace(self._path)
        # Keychain-esque perms on the file — contains device IDs that
        # can be used to target push at specific devices. Not secret
        # per se, but private.
        try:
            self._path.chmod(0o600)
        except Exception:
            pass

    def register(self, token: str) -> bool:
        if not token or len(token) > 200:
            return False
        if token in self._tokens:
            return True
        self._tokens.add(token)
        self._save()
        logger.info(f"registered device token {token[:8]}… (total {len(self._tokens)})")
        return True

    def unregister(self, token: str) -> None:
        if token in self._tokens:
            self._tokens.discard(token)
            self._save()
            logger.info(f"unregistered device token {token[:8]}…")

    def all(self) -> list[str]:
        return sorted(self._tokens)

    def count(self) -> int:
        return len(self._tokens)
