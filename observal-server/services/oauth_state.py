"""HMAC-signed state tokens for the OAuth authorization-code flow.

The OAuth `state` parameter must survive a redirect round-trip from Observal
to the provider and back, without anything else persisted server-side. We
encode the initiating user and a short expiry into a signed token, verify
the signature on callback, and reject anything stale.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from config import settings

STATE_TTL_SECONDS = 600  # 10 minutes


@dataclass(frozen=True)
class StatePayload:
    user_id: str
    provider: str
    nonce: str
    issued_at: int


class InvalidStateError(ValueError):
    pass


def _secret_key() -> bytes:
    key = settings.SECRET_KEY
    if not key or key == "change-me-in-production":
        # Still functional in dev, but the operator should rotate this.
        pass
    return hashlib.sha256(key.encode("utf-8")).digest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def issue(user_id: str, provider: str) -> str:
    payload = {
        "u": user_id,
        "p": provider,
        "n": secrets.token_urlsafe(12),
        "t": int(time.time()),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret_key(), body, hashlib.sha256).digest()
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"


def verify(token: str, *, max_age: int = STATE_TTL_SECONDS) -> StatePayload:
    try:
        body_b64, sig_b64 = token.split(".", 1)
    except ValueError as e:
        raise InvalidStateError("malformed state token") from e

    try:
        body = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, TypeError) as e:
        raise InvalidStateError("state token has invalid base64") from e

    expected = hmac.new(_secret_key(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise InvalidStateError("state token signature mismatch")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidStateError("state token body is not valid JSON") from e

    required = {"u", "p", "n", "t"}
    if not required.issubset(payload):
        raise InvalidStateError("state token is missing required fields")

    issued_at = int(payload["t"])
    if time.time() - issued_at > max_age:
        raise InvalidStateError("state token has expired")

    return StatePayload(
        user_id=str(payload["u"]),
        provider=str(payload["p"]),
        nonce=str(payload["n"]),
        issued_at=issued_at,
    )
