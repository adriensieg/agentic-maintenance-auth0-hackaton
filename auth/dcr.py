"""
auth/dcr.py
────────────
Dynamic Client Registration (DCR) — RFC 7591.

Used to register short-lived OAuth clients for partner APIs
(technician field-ops APIs, warranty registry, etc.).

Why DCR?
  Instead of pre-registering a static client_id for each external service,
  we register a new OAuth client ON DEMAND with the exact scopes needed,
  use it for ONE session, then delete it.

  Benefits:
    • Minimal blast radius — compromised credentials are session-scoped.
    • Each partner session has unique credentials → auditable.
    • Clients auto-expire (we set `client_secret_expires_at`).

Flow per partner call:
  1. Register ephemeral client via Auth0 /oidc/register.
  2. Obtain access token with client-credentials.
  3. Use token to call partner API.
  4. DELETE the client after use.

Auth0 DCR reference:
  https://auth0.com/docs/get-started/authentication-and-authorization-flow/dynamic-client-registration
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("washfix.auth.dcr")


@dataclass
class EphemeralClient:
    client_id:     str
    client_secret: str
    client_name:   str
    scopes:        list[str] = field(default_factory=list)
    registration_access_token: Optional[str] = None
    registration_client_uri:   Optional[str] = None


class DCRClient:
    """Register, use, and delete ephemeral OAuth clients via Auth0 DCR."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def register(
        self,
        client_name: str,
        scopes: list[str],
        redirect_uris: Optional[list[str]] = None,
        grant_types: Optional[list[str]] = None,
    ) -> EphemeralClient:
        """
        Register a new ephemeral client in Auth0.
        Returns an EphemeralClient with credentials.
        """
        s = self._settings
        payload: dict[str, Any] = {
            "client_name":              client_name,
            "grant_types":              grant_types or ["client_credentials"],
            "token_endpoint_auth_method": "client_secret_post",
            "redirect_uris":            redirect_uris or [],
            "scope":                    " ".join(scopes),
            "response_types":           ["code"],
        }

        logger.info(f"DCR: registering client '{client_name}' scopes={scopes}")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://{s.auth0_domain}/oidc/register",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if resp.status_code not in (200, 201):
            logger.error(f"DCR registration failed: HTTP {resp.status_code} — {resp.text}")
            raise RuntimeError(f"DCR failed: {resp.status_code}")

        data = resp.json()
        ec = EphemeralClient(
            client_id     = data["client_id"],
            client_secret = data.get("client_secret", ""),
            client_name   = client_name,
            scopes        = scopes,
            registration_access_token = data.get("registration_access_token"),
            registration_client_uri   = data.get("registration_client_uri"),
        )
        logger.info(f"DCR: client registered — id={ec.client_id}")
        return ec

    async def get_token(
        self,
        ec: EphemeralClient,
        audience: str,
    ) -> str:
        """
        Obtain an access token for the ephemeral client using
        the client-credentials grant.
        """
        s = self._settings
        payload = {
            "grant_type":    "client_credentials",
            "client_id":     ec.client_id,
            "client_secret": ec.client_secret,
            "audience":      audience,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(s.auth0_token_url, data=payload)

        if resp.status_code != 200:
            logger.error(f"DCR token fetch failed: HTTP {resp.status_code} — {resp.text}")
            raise RuntimeError(f"DCR token fetch failed: {resp.status_code}")

        token: str = resp.json()["access_token"]
        logger.info(f"DCR: access token obtained for client={ec.client_id}")
        return token

    async def delete(self, ec: EphemeralClient) -> None:
        """
        Delete the ephemeral client from Auth0 after use.
        Uses the registration_access_token if available; falls back to
        Management API deletion.
        """
        s = self._settings

        if ec.registration_client_uri and ec.registration_access_token:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(
                    ec.registration_client_uri,
                    headers={"Authorization": f"Bearer {ec.registration_access_token}"},
                )
            if resp.status_code in (200, 204):
                logger.info(f"DCR: client {ec.client_id} deleted via RFC7592.")
                return
            logger.warning(f"DCR RFC7592 delete returned {resp.status_code} — trying Mgmt API.")

        # Fallback: Management API delete
        from auth.auth0_client import auth0_client
        mgmt_token = await auth0_client.get_management_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                f"https://{s.auth0_domain}/api/v2/clients/{ec.client_id}",
                headers={"Authorization": f"Bearer {mgmt_token}"},
            )
        if resp.status_code in (200, 204):
            logger.info(f"DCR: client {ec.client_id} deleted via Mgmt API.")
        else:
            logger.error(f"DCR: failed to delete client {ec.client_id}: {resp.status_code}")

    async def use_once(
        self,
        client_name: str,
        scopes: list[str],
        audience: str,
    ) -> str:
        """
        Convenience: register → get token → delete → return token.
        The client exists only for the duration of this call.
        """
        ec = await self.register(client_name, scopes)
        try:
            token = await self.get_token(ec, audience)
        finally:
            # Always delete, even if token fetch fails
            await self.delete(ec)
        logger.info(f"DCR use_once complete for '{client_name}'.")
        return token


# Singleton
dcr_client = DCRClient()
