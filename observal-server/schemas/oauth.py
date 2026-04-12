"""API schemas for OAuth integration endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IntegrationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    provider_base_url: str
    provider_username: str
    scopes: str
    access_token_expires_at: datetime | None
    connected_at: datetime


class AuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


class DisconnectResponse(BaseModel):
    provider: str
    disconnected: bool
