"""APNs sender tests.

Avoids real network — validates JWT signing, payload shape, and
registry persistence. The actual HTTP round-trip is covered by the
integration tests (tests/test_integration.py).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
import pytest

from apns import APNsClient, DeviceTokenRegistry


# ES256 requires an EC P-256 key. Smallest valid .p8 for tests:
_SAMPLE_P8 = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgevZzL1gdAFr88hb2
OF/2NxApJCzGCEDdfSp6VQO30hyhRANCAAQRWz+jn65BtOMvdyHKcvjBeBSDZH2r
1RTwjmYSi9R/zpBnuQ4EiMnCqfMPWiZqB4QdbAd0E7oH50VpuZ1P087G
-----END PRIVATE KEY-----
"""


@pytest.fixture
def p8_key_path(tmp_path: Path) -> Path:
    p = tmp_path / "AuthKey_TEST.p8"
    p.write_text(_SAMPLE_P8)
    return p


class TestAPNsClientConfig:
    def test_unconfigured_when_missing_env(self, monkeypatch) -> None:
        monkeypatch.delenv("APNS_KEY_PATH", raising=False)
        monkeypatch.delenv("APNS_KEY_ID", raising=False)
        monkeypatch.delenv("APNS_TEAM_ID", raising=False)
        c = APNsClient()
        assert not c.is_configured

    def test_unconfigured_when_key_file_missing(
        self, monkeypatch, tmp_path
    ) -> None:
        # Path set but file doesn't exist → explicitly not configured
        # so we skip sends instead of crashing at signing time.
        monkeypatch.setenv("APNS_KEY_PATH", str(tmp_path / "nope.p8"))
        monkeypatch.setenv("APNS_KEY_ID", "ABCD123456")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM456")
        c = APNsClient()
        assert not c.is_configured

    def test_configured_with_all_env(
        self, monkeypatch, p8_key_path
    ) -> None:
        monkeypatch.setenv("APNS_KEY_PATH", str(p8_key_path))
        monkeypatch.setenv("APNS_KEY_ID", "ABCD123456")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM456")
        c = APNsClient()
        assert c.is_configured

    def test_env_selects_host(self, monkeypatch, p8_key_path) -> None:
        monkeypatch.setenv("APNS_KEY_PATH", str(p8_key_path))
        monkeypatch.setenv("APNS_KEY_ID", "ABC")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM")
        monkeypatch.setenv("APNS_ENV", "sandbox")
        c = APNsClient()
        assert c._host == "api.sandbox.push.apple.com"
        monkeypatch.setenv("APNS_ENV", "production")
        c2 = APNsClient()
        assert c2._host == "api.push.apple.com"
        # Unknown values default to production (safer than sandbox —
        # at least the server won't fail auth; it just won't reach
        # dev-signed devices).
        monkeypatch.setenv("APNS_ENV", "whatever")
        c3 = APNsClient()
        assert c3._host == "api.push.apple.com"


class TestProviderToken:
    def test_jwt_has_required_claims(
        self, monkeypatch, p8_key_path
    ) -> None:
        monkeypatch.setenv("APNS_KEY_PATH", str(p8_key_path))
        monkeypatch.setenv("APNS_KEY_ID", "KEY123ABCD")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM567890")
        c = APNsClient()
        token = c._provider_token()
        # Decode without signature verify — we trust PyJWT signed it;
        # we just want to confirm the header + claims shape match
        # what Apple's APNs docs require.
        unverified = jwt.decode(token, options={"verify_signature": False})
        assert unverified["iss"] == "TEAM567890"
        # Issued-at is within the last minute.
        assert abs(unverified["iat"] - time.time()) < 60
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "ES256"
        assert header["kid"] == "KEY123ABCD"

    def test_jwt_caches_until_rotation(
        self, monkeypatch, p8_key_path
    ) -> None:
        monkeypatch.setenv("APNS_KEY_PATH", str(p8_key_path))
        monkeypatch.setenv("APNS_KEY_ID", "KEY")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM")
        c = APNsClient()
        t1 = c._provider_token()
        t2 = c._provider_token()
        # Same token re-used within the rotation window — Apple rejects
        # JWTs issued more than 1h ago, so we cache aggressively.
        assert t1 == t2

    def test_jwt_rotates_after_expiry(
        self, monkeypatch, p8_key_path
    ) -> None:
        monkeypatch.setenv("APNS_KEY_PATH", str(p8_key_path))
        monkeypatch.setenv("APNS_KEY_ID", "KEY")
        monkeypatch.setenv("APNS_TEAM_ID", "TEAM")
        c = APNsClient()
        t1 = c._provider_token()
        # Simulate time passing beyond the cache window.
        c._jwt_issued_at = time.time() - c._JWT_LIFETIME_SEC - 10
        t2 = c._provider_token()
        # Different iat claim → different token.
        assert t1 != t2


class TestDeviceTokenRegistry:
    def test_register_persists(self, tmp_path) -> None:
        path = tmp_path / "push.json"
        r1 = DeviceTokenRegistry(path)
        # Register a 64-hex token (matches real APNs token length).
        tok = "a" * 64
        assert r1.register(tok) is True
        assert r1.count() == 1

        # Fresh registry on the same path should see the persisted token.
        r2 = DeviceTokenRegistry(path)
        assert r2.count() == 1
        assert tok in r2.all()

    def test_register_idempotent(self, tmp_path) -> None:
        r = DeviceTokenRegistry(tmp_path / "p.json")
        tok = "b" * 64
        r.register(tok)
        r.register(tok)
        assert r.count() == 1

    def test_register_rejects_garbage(self, tmp_path) -> None:
        r = DeviceTokenRegistry(tmp_path / "p.json")
        assert r.register("") is False
        assert r.register("x" * 500) is False
        assert r.count() == 0

    def test_unregister(self, tmp_path) -> None:
        r = DeviceTokenRegistry(tmp_path / "p.json")
        tok = "c" * 64
        r.register(tok)
        r.unregister(tok)
        assert r.count() == 0
        # Unregistering a token we never had is a no-op.
        r.unregister("d" * 64)

    def test_file_permissions_tight(self, tmp_path) -> None:
        # Device tokens can be used to target specific users.
        # Persisted registry must be 0600 on disk.
        path = tmp_path / "p.json"
        r = DeviceTokenRegistry(path)
        r.register("e" * 64)
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {mode:o}"
