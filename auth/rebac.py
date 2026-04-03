"""
auth/rebac.py
─────────────
Relationship-Based Access Control (ReBAC) using Auth0 Fine-Grained
Authorization (FGA).

Why ReBAC here?
  The washing-machine data model has OBJECTS (appliances, units, buildings)
  and SUBJECTS (tenants, building managers, service accounts).
  We need to ensure that when a user asks about "my washing machine" the
  agent only loads data for appliances that the user has a `viewer` or
  `owner` relationship with — NOT a blanket SELECT from the database.

Model (simplified):
  type appliance
    relations
      define owner: [user]
      define viewer: [user] or owner

  type unit
    relations
      define tenant: [user]
      define appliance_viewer: viewer from appliance

Usage::
  rebac = ReBACClient()
  # Check
  ok = await rebac.check("user:auth0|abc", "viewer", "appliance:samsung-wd85-3a")
  # List what a user can see
  appliances = await rebac.list_objects("user:auth0|abc", "viewer", "appliance")
  # Write a relationship (onboarding)
  await rebac.write_relationship("user:auth0|abc", "owner", "appliance:samsung-wd85-3a")
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("washfix.auth.rebac")


class ReBACClient:
    """
    Thin async client for Auth0 FGA (OpenFGA-compatible API).
    Falls back gracefully when FGA is not configured (dev mode).
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._fga_token: Optional[str] = None

    @property
    def _configured(self) -> bool:
        s = self._settings
        return bool(s.auth0_fga_store_id and s.auth0_fga_client_id)

    @property
    def _api_base(self) -> str:
        s = self._settings
        return f"https://api.us1.fga.dev/stores/{s.auth0_fga_store_id}"

    async def _get_fga_token(self) -> str:
        """Obtain an FGA-scoped access token via client-credentials."""
        if self._fga_token:
            return self._fga_token
        s = self._settings
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://{s.auth0_domain}/oauth/token",
                json={
                    "grant_type":    "client_credentials",
                    "client_id":     s.auth0_fga_client_id,
                    "client_secret": s.auth0_fga_client_secret,
                    "audience":      "https://api.us1.fga.dev/",
                },
            )
            resp.raise_for_status()
        self._fga_token = resp.json()["access_token"]
        logger.info("FGA token obtained.")
        return self._fga_token

    async def check(
        self,
        user: str,
        relation: str,
        object_type: str,
        object_id: str,
    ) -> bool:
        """
        Ask FGA: does `user` have `relation` on `object_type:object_id`?

        Example:
            ok = await rebac.check("user:auth0|abc", "viewer", "appliance", "samsung-wd85-3a")
        """
        if not self._configured:
            logger.warning("FGA not configured — permitting by default (dev mode).")
            return True

        token = await self._get_fga_token()
        payload = {
            "tuple_key": {
                "user":     user,
                "relation": relation,
                "object":   f"{object_type}:{object_id}",
            }
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._api_base}/check",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            resp.raise_for_status()

        allowed: bool = resp.json().get("allowed", False)
        logger.info(
            f"ReBAC check: user={user} relation={relation} "
            f"object={object_type}:{object_id} → {allowed}"
        )
        return allowed

    async def list_objects(
        self,
        user: str,
        relation: str,
        object_type: str,
    ) -> list[str]:
        """
        List all objects of `object_type` that `user` has `relation` on.
        Returns list of object IDs (without the type prefix).
        """
        if not self._configured:
            logger.warning("FGA not configured — returning wildcard (dev mode).")
            return ["*"]

        token = await self._get_fga_token()
        payload = {
            "user":        user,
            "relation":    relation,
            "object_type": object_type,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._api_base}/list-objects",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            resp.raise_for_status()

        objects = resp.json().get("objects", [])
        # Strip "appliance:" prefix
        ids = [o.split(":", 1)[1] if ":" in o else o for o in objects]
        logger.info(f"ReBAC list: user={user} relation={relation} type={object_type} → {ids}")
        return ids

    async def write_relationship(
        self,
        user: str,
        relation: str,
        object_type: str,
        object_id: str,
    ) -> None:
        """Assert a new relationship tuple in FGA."""
        if not self._configured:
            logger.warning("FGA not configured — skipping write (dev mode).")
            return

        token = await self._get_fga_token()
        payload = {
            "writes": {
                "tuple_keys": [{
                    "user":     user,
                    "relation": relation,
                    "object":   f"{object_type}:{object_id}",
                }]
            }
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._api_base}/write",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            resp.raise_for_status()
        logger.info(f"ReBAC write: {user} {relation} {object_type}:{object_id}")

    async def delete_relationship(
        self,
        user: str,
        relation: str,
        object_type: str,
        object_id: str,
    ) -> None:
        """Remove a relationship tuple (e.g. when a tenant moves out)."""
        if not self._configured:
            return

        token = await self._get_fga_token()
        payload = {
            "deletes": {
                "tuple_keys": [{
                    "user":     user,
                    "relation": relation,
                    "object":   f"{object_type}:{object_id}",
                }]
            }
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._api_base}/write",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            resp.raise_for_status()
        logger.info(f"ReBAC delete: {user} {relation} {object_type}:{object_id}")


# Singleton
rebac_client = ReBACClient()
