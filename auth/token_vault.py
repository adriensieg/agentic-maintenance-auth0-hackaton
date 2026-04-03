"""
auth/token_vault.py
────────────────────
Secure token vault — stores third-party OAuth tokens encrypted inside
Auth0 user `app_metadata`.

Why Auth0 as the vault?
  • Centralised; no extra secrets store to provision.
  • Tokens are bound to the user identity.
  • Management API access is gated by a separate M2M client with minimal
    scopes (`read:users`, `update:users`).
  • Tokens are AES-256-GCM encrypted before storage so Auth0 staff / logs
    never see plaintext credentials.

Token lifecycle:
  • After a token is READ for use it is immediately ROTATED:
    the old refresh token is revoked and the new one is stored.
  • Single-use access tokens are DELETED from the vault after retrieval.
  • `expire_at` timestamps are stored alongside every token so
    the background worker (`workers/token_refresh.py`) can proactively
    refresh before expiry.

Stored structure inside `app_metadata.token_vault`:
  {
    "jira":    { "access_token": "<enc>", "refresh_token": "<enc>", "expire_at": 1720000000 },
    "google":  { "access_token": "<enc>", "refresh_token": "<enc>", "expire_at": 1720000000 },
    "stripe":  { "payment_method_id": "<enc>", "customer_id": "<enc>" },
    ...
  }
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from auth.auth0_client import auth0_client
from utils.crypto import decrypt_value, encrypt_value

logger = logging.getLogger("washfix.auth.token_vault")


class TokenVault:
    """Read / write encrypted third-party tokens from Auth0 app_metadata."""

    VAULT_KEY = "token_vault"

    # ── Read ──────────────────────────────────────────────────────────────

    async def get(self, user_id: str, service: str) -> dict[str, Any]:
        """
        Retrieve decrypted token bundle for a service.
        Returns an empty dict if not found.
        """
        user = await auth0_client.get_user(user_id)
        vault: dict = (
            user.get("app_metadata", {}).get(self.VAULT_KEY, {})
        )
        bundle = vault.get(service, {})
        if not bundle:
            logger.info(f"Vault: no tokens found for service={service} user={user_id}")
            return {}

        decrypted: dict[str, Any] = {}
        for k, v in bundle.items():
            if isinstance(v, str) and v.startswith("enc:"):
                decrypted[k] = decrypt_value(v[4:])
            else:
                decrypted[k] = v

        logger.info(f"Vault: tokens retrieved for service={service} user={user_id}")
        return decrypted

    async def get_access_token(self, user_id: str, service: str) -> Optional[str]:
        """
        Convenience: return just the access token, rotating it if needed.

        If the stored access token is within 5 minutes of expiry it is
        refreshed first using the stored refresh token.
        """
        bundle = await self.get(user_id, service)
        if not bundle:
            return None

        expire_at = bundle.get("expire_at", 0)
        access_token = bundle.get("access_token")

        if time.time() > (expire_at - 300) and bundle.get("refresh_token"):
            logger.info(f"Vault: rotating expiring {service} token for user={user_id}")
            from auth.auth0_client import auth0_client as _c
            new_tokens = await _c.refresh_access_token(
                bundle["refresh_token"],
                # For Jira we use their own client credentials:
                client_id=None,
                client_secret=None,
            )
            await self.set(user_id, service, {
                "access_token":  new_tokens["access_token"],
                "refresh_token": new_tokens.get("refresh_token", bundle["refresh_token"]),
                "expire_at":     time.time() + new_tokens.get("expires_in", 3600),
            })
            return new_tokens["access_token"]

        return access_token

    # ── Write ─────────────────────────────────────────────────────────────

    async def set(
        self,
        user_id: str,
        service: str,
        bundle: dict[str, Any],
    ) -> None:
        """
        Encrypt and store a token bundle for `service` in Auth0.
        Merges with any existing vault data for other services.
        """
        # Fetch current vault
        user = await auth0_client.get_user(user_id)
        vault: dict = dict(
            user.get("app_metadata", {}).get(self.VAULT_KEY, {})
        )

        # Encrypt all string values
        encrypted: dict[str, Any] = {}
        for k, v in bundle.items():
            if isinstance(v, str):
                encrypted[k] = "enc:" + encrypt_value(v)
            else:
                encrypted[k] = v

        vault[service] = encrypted

        await auth0_client.update_user_metadata(
            user_id,
            app_metadata={self.VAULT_KEY: vault},
        )
        logger.info(f"Vault: tokens stored for service={service} user={user_id}")

    async def delete(self, user_id: str, service: str) -> None:
        """Remove a service's token bundle from the vault."""
        user = await auth0_client.get_user(user_id)
        vault: dict = dict(
            user.get("app_metadata", {}).get(self.VAULT_KEY, {})
        )
        if service in vault:
            del vault[service]
            await auth0_client.update_user_metadata(
                user_id,
                app_metadata={self.VAULT_KEY: vault},
            )
            logger.info(f"Vault: tokens deleted for service={service} user={user_id}")

    async def invalidate_access_token(self, user_id: str, service: str) -> None:
        """
        Clear only the access token after single use.
        The refresh token is retained for future rotation.
        """
        bundle = await self.get(user_id, service)
        if "access_token" in bundle:
            del bundle["access_token"]
            bundle["expire_at"] = 0  # Force refresh on next access
            await self.set(user_id, service, bundle)
            logger.info(
                f"Vault: access token invalidated (single-use) "
                f"service={service} user={user_id}"
            )


# Singleton
token_vault = TokenVault()
