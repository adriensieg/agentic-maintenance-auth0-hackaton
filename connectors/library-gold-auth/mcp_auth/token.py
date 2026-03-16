import logging

import httpx
from cachetools import TTLCache
from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError

from .config import AUTH0_DOMAIN, AUTH0_AUDIENCE, ISSUER, JWKS_URL

logger = logging.getLogger(__name__)

_jwks_cache: TTLCache = TTLCache(maxsize=1, ttl=600)


async def get_jwks() -> dict:
    if "jwks" in _jwks_cache:
        return _jwks_cache["jwks"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
    jwks = resp.json()
    _jwks_cache["jwks"] = jwks
    logger.info("JWKS refreshed from Auth0")
    return jwks


async def verify_token(token: str) -> dict:
    """
    Try JWT verification first (fast, no network call).
    Fall back to Auth0 /userinfo for opaque tokens.
    """
    # ── 1. Try JWT verification ──────────────────────────────────────────────
    try:
        jwks = await get_jwks()
        payload = jwt.decode(
            token, jwks, algorithms=["RS256"],
            audience=AUTH0_AUDIENCE, issuer=ISSUER,
            options={"verify_at_hash": False},
        )
        logger.info("Token verified as JWT ✅")
        return payload
    except ExpiredSignatureError:
        raise ValueError("Token has expired")
    except JWTError as e:
        logger.info(f"Not a valid JWT ({e}), trying /userinfo...")
    except Exception as e:
        logger.info(f"JWT verification failed ({e}), trying /userinfo...")

    # ── 2. Fall back to Auth0 /userinfo for opaque tokens ────────────────────
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{AUTH0_DOMAIN}/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    logger.info(f"/userinfo response: HTTP {resp.status_code}")

    if resp.status_code == 401:
        raise ValueError("Token rejected by Auth0 /userinfo — invalid or expired")

    if resp.status_code != 200:
        raise ValueError(f"/userinfo request failed: HTTP {resp.status_code}")

    data = resp.json()
    logger.info(f"Token verified via /userinfo ✅ sub={data.get('sub')}")
    return data
