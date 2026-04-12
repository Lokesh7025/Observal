"""Unit tests for the GitHub/GitLab OAuth integration feature.

Covers:
  * token_crypto  — encrypt/decrypt round-trip and misconfiguration errors.
  * oauth_state   — HMAC state issue/verify, tamper detection, expiry.
  * oauth_providers — URL building plus mocked token exchange and user fetch.
  * api.routes.integrations — list/authorize/callback/disconnect with DB mocked.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.deps import get_current_user, get_db
from config import settings
from models.user import User, UserRole
from services import oauth_providers, oauth_state, token_crypto

# ── Helpers ──────────────────────────────────────────────


def _user(**kw):
    u = MagicMock(spec=User)
    u.id = kw.get("id", uuid.uuid4())
    u.role = kw.get("role", UserRole.developer)
    return u


def _mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


def _app_with(router, user=None, db=None):
    user = user or _user()
    db = db or _mock_db()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return app, db, user


@pytest.fixture(autouse=True)
def _fernet_key():
    """Every test gets a fresh, valid Fernet key and a wired-up cipher cache."""
    original = settings.OAUTH_TOKEN_ENCRYPTION_KEY
    settings.OAUTH_TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    token_crypto.reset_cipher_cache()
    yield
    settings.OAUTH_TOKEN_ENCRYPTION_KEY = original
    token_crypto.reset_cipher_cache()


# ═══════════════════════════════════════════════════════════
# token_crypto
# ═══════════════════════════════════════════════════════════


class TestTokenCrypto:
    def test_round_trip(self):
        ct = token_crypto.encrypt("hello-world")
        assert ct != b"hello-world"
        assert token_crypto.decrypt(ct) == "hello-world"

    def test_encrypt_empty_raises(self):
        with pytest.raises(token_crypto.TokenCryptoError):
            token_crypto.encrypt("")

    def test_missing_key_raises(self):
        settings.OAUTH_TOKEN_ENCRYPTION_KEY = ""
        token_crypto.reset_cipher_cache()
        with pytest.raises(token_crypto.TokenCryptoError):
            token_crypto.encrypt("x")

    def test_decrypt_wrong_key_raises(self):
        ct = token_crypto.encrypt("secret")
        settings.OAUTH_TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
        token_crypto.reset_cipher_cache()
        with pytest.raises(token_crypto.TokenCryptoError):
            token_crypto.decrypt(ct)


# ═══════════════════════════════════════════════════════════
# oauth_state
# ═══════════════════════════════════════════════════════════


class TestOAuthState:
    def test_issue_and_verify_round_trip(self):
        tok = oauth_state.issue(user_id="user-123", provider="github")
        payload = oauth_state.verify(tok)
        assert payload.user_id == "user-123"
        assert payload.provider == "github"
        assert payload.nonce

    def test_tampered_signature_rejected(self):
        tok = oauth_state.issue(user_id="u", provider="github")
        body_b64, _sig_b64 = tok.split(".", 1)
        bad = f"{body_b64}.{base64.urlsafe_b64encode(b'nope').rstrip(b'=').decode()}"
        with pytest.raises(oauth_state.InvalidStateError):
            oauth_state.verify(bad)

    def test_expired_token_rejected(self):
        tok = oauth_state.issue(user_id="u", provider="github")
        with (
            patch.object(oauth_state.time, "time", return_value=time.time() + 10_000),
            pytest.raises(oauth_state.InvalidStateError),
        ):
            oauth_state.verify(tok)

    def test_malformed_token_rejected(self):
        with pytest.raises(oauth_state.InvalidStateError):
            oauth_state.verify("not-a-token")

    def test_missing_fields_rejected(self):
        import hashlib
        import hmac

        body = json.dumps({"u": "x"}).encode()
        key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        sig = hmac.new(key, body, hashlib.sha256).digest()
        tok = (
            base64.urlsafe_b64encode(body).rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        )
        with pytest.raises(oauth_state.InvalidStateError):
            oauth_state.verify(tok)


# ═══════════════════════════════════════════════════════════
# oauth_providers
# ═══════════════════════════════════════════════════════════


class TestOAuthProviders:
    def _github_cfg(self):
        return oauth_providers.ProviderConfig(
            name="github",
            base_url="https://github.com",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            user_info_url="https://api.github.com/user",
            revoke_url=None,
            default_scopes="read:user repo",
            client_id="cid",
            client_secret="csec",
        )

    def test_build_authorize_url(self):
        url = oauth_providers.build_authorize_url(
            self._github_cfg(),
            redirect_uri="https://obs.example/cb",
            state="STATE",
        )
        assert url.startswith("https://github.com/login/oauth/authorize?")
        assert "client_id=cid" in url
        assert "state=STATE" in url
        assert "scope=read%3Auser+repo" in url

    def test_get_provider_config_unknown_raises(self):
        with pytest.raises(oauth_providers.OAuthConfigError):
            oauth_providers.get_provider_config("bitbucket")

    def test_github_config_missing_credentials(self):
        original_id = settings.GITHUB_OAUTH_CLIENT_ID
        settings.GITHUB_OAUTH_CLIENT_ID = ""
        try:
            with pytest.raises(oauth_providers.OAuthConfigError):
                oauth_providers.github_config()
        finally:
            settings.GITHUB_OAUTH_CLIENT_ID = original_id

    @pytest.mark.asyncio
    async def test_exchange_code_success(self):
        cfg = self._github_cfg()
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "scope": "read:user repo",
        }
        http.post = AsyncMock(return_value=resp)

        bundle = await oauth_providers.exchange_code(cfg, code="CODE", redirect_uri="https://obs/cb", http=http)
        assert bundle.access_token == "at"
        assert bundle.refresh_token == "rt"
        assert bundle.expires_in == 3600
        assert bundle.scopes == "read:user repo"
        http.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exchange_code_http_error(self):
        cfg = self._github_cfg()
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "bad_code"
        http.post = AsyncMock(return_value=resp)

        with pytest.raises(oauth_providers.OAuthProviderError):
            await oauth_providers.exchange_code(cfg, code="x", redirect_uri="y", http=http)

    @pytest.mark.asyncio
    async def test_exchange_code_error_body(self):
        cfg = self._github_cfg()
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"error": "bad_verification_code"}
        http.post = AsyncMock(return_value=resp)

        with pytest.raises(oauth_providers.OAuthProviderError):
            await oauth_providers.exchange_code(cfg, code="x", redirect_uri="y", http=http)

    @pytest.mark.asyncio
    async def test_fetch_user_github(self):
        cfg = self._github_cfg()
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": 42, "login": "octocat"}
        http.get = AsyncMock(return_value=resp)

        user = await oauth_providers.fetch_user(cfg, access_token="at", http=http)
        assert user.id == "42"
        assert user.username == "octocat"

    @pytest.mark.asyncio
    async def test_fetch_user_missing_fields(self):
        cfg = self._github_cfg()
        http = AsyncMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": None, "login": None}
        http.get = AsyncMock(return_value=resp)

        with pytest.raises(oauth_providers.OAuthProviderError):
            await oauth_providers.fetch_user(cfg, access_token="at", http=http)


# ═══════════════════════════════════════════════════════════
# api.routes.integrations
# ═══════════════════════════════════════════════════════════


def _scalar_result(val):
    r = MagicMock()
    r.scalar_one_or_none.return_value = val
    r.scalars.return_value.all.return_value = [val] if val else []
    return r


class TestIntegrationsRoutes:
    @pytest.mark.asyncio
    async def test_list_empty(self):
        from api.routes.integrations import router

        app, db, _ = _app_with(router)
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=empty)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/api/v1/integrations")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_authorize_unknown_provider_404(self):
        from api.routes.integrations import router

        app, _db, _ = _app_with(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/api/v1/integrations/bitbucket/authorize")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_authorize_returns_url(self):
        from api.routes.integrations import router

        app, _db, _ = _app_with(router)
        cfg = oauth_providers.ProviderConfig(
            name="github",
            base_url="https://github.com",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            user_info_url="https://api.github.com/user",
            revoke_url=None,
            default_scopes="read:user repo",
            client_id="cid",
            client_secret="csec",
        )
        with patch.object(oauth_providers, "get_provider_config", return_value=cfg):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/api/v1/integrations/github/authorize")
        assert r.status_code == 200
        body = r.json()
        assert body["authorize_url"].startswith("https://github.com/login/oauth/authorize?")
        assert body["state"]

    @pytest.mark.asyncio
    async def test_authorize_unconfigured_returns_503(self):
        from api.routes.integrations import router

        app, _db, _ = _app_with(router)
        with patch.object(
            oauth_providers,
            "get_provider_config",
            side_effect=oauth_providers.OAuthConfigError("nope"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/api/v1/integrations/github/authorize")
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_callback_bad_state_returns_400(self):
        from api.routes.integrations import router

        app, _db, _ = _app_with(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/api/v1/integrations/github/callback?code=x&state=bogus")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_happy_path_redirects(self):
        from api.routes import integrations as route_mod
        from api.routes.integrations import router

        user_id = uuid.uuid4()
        app, db, _ = _app_with(router, user=_user(id=user_id))
        db.execute = AsyncMock(return_value=_scalar_result(None))

        cfg = oauth_providers.ProviderConfig(
            name="github",
            base_url="https://github.com",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            user_info_url="https://api.github.com/user",
            revoke_url=None,
            default_scopes="read:user repo",
            client_id="cid",
            client_secret="csec",
        )
        bundle = oauth_providers.TokenBundle(
            access_token="at",
            refresh_token="rt",
            expires_in=3600,
            scopes="read:user repo",
            raw={},
        )
        provider_user = oauth_providers.ProviderUser(id="42", username="octocat")

        state_token = oauth_state.issue(user_id=str(user_id), provider="github")

        with (
            patch.object(oauth_providers, "get_provider_config", return_value=cfg),
            patch.object(oauth_providers, "exchange_code", AsyncMock(return_value=bundle)),
            patch.object(oauth_providers, "fetch_user", AsyncMock(return_value=provider_user)),
            patch.object(route_mod, "_upsert_token", AsyncMock(return_value=MagicMock())),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.get(
                    f"/api/v1/integrations/github/callback?code=CODE&state={state_token}",
                    follow_redirects=False,
                )
        assert r.status_code == 302
        assert "connected=github" in r.headers["location"]

    @pytest.mark.asyncio
    async def test_callback_state_provider_mismatch(self):
        from api.routes.integrations import router

        app, _db, _ = _app_with(router)
        state_token = oauth_state.issue(user_id="u", provider="gitlab")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get(
                f"/api/v1/integrations/github/callback?code=CODE&state={state_token}",
                follow_redirects=False,
            )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_disconnect_deletes(self):
        from api.routes.integrations import router

        app, db, _ = _app_with(router)
        result = MagicMock()
        result.rowcount = 1
        db.execute = AsyncMock(return_value=result)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.delete("/api/v1/integrations/github")
        assert r.status_code == 200
        body = r.json()
        assert body["provider"] == "github"
        assert body["disconnected"] is True

    @pytest.mark.asyncio
    async def test_disconnect_unknown_provider_404(self):
        from api.routes.integrations import router

        app, _db, _ = _app_with(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.delete("/api/v1/integrations/bitbucket")
        assert r.status_code == 404
