"""OAuth integration endpoints for GitHub and GitLab.

Flow:
  1. Authenticated user hits POST /api/v1/integrations/{provider}/authorize → gets a URL to redirect to.
  2. User is bounced to the provider, consents, and gets redirected back to /api/v1/integrations/{provider}/callback.
  3. The callback exchanges the code for tokens, fetches the provider username, and upserts an oauth_tokens row.
  4. User can list connected providers with GET /api/v1/integrations, or remove one with DELETE /api/v1/integrations/{provider}.
"""

from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db
from config import settings
from models.oauth_token import VALID_PROVIDERS, OAuthProvider, OAuthToken
from models.user import User
from schemas.oauth import AuthorizeResponse, DisconnectResponse, IntegrationResponse
from services import oauth_providers, oauth_state, token_crypto

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])


def _callback_url(provider: str) -> str:
    return f"{settings.OAUTH_PUBLIC_BASE_URL.rstrip('/')}/api/v1/integrations/{provider}/callback"


def _assert_provider(provider: str) -> None:
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth provider '{provider}'")


@router.get("", response_model=list[IntegrationResponse])
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[IntegrationResponse]:
    result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == current_user.id))
    rows = result.scalars().all()
    return [
        IntegrationResponse(
            provider=row.provider,
            provider_base_url=row.provider_base_url,
            provider_username=row.provider_username,
            scopes=row.scopes,
            access_token_expires_at=row.access_token_expires_at,
            connected_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/{provider}/authorize", response_model=AuthorizeResponse)
async def start_authorization(
    provider: str,
    current_user: User = Depends(get_current_user),
) -> AuthorizeResponse:
    _assert_provider(provider)
    try:
        cfg = oauth_providers.get_provider_config(provider)
    except oauth_providers.OAuthConfigError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    state = oauth_state.issue(user_id=str(current_user.id), provider=provider)
    url = oauth_providers.build_authorize_url(
        cfg,
        redirect_uri=_callback_url(provider),
        state=state,
    )
    return AuthorizeResponse(authorize_url=url, state=state)


@router.get("/{provider}/callback")
async def callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    _assert_provider(provider)
    try:
        payload = oauth_state.verify(state)
    except oauth_state.InvalidStateError as e:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {e}") from e

    if payload.provider != provider:
        raise HTTPException(status_code=400, detail="State/provider mismatch")

    try:
        cfg = oauth_providers.get_provider_config(provider)
    except oauth_providers.OAuthConfigError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            bundle = await oauth_providers.exchange_code(
                cfg,
                code=code,
                redirect_uri=_callback_url(provider),
                http=http,
            )
            provider_user = await oauth_providers.fetch_user(cfg, access_token=bundle.access_token, http=http)
    except oauth_providers.OAuthProviderError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    await _upsert_token(
        db,
        user_id=payload.user_id,
        cfg=cfg,
        bundle=bundle,
        provider_user=provider_user,
    )

    redirect_target = f"{settings.OAUTH_PUBLIC_BASE_URL.rstrip('/')}/settings/integrations?connected={provider}"
    return RedirectResponse(url=redirect_target, status_code=302)


@router.delete("/{provider}", response_model=DisconnectResponse)
async def disconnect(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DisconnectResponse:
    _assert_provider(provider)
    result = await db.execute(
        delete(OAuthToken).where(OAuthToken.user_id == current_user.id).where(OAuthToken.provider == provider)
    )
    await db.commit()
    return DisconnectResponse(provider=provider, disconnected=bool(result.rowcount))


async def _upsert_token(
    db: AsyncSession,
    *,
    user_id: str,
    cfg: oauth_providers.ProviderConfig,
    bundle: oauth_providers.TokenBundle,
    provider_user: oauth_providers.ProviderUser,
) -> OAuthToken:
    access_encrypted = token_crypto.encrypt(bundle.access_token)
    refresh_encrypted = token_crypto.encrypt(bundle.refresh_token) if bundle.refresh_token else None
    expires_at = datetime.now(UTC) + timedelta(seconds=bundle.expires_in) if bundle.expires_in is not None else None

    existing_row = await db.execute(
        select(OAuthToken)
        .where(OAuthToken.user_id == user_id)
        .where(OAuthToken.provider == cfg.name)
        .where(OAuthToken.provider_base_url == cfg.base_url)
    )
    existing = existing_row.scalar_one_or_none()

    if existing is not None:
        existing.provider_user_id = provider_user.id
        existing.provider_username = provider_user.username
        existing.access_token_encrypted = access_encrypted
        existing.refresh_token_encrypted = refresh_encrypted
        existing.access_token_expires_at = expires_at
        existing.scopes = bundle.scopes or cfg.default_scopes
        await db.commit()
        await db.refresh(existing)
        return existing

    row = OAuthToken(
        user_id=user_id,
        provider=cfg.name,
        provider_base_url=cfg.base_url,
        provider_user_id=provider_user.id,
        provider_username=provider_user.username,
        access_token_encrypted=access_encrypted,
        refresh_token_encrypted=refresh_encrypted,
        access_token_expires_at=expires_at,
        scopes=bundle.scopes or cfg.default_scopes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


__all__ = ["OAuthProvider", "router"]
