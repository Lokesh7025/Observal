"""GitHub and GitLab OAuth2 authorization-code flows.

Observal is registered as an OAuth client in each provider. The flows here
only cover the user-to-server variant: a real human sits in front of a
browser and authorizes Observal on their own behalf. Service-to-service
flows (GitHub App installation tokens, GitLab machine credentials) are not
implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from config import settings
from models.oauth_token import OAuthProvider


class OAuthConfigError(RuntimeError):
    """Raised when a provider is requested but not configured in settings."""


class OAuthProviderError(RuntimeError):
    """Raised when the provider rejects an exchange or returns malformed data."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    authorize_url: str
    token_url: str
    user_info_url: str
    revoke_url: str | None
    default_scopes: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scopes: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProviderUser:
    id: str
    username: str


def github_config() -> ProviderConfig:
    if not settings.GITHUB_OAUTH_CLIENT_ID or not settings.GITHUB_OAUTH_CLIENT_SECRET:
        raise OAuthConfigError(
            "GitHub OAuth is not configured. Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET."
        )
    return ProviderConfig(
        name=OAuthProvider.github,
        base_url="https://github.com",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        user_info_url="https://api.github.com/user",
        revoke_url=None,  # GitHub revocation requires basic auth on a different shape — handled in disconnect
        default_scopes="read:user repo",
        client_id=settings.GITHUB_OAUTH_CLIENT_ID,
        client_secret=settings.GITHUB_OAUTH_CLIENT_SECRET,
    )


def gitlab_config(base_url: str | None = None) -> ProviderConfig:
    if not settings.GITLAB_OAUTH_CLIENT_ID or not settings.GITLAB_OAUTH_CLIENT_SECRET:
        raise OAuthConfigError(
            "GitLab OAuth is not configured. Set GITLAB_OAUTH_CLIENT_ID and GITLAB_OAUTH_CLIENT_SECRET."
        )
    base = (base_url or settings.GITLAB_BASE_URL).rstrip("/")
    return ProviderConfig(
        name=OAuthProvider.gitlab,
        base_url=base,
        authorize_url=f"{base}/oauth/authorize",
        token_url=f"{base}/oauth/token",
        user_info_url=f"{base}/api/v4/user",
        revoke_url=f"{base}/oauth/revoke",
        default_scopes="read_user read_repository",
        client_id=settings.GITLAB_OAUTH_CLIENT_ID,
        client_secret=settings.GITLAB_OAUTH_CLIENT_SECRET,
    )


def get_provider_config(provider: str, base_url: str | None = None) -> ProviderConfig:
    if provider == OAuthProvider.github:
        return github_config()
    if provider == OAuthProvider.gitlab:
        return gitlab_config(base_url)
    raise OAuthConfigError(f"Unsupported OAuth provider: {provider}")


def build_authorize_url(cfg: ProviderConfig, *, redirect_uri: str, state: str, scopes: str | None = None) -> str:
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes or cfg.default_scopes,
        "state": state,
    }
    return f"{cfg.authorize_url}?{urlencode(params)}"


async def exchange_code(
    cfg: ProviderConfig,
    *,
    code: str,
    redirect_uri: str,
    http: httpx.AsyncClient | None = None,
) -> TokenBundle:
    body = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    return await _post_token(cfg, body, http=http)


async def refresh_access_token(
    cfg: ProviderConfig,
    *,
    refresh_token: str,
    http: httpx.AsyncClient | None = None,
) -> TokenBundle:
    body = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    return await _post_token(cfg, body, http=http)


async def _post_token(
    cfg: ProviderConfig,
    body: dict[str, str],
    *,
    http: httpx.AsyncClient | None,
) -> TokenBundle:
    headers = {"Accept": "application/json"}
    client = http or httpx.AsyncClient(timeout=15.0)
    owns_client = http is None
    try:
        resp = await client.post(cfg.token_url, data=body, headers=headers)
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code >= 400:
        raise OAuthProviderError(f"{cfg.name} token endpoint returned {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise OAuthProviderError(f"{cfg.name} token endpoint returned non-JSON body") from e

    if "error" in data:
        raise OAuthProviderError(f"{cfg.name} token endpoint error: {data.get('error_description') or data['error']}")

    access = data.get("access_token")
    if not access:
        raise OAuthProviderError(f"{cfg.name} token response missing access_token")

    return TokenBundle(
        access_token=str(access),
        refresh_token=(str(data["refresh_token"]) if data.get("refresh_token") else None),
        expires_in=(int(data["expires_in"]) if data.get("expires_in") is not None else None),
        scopes=str(data.get("scope", "")),
        raw=data,
    )


async def fetch_user(
    cfg: ProviderConfig,
    *,
    access_token: str,
    http: httpx.AsyncClient | None = None,
) -> ProviderUser:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    client = http or httpx.AsyncClient(timeout=15.0)
    owns_client = http is None
    try:
        resp = await client.get(cfg.user_info_url, headers=headers)
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code >= 400:
        raise OAuthProviderError(f"{cfg.name} user-info endpoint returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    if cfg.name == OAuthProvider.github:
        provider_id = data.get("id")
        username = data.get("login") or data.get("name") or ""
    else:
        provider_id = data.get("id")
        username = data.get("username") or data.get("name") or ""

    if provider_id is None or not username:
        raise OAuthProviderError(f"{cfg.name} user-info response missing id or username")

    return ProviderUser(id=str(provider_id), username=str(username))
