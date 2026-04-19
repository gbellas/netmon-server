"""Auth token bootstrap + header extraction tests."""

from __future__ import annotations

import os

import pytest

import auth


class TestInitToken:
    def test_uses_env_var_if_set(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("NETMON_API_TOKEN", "explicit-env-token-42")
        # Point the env-file helper at a temp file so we don't clobber
        # the real .env during tests.
        monkeypatch.setattr(auth, "_ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(auth, "_TOKEN", None)
        tok = auth.init_token()
        assert tok == "explicit-env-token-42"
        assert auth.current_token() == "explicit-env-token-42"

    def test_generates_token_if_absent(self, monkeypatch, tmp_path) -> None:
        # Neither env var nor existing .env — expect a freshly generated
        # token and a persisted .env line.
        monkeypatch.delenv("NETMON_API_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        monkeypatch.setattr(auth, "_ENV_PATH", env_file)
        monkeypatch.setattr(auth, "_TOKEN", None)
        tok = auth.init_token()
        # URL-safe base64 of 32 random bytes → ~43 chars.
        assert 30 <= len(tok) <= 80
        # Persisted for next startup.
        assert env_file.exists()
        assert f"NETMON_API_TOKEN={tok}" in env_file.read_text()

    def test_reads_existing_env_file(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("NETMON_API_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=x\nNETMON_API_TOKEN=persisted-value\nMORE=y\n")
        monkeypatch.setattr(auth, "_ENV_PATH", env_file)
        monkeypatch.setattr(auth, "_TOKEN", None)
        tok = auth.init_token()
        assert tok == "persisted-value"


class TestHeaderParsing:
    def test_bearer_extraction(self) -> None:
        assert auth._extract_bearer("Bearer abc123") == "abc123"
        assert auth._extract_bearer("bearer abc123") == "abc123"
        # Case-insensitive scheme match.
        assert auth._extract_bearer("BEARER   xyz") == "xyz"

    def test_non_bearer_returns_none(self) -> None:
        assert auth._extract_bearer("Basic xxx") is None
        assert auth._extract_bearer("Token abc") is None
        assert auth._extract_bearer(None) is None
        assert auth._extract_bearer("") is None

    def test_missing_value_returns_none(self) -> None:
        assert auth._extract_bearer("Bearer") is None


class TestRequireAuth:
    @pytest.mark.asyncio
    async def test_rejects_missing_token(self, monkeypatch) -> None:
        from fastapi import HTTPException
        monkeypatch.setattr(auth, "_TOKEN", "the-right-token")
        with pytest.raises(HTTPException) as exc:
            await auth.require_auth(authorization=None, token=None)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_token(self, monkeypatch) -> None:
        from fastapi import HTTPException
        monkeypatch.setattr(auth, "_TOKEN", "the-right-token")
        with pytest.raises(HTTPException) as exc:
            await auth.require_auth(
                authorization="Bearer not-the-token", token=None)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_accepts_header(self, monkeypatch) -> None:
        monkeypatch.setattr(auth, "_TOKEN", "ok")
        # Should not raise.
        await auth.require_auth(authorization="Bearer ok", token=None)

    @pytest.mark.asyncio
    async def test_accepts_query_param(self, monkeypatch) -> None:
        monkeypatch.setattr(auth, "_TOKEN", "ok")
        # Some clients (web SSE, WebSocket, <img> tags) can only send
        # auth via query string, not headers.
        await auth.require_auth(authorization=None, token="ok")
