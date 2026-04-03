"""
auth/auth0_client.py
────────────────────
Low-level Auth0 Management API client.

Responsibilities:
  • Fetch & cache Management API tokens (M2M client-credentials flow).
  • Read / write user identities stored in Auth0 (used by Token Vault).
  • Exchange authorization codes for access + refresh tokens.
  • Revoke tokens explicitly (lifecycle management).

All tokens obtained here are SHORT-LIVED and single-use where applicable.
Expired / used tokens are revoked immediately after extraction.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx
from cachetools import TTLCache

from config.settings import get_settings

logger = logging.getLogger("washfix.auth.auth0_client")

# ── Management token cache (TTL slightly shorter than 24h expiry) ─────────
_mgmt_token_cache: TTLCache = TTLCache(maxsize=1, ttl=82_000)


class Auth0Client:
    """Thin async wrapper around Auth0 Management API v2."""

    def __init__(self) -> None:
        self._settings = get_settings()

    # ── Management token ─────────────────────────────────────────────────

    async def get_management_token(self) -> str:
        """
        Return a cached Auth0 Management API access token.
        Uses client-credentials grant with the dedicated M2M application.
        """
        if "token" in _mgmt_token_cache:
            logger.debug("Management token served from cache.")
            return _mgmt_token_cache["token"]

        s = self._settings
        logger.info("Fetching new Auth0 management token.")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(s.auth0_token_url, json={
                "grant_type":    "client_credentials",
                "client_id":     s.auth0_mgmt_client_id,
                "client_secret": s.auth0_mgmt_client_secret,
                "audience":      f"https://{s.auth0_domain}/api/v2/",
            })
            resp.raise_for_status()

        token = resp.json()["access_token"]
        _mgmt_token_cache["token"] = token
        logger.info("Management token cached.")
        return token

    # ── User identity helpers ─────────────────────────────────────────────

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """Fetch full user profile from Auth0 Management API."""
        mgmt_token = await self.get_management_token()
        s = self._settings
        from urllib.parse import quote
        encoded = quote(user_id, safe="")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://{s.auth0_domain}/api/v2/users/{encoded}",
                headers={"Authorization": f"Bearer {mgmt_token}"},
            )
            resp.raise_for_status()
        logger.info(f"Fetched user {user_id} from Auth0.")
        return resp.json()

    async def update_user_metadata(
        self,
        user_id: str,
        app_metadata: Optional[dict] = None,
        user_metadata: Optional[dict] = None,
    ) -> dict[str, Any]:
        """PATCH user metadata — used to store per-user tokens in app_metadata."""
        mgmt_token = await self.get_management_token()
        s = self._settings
        from urllib.parse import quote
        encoded = quote(user_id, safe="")
        payload: dict[str, Any] = {}
        if app_metadata is not None:
            payload["app_metadata"] = app_metadata
        if user_metadata is not None:
            payload["user_metadata"] = user_metadata

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"https://{s.auth0_domain}/api/v2/users/{encoded}",
                headers={
                    "Authorization": f"Bearer {mgmt_token}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
        logger.info(f"Updated metadata for user {user_id}.")
        return resp.json()

    async def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        """Search for a user by email address."""
        mgmt_token = await self.get_management_token()
        s = self._settings
        from urllib.parse import quote
        q = quote(f'email:"{email}"', safe="")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://{s.auth0_domain}/api/v2/users?q={q}&search_engine=v3",
                headers={"Authorization": f"Bearer {mgmt_token}"},
            )
            resp.raise_for_status()
        users = resp.json()
        return users[0] if users else None

    # ── Token exchange ────────────────────────────────────────────────────

    async def exchange_code_for_tokens(
        self,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Exchange an authorization code for access + refresh tokens.
        Supports PKCE (code_verifier).
        """
        s = self._settings
        payload: dict[str, Any] = {
            "grant_type":    "authorization_code",
            "client_id":     client_id,
            "client_secret": client_secret,
            "code":          code,
            "redirect_uri":  redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(s.auth0_token_url, json=payload)
            resp.raise_for_status()

        tokens = resp.json()
        logger.info("Authorization code exchanged for tokens successfully.")
        return tokens

    async def refresh_access_token(
        self,
        refresh_token: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Use a refresh token to obtain a new access token.
        After extraction the caller should store the NEW refresh token
        and discard the old one (rotation).
        """
        s = self._settings
        payload = {
            "grant_type":    "refresh_token",
            "client_id":     client_id or s.auth0_client_id,
            "client_secret": client_secret or s.auth0_client_secret,
            "refresh_token": refresh_token,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(s.auth0_token_url, json=payload)
            resp.raise_for_status()

        result = resp.json()
        logger.info("Refresh token exchanged — new access token issued.")
        return result

    async def revoke_token(self, token: str, token_hint: str = "refresh_token") -> None:
        """
        Revoke an access or refresh token immediately.
        This is called after single-use tokens are consumed.
        """
        s = self._settings
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://{s.auth0_domain}/oauth/revoke",
                json={
                    "client_id":     s.auth0_client_id,
                    "client_secret": s.auth0_client_secret,
                    "token":         token,
                    "token_type_hint": token_hint,
                },
            )
        logger.info(f"Token revoked (hint={token_hint}).")

    # ── Introspect / userinfo ─────────────────────────────────────────────

    async def get_userinfo(self, access_token: str) -> dict[str, Any]:
        """Call /userinfo with a bearer token — for opaque token validation."""
        s = self._settings
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://{s.auth0_domain}/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
        return resp.json()


# Singleton
auth0_client = Auth0Client()
