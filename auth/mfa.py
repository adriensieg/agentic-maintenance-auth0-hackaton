"""
auth/mfa.py
────────────
MFA enrollment & OTP verification.

Two mechanisms are supported:

1. Auth0 MFA (Guardian / SMS) — for Auth0-native users.
   Sends an OTP challenge to the user's registered phone via Auth0.
   Verified by POSTing the code to Auth0's MFA token endpoint.

2. Twilio Verify — fallback / explicit SMS OTP.
   Used for payment confirmation when we want explicit control over
   the SMS content and delivery (the CIBA flow triggers this).

Both flows result in a boolean is_verified + an optional short-lived
session_token that the agent includes in downstream API calls as proof
of MFA completion.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("washfix.auth.mfa")


class MFAClient:
    """Coordinate MFA challenges and verification."""

    def __init__(self) -> None:
        self._settings = get_settings()

    # ── Auth0 MFA (Guardian) ─────────────────────────────────────────────

    async def challenge_auth0_mfa(
        self,
        mfa_token: str,
        challenge_type: str = "oob",  # "oob" = out-of-band (SMS/email)
        authenticator_id: Optional[str] = None,
    ) -> dict:
        """
        Request an MFA challenge from Auth0.
        `mfa_token` is the token returned by Auth0 when step-up MFA is needed.
        Returns challenge details including `oob_code`.
        """
        s = self._settings
        payload: dict = {
            "client_id":     s.auth0_client_id,
            "client_secret": s.auth0_client_secret,
            "challenge_type": challenge_type,
            "mfa_token":     mfa_token,
        }
        if authenticator_id:
            payload["authenticator_id"] = authenticator_id

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://{s.auth0_domain}/mfa/challenge",
                data=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        logger.info(f"Auth0 MFA challenge issued: oob_code_prefix={str(data.get('oob_code',''))[:8]}")
        return data

    async def verify_auth0_mfa(
        self,
        mfa_token: str,
        oob_code: str,
        binding_code: str,
    ) -> dict:
        """
        Complete MFA by submitting the OTP that the user received.
        Returns tokens (access_token, etc.) on success.
        Raises RuntimeError on failure.
        """
        s = self._settings
        payload = {
            "grant_type":    "http://auth0.com/oauth/grant-type/mfa-oob",
            "client_id":     s.auth0_client_id,
            "client_secret": s.auth0_client_secret,
            "mfa_token":     mfa_token,
            "oob_code":      oob_code,
            "binding_code":  binding_code,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(s.auth0_token_url, data=payload)

        if resp.status_code != 200:
            error = resp.json().get("error", "")
            if error == "invalid_grant":
                raise RuntimeError("MFA: invalid or expired OTP.")
            raise RuntimeError(f"MFA verification failed: {resp.status_code} — {resp.text}")

        logger.info("Auth0 MFA OTP verified successfully.")
        return resp.json()

    # ── Twilio Verify ─────────────────────────────────────────────────────

    async def send_twilio_otp(self, phone_number: str) -> str:
        """
        Send a one-time SMS passcode via Twilio Verify.
        Returns the verification SID.
        """
        s = self._settings
        if not s.twilio_verify_service_sid:
            logger.warning("Twilio Verify SID not configured — generating demo OTP.")
            return "DEMO"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://verify.twilio.com/v2/Services/"
                f"{s.twilio_verify_service_sid}/Verifications",
                auth=(s.twilio_account_sid, s.twilio_auth_token),
                data={"To": phone_number, "Channel": "sms"},
            )
            resp.raise_for_status()

        sid = resp.json().get("sid", "")
        logger.info(f"Twilio Verify OTP sent to {phone_number[-4:]:>4}**** sid={sid}")
        return sid

    async def verify_twilio_otp(self, phone_number: str, code: str) -> bool:
        """
        Check a Twilio Verify code.
        Returns True if approved, False otherwise.
        """
        s = self._settings
        if not s.twilio_verify_service_sid:
            # Demo mode: any 4-digit code passes
            logger.warning("Twilio Verify not configured — demo mode, accepting any code.")
            return len(code) == 4 and code.isdigit()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://verify.twilio.com/v2/Services/"
                f"{s.twilio_verify_service_sid}/VerificationCheck",
                auth=(s.twilio_account_sid, s.twilio_auth_token),
                data={"To": phone_number, "Code": code},
            )

        if resp.status_code != 200:
            logger.warning(f"Twilio Verify check failed: {resp.status_code}")
            return False

        status = resp.json().get("status")
        approved = status == "approved"
        logger.info(f"Twilio Verify check: status={status} approved={approved}")
        return approved

    def generate_demo_otp(self, length: int = 4) -> str:
        """Generate a demo OTP code (used when Twilio is not configured)."""
        return str(secrets.randbelow(10**length)).zfill(length)


# Singleton
mfa_client = MFAClient()
