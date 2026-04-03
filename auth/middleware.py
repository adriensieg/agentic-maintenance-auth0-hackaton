
"""
auth/middleware.py
──────────────────
FastAPI dependency + ASGI middleware for bearer-token authentication.

Every protected endpoint calls `require_auth()` which:
  1. Extracts the Bearer token from the Authorization header.
  2. Tries RS256 JWT verification against cached JWKS.
  3. Falls back to Auth0 /userinfo for opaque tokens.
  4. Attaches the verified claims to request.state.

Token lifecycle:
  • JWKS is refreshed at most every 10 minutes.
  • Expired tokens raise 401 immediately.
  • Claims are NOT cached between requests — every request re-verifies.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

import httpx
from cachetools import TTLCache
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt

from config.settings import get_settings

logger = logging.getLogger("washfix.auth.middleware")

_jwks_cache: TTLCache = TTLCache(maxsize=1, ttl=600)

bearer_scheme = HTTPBearer(auto_error=False)


# ── JWKS helpers ─────────────────────────────────────────────────────────

async def _fetch_jwks() -> dict:
    if "jwks" in _jwks_cache:
        return _jwks_cache["jwks"]
    s = get_settings()
    logger.info(f"Refreshing JWKS from {s.auth0_jwks_url}")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(s.auth0_jwks_url)
        resp.raise_for_status()
    jwks = resp.json()
    _jwks_cache["jwks"] = jwks
    logger.info(f"JWKS refreshed — {len(jwks.get('keys', []))} key(s).")
    return jwks


# ── Core verification ─────────────────────────────────────────────────────

async def verify_bearer_token(token: str) -> dict[str, Any]:
    """
    Verify a bearer token.  Returns the decoded claims dict.
    Raises ValueError on any failure.
    """
    s = get_settings()

    # ── Try RS256 JWT first ──────────────────────────────────────────────
    try:
        jwks = await _fetch_jwks()
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=s.auth0_audience,
            issuer=s.auth0_issuer,
            options={"verify_at_hash": False},
        )
        logger.debug(f"JWT verified — sub={claims.get('sub')}")
        return claims
    except ExpiredSignatureError:
        logger.warning("JWT expired.")
        raise ValueError("Token has expired.")
    except JWTError as exc:
        logger.debug(f"Not a valid JWT ({exc}); falling back to /userinfo.")

    # ── Opaque token fallback ────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://{s.auth0_domain}/userinfo",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 401:
        raise ValueError("Token rejected by Auth0 /userinfo.")
    if resp.status_code != 200:
        raise ValueError(f"/userinfo returned HTTP {resp.status_code}.")

    claims = resp.json()
    logger.debug(f"Opaque token verified via /userinfo — sub={claims.get('sub')}")
    return claims


# ── FastAPI dependency ────────────────────────────────────────────────────

async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict[str, Any]:
    """
    FastAPI dependency.  Returns the verified JWT claims.
    Usage::

        @router.get("/protected")
        async def my_endpoint(claims: dict = Depends(require_auth)):
            user_id = claims["sub"]
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await verify_bearer_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    request.state.claims = claims
    return claims


def get_subject(claims: dict[str, Any]) -> str:
    """Extract the Auth0 user `sub` from verified claims."""
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="No subject in token.")
    return sub
