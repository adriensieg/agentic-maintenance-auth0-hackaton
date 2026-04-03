"""
auth/ciba.py
─────────────
Client-Initiated Backchannel Authentication (CIBA) — RFC 9126 / OpenID CIBA.

This is used for TWO high-value actions that require explicit user consent
WITHOUT opening a browser redirect:

  1. Booking confirmation   — confirm appointment with the technician.
  2. Payment authorisation  — approve the $178.00 charge.

Flow:
  ┌──────────┐  (1) bc-authorize  ┌──────────┐
  │  Agent   │ ────────────────▶  │  Auth0   │
  └──────────┘                    └────┬─────┘
                                       │ Push notification / SMS to user
                                  ┌────▼─────┐
                                  │  User    │ approves on device
                                  └────┬─────┘
  ┌──────────┐  (2) poll token    ┌────▼─────┐
  │  Agent   │ ◀──── granted ──── │  Auth0   │
  └──────────┘                    └──────────┘

Auth0 CIBA requires:
  • `grant_types_supported` includes `urn:openid:params:grant-type:ciba`
  • Application type: "Native" or "Regular Web App" with CIBA enabled
  • Binding message shown to user for context

Reference: https://auth0.com/docs/get-started/authentication-and-authorization-flow/client-initiated-backchannel-authentication-flow
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("washfix.auth.ciba")

CIBA_GRANT_TYPE = "urn:openid:params:grant-type:ciba"
POLL_INTERVAL   = 5   # seconds between polls
MAX_POLLS       = 60  # 5 min timeout


class CIBAStatus(str, Enum):
    PENDING  = "pending"
    GRANTED  = "granted"
    DENIED   = "denied"
    EXPIRED  = "expired"


class CIBARequest:
    """Represents an in-flight CIBA authorisation request."""

    def __init__(
        self,
        auth_req_id: str,
        expires_in: int,
        interval: int,
        binding_message: str,
        user_id: str,
        scope: str,
        context: dict[str, Any],
    ) -> None:
        self.auth_req_id      = auth_req_id
        self.expires_in       = expires_in
        self.interval         = max(interval, POLL_INTERVAL)
        self.binding_message  = binding_message
        self.user_id          = user_id
        self.scope            = scope
        self.context          = context
        self.status           = CIBAStatus.PENDING
        self.tokens: Optional[dict[str, Any]] = None


class CIBAClient:
    """
    Async CIBA client for Auth0.

    Usage::
        ciba = CIBAClient()

        # Initiate — sends push/SMS to user
        req = await ciba.initiate(
            user_id     = "auth0|abc",
            scope       = "openid payment:approve",
            binding_msg = "Approve $178.00 to AllPro Appliance?",
            context     = {"amount": 178.00, "vendor": "AllPro Appliance"},
        )

        # Poll until granted / denied / expired
        tokens = await ciba.poll_until_granted(req)
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    async def initiate(
        self,
        user_id: str,
        scope: str,
        binding_msg: str,
        acr_values: str = "http://schemas.openid.net/pape/policies/2007/06/multi-factor",
        context: Optional[dict[str, Any]] = None,
    ) -> CIBARequest:
        """
        Send a CIBA backchannel authorisation request to Auth0.
        Returns a CIBARequest object that can be polled.

        `binding_msg` is shown to the user in the push notification
        so they know what they are approving.
        """
        s = self._settings
        logger.info(
            f"CIBA initiate: user={user_id} "
            f"scope='{scope}' msg='{binding_msg}'"
        )

        payload = {
            "client_id":         s.auth0_client_id,
            "client_secret":     s.auth0_client_secret,
            "scope":             scope,
            "login_hint":        f'{{"format":"iss_sub","iss":"{s.auth0_issuer}","sub":"{user_id}"}}',
            "binding_message":   binding_msg,
            "acr_values":        acr_values,
            "requested_expiry":  300,  # 5 minutes
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                s.auth0_backchannel_url,
                data=payload,  # application/x-www-form-urlencoded
            )

        if resp.status_code != 200:
            logger.error(f"CIBA initiate failed: HTTP {resp.status_code} — {resp.text}")
            raise RuntimeError(
                f"CIBA initiate failed: {resp.status_code} — {resp.text}"
            )

        data = resp.json()
        req = CIBARequest(
            auth_req_id     = data["auth_req_id"],
            expires_in      = data.get("expires_in", 300),
            interval        = data.get("interval", POLL_INTERVAL),
            binding_message = binding_msg,
            user_id         = user_id,
            scope           = scope,
            context         = context or {},
        )
        logger.info(f"CIBA auth_req_id={req.auth_req_id} expires_in={req.expires_in}s")
        return req

    async def poll(self, req: CIBARequest) -> CIBAStatus:
        """
        Poll Auth0 once for the status of a CIBA request.
        Updates `req.status` and `req.tokens` in place.
        """
        s = self._settings
        payload = {
            "grant_type":   CIBA_GRANT_TYPE,
            "client_id":    s.auth0_client_id,
            "client_secret": s.auth0_client_secret,
            "auth_req_id":  req.auth_req_id,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(s.auth0_token_url, data=payload)

        if resp.status_code == 200:
            req.tokens = resp.json()
            req.status = CIBAStatus.GRANTED
            logger.info(f"CIBA GRANTED — auth_req_id={req.auth_req_id}")
            return CIBAStatus.GRANTED

        error = resp.json().get("error", "")
        if error == "authorization_pending":
            return CIBAStatus.PENDING
        if error == "slow_down":
            req.interval += 5
            logger.debug("CIBA slow_down — increasing poll interval.")
            return CIBAStatus.PENDING
        if error in ("access_denied", "transaction_failed"):
            req.status = CIBAStatus.DENIED
            logger.warning(f"CIBA DENIED — auth_req_id={req.auth_req_id}")
            return CIBAStatus.DENIED
        if error == "expired_token":
            req.status = CIBAStatus.EXPIRED
            logger.warning(f"CIBA EXPIRED — auth_req_id={req.auth_req_id}")
            return CIBAStatus.EXPIRED

        # Unexpected error
        logger.error(f"CIBA poll unexpected error: {resp.status_code} — {resp.text}")
        return CIBAStatus.PENDING

    async def poll_until_granted(
        self,
        req: CIBARequest,
        on_pending: Optional[Any] = None,
    ) -> dict[str, Any]:
        """
        Async-poll until the CIBA request is granted, denied, or expired.

        `on_pending` is an optional async callable invoked on each pending
        tick — useful for sending SSE events to the frontend.

        Raises RuntimeError if denied or expired.
        Returns the token dict on success.
        """
        polls = 0
        while polls < MAX_POLLS:
            await asyncio.sleep(req.interval)
            status = await self.poll(req)

            if status == CIBAStatus.GRANTED:
                return req.tokens  # type: ignore[return-value]
            if status == CIBAStatus.DENIED:
                raise RuntimeError("CIBA: user denied the authorisation request.")
            if status == CIBAStatus.EXPIRED:
                raise RuntimeError("CIBA: authorisation request expired.")

            polls += 1
            if on_pending:
                await on_pending(polls)

        raise RuntimeError("CIBA: timed out waiting for user approval.")

    async def verify_token(self, access_token: str, expected_scope: str) -> bool:
        """
        Light verification that the CIBA-obtained token contains the
        expected scope.  The full JWT verification is done in middleware.
        """
        from auth.middleware import verify_bearer_token
        try:
            claims = await verify_bearer_token(access_token)
            token_scopes = claims.get("scope", "").split()
            for scope in expected_scope.split():
                if scope not in token_scopes:
                    logger.warning(f"CIBA token missing scope: {scope}")
                    return False
            logger.info("CIBA token scope verified.")
            return True
        except Exception as exc:
            logger.error(f"CIBA token verification failed: {exc}")
            return False


# Singleton
ciba_client = CIBAClient()
