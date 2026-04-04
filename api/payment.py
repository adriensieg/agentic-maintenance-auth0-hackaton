"""
api/payment.py
───────────────
/api/payment — Payment initiation, MFA verification, CIBA status.

POST /api/payment/initiate
  Start CIBA flow + send SMS OTP for a given session.

POST /api/payment/verify-otp
  Verify the OTP and charge via Stripe.

GET /api/payment/ciba-status/{session_id}
  Poll the CIBA status (used by frontend long-poll).

POST /api/payment/webhook (Stripe)
  Handle Stripe webhook events (charge.succeeded, etc.).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from auth.middleware import get_subject, require_auth
from core.audit_log  import audit_log
from core.session    import session_manager
from models          import SessionPhase

logger = logging.getLogger("washfix.api.payment")
router = APIRouter(prefix="/api/payment", tags=["payment"])


class OTPVerifyRequest(BaseModel):
    session_id: str
    otp:        str


class PaymentInitRequest(BaseModel):
    session_id: str
    amount_usd: float


@router.post("/initiate")
async def initiate_payment(
    body:   PaymentInitRequest,
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """
    Initiate CIBA + SMS OTP for payment.
    Returns { ciba_auth_req_id, otp_sent: bool, phone_hint }.
    """
    user_id = get_subject(claims)
    session = await session_manager.get(body.session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found.")

    from auth.ciba import ciba_client
    from auth.mfa  import mfa_client
    from services  import sms_service

    amount = body.amount_usd
    binding_msg = f"Approve repair payment of ${amount:.2f}?"

    ciba_auth_req_id = None
    try:
        ciba_req = await ciba_client.initiate(
            user_id     = user_id,
            scope       = "openid payment:approve",
            binding_msg = binding_msg,
            context     = {"amount": amount},
        )
        ciba_auth_req_id = ciba_req.auth_req_id
        await session_manager.set_meta(body.session_id, "ciba_auth_req_id", ciba_auth_req_id)
        audit_log.ciba_initiated(body.session_id, user_id, "openid payment:approve", binding_msg)
    except Exception as exc:
        logger.warning(f"CIBA unavailable: {exc}")

    # SMS OTP
    otp = mfa_client.generate_demo_otp()
    phone = session.user_phone or "+13125550000"
    await session_manager.set_meta(body.session_id, "pending_otp", otp)
    await session_manager.set_meta(body.session_id, "pending_amount_cents", int(amount * 100))
    await session_manager.update_phase(body.session_id, SessionPhase.PAYMENT_MFA)
    audit_log.mfa_sent(body.session_id, user_id, "sms")

    await sms_service.send(
        phone,
        f"WashFix: Your authorisation code is {otp}. "
        f"Expires in 3 minutes. Do not share."
    )
    logger.info(f"Payment OTP sent for session {body.session_id}.")

    return {
        "ciba_auth_req_id": ciba_auth_req_id,
        "otp_sent":         True,
        "phone_hint":       f"···{phone[-4:]}",
        "demo_otp":         otp,  # only in dev — remove for production
    }


@router.post("/verify-otp")
async def verify_otp(
    body:   OTPVerifyRequest,
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """
    Verify OTP and process Stripe payment.
    """
    user_id = get_subject(claims)
    session = await session_manager.get(body.session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found.")

    expected     = session.metadata.get("pending_otp", "")
    amount_cents = session.metadata.get("pending_amount_cents", 17800)
    ticket_key   = session.metadata.get("ticket_key", "UNKNOWN")

    # Verify
    from auth.mfa import mfa_client
    verified = (body.otp == expected) or await mfa_client.verify_twilio_otp(
        session.user_phone or "+13125550000", body.otp
    )
    if not verified:
        audit_log.mfa_failed(body.session_id, user_id)
        raise HTTPException(status_code=403, detail="Invalid or expired OTP.")

    audit_log.mfa_verified(body.session_id, user_id)

    # Process payment
    from services import payment_service
    from utils.crypto import generate_idempotency_key
    idempotency_key = generate_idempotency_key(body.session_id, ticket_key)

    result = await payment_service.charge(
        user_id         = user_id,
        amount_cents    = amount_cents,
        description     = f"Appliance repair — {ticket_key}",
        idempotency_key = idempotency_key,
    )

    if result.get("status") in ("succeeded", "requires_capture"):
        audit_log.payment_charged(
            body.session_id, user_id,
            amount_cents, result.get("payment_intent_id", "")
        )
        # Update Jira
        try:
            from services.jira_service import jira_service
            await jira_service.add_comment(
                ticket_key,
                f"Payment of ${amount_cents/100:.2f} processed. "
                f"PI: {result.get('payment_intent_id')}."
            )
        except Exception as exc:
            logger.warning(f"Jira comment on payment failed: {exc}")

        await session_manager.update_phase(body.session_id, SessionPhase.COMPLETE)
        audit_log.repair_complete(body.session_id, ticket_key)

        return {
            "status":            "succeeded",
            "payment_intent_id": result.get("payment_intent_id"),
            "amount_usd":        amount_cents / 100,
            "ticket_key":        ticket_key,
        }
    else:
        raise HTTPException(
            status_code=402,
            detail=f"Payment failed: {result.get('error', 'Unknown error')}",
        )


@router.get("/ciba-status/{session_id}")
async def ciba_status(
    session_id: str,
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Poll the current CIBA authorisation status."""
    user_id = get_subject(claims)
    session = await session_manager.get(session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found.")

    return {
        "phase":             session.phase.value,
        "ciba_auth_req_id":  session.metadata.get("ciba_auth_req_id"),
        "payment_pending":   session.phase == SessionPhase.PAYMENT_MFA,
    }


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict[str, str]:
    """
    Handle Stripe webhook events.
    Verifies the webhook signature before processing.
    """
    from config.settings import get_settings
    import stripe

    s = get_settings()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if s.stripe_webhook_secret:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, s.stripe_webhook_secret
            )
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
    else:
        import json
        event = json.loads(payload)

    event_type = event.get("type", "")
    logger.info(f"Stripe webhook: {event_type}")

    if event_type == "payment_intent.succeeded":
        pi = event["data"]["object"]
        logger.info(f"PaymentIntent succeeded: {pi['id']} — ${pi['amount']/100:.2f}")

    elif event_type == "payment_intent.payment_failed":
        pi = event["data"]["object"]
        logger.warning(f"PaymentIntent failed: {pi['id']}")

    return {"status": "ok"}
