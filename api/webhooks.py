"""
api/webhooks.py
────────────────
/api/webhooks — Inbound webhook handlers.

POST /api/webhooks/twiml/{session_id}
  Twilio calls this when an outbound call connects.
  Returns TwiML <ConversationRelay> XML pointing to our WebSocket.

WS   /api/webhooks/voice-ws/{session_id}
  WebSocket endpoint for Twilio ConversationRelay.
  The AI agent (Gemini 2.5 Flash) conducts the call in real-time.

POST /api/webhooks/auth0-events
  Auth0 Log Streaming endpoint.
  Receives identity events (login, MFA, token issued) for audit enrichment.

POST /api/webhooks/stripe
  Stripe webhook (charge succeeded, failed, refunded).
  Verified via Stripe-Signature header.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from core.audit_log import audit_log

logger = logging.getLogger("washfix.api.webhooks")
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


# ── Twilio TwiML ──────────────────────────────────────────────────────────

@router.post("/twiml/{session_id}")
async def twiml_for_session(session_id: str, request: Request) -> Response:
    """
    Return TwiML ConversationRelay XML for an outbound technician call.
    Twilio fetches this URL when our outbound call is answered.
    """
    from services.voice_service import voice_service
    xml = voice_service.build_twiml(session_id)
    logger.info(f"TwiML served for session={session_id}")
    return Response(content=xml, media_type="text/xml")


# ── Twilio ConversationRelay WebSocket ────────────────────────────────────

@router.websocket("/voice-ws/{session_id}")
async def voice_websocket(websocket: WebSocket, session_id: str) -> None:
    """
    WebSocket handler for Twilio ConversationRelay.

    Message flow:
      Twilio → setup (with CallSid)
      Twilio → prompt (voicePrompt = transcribed speech)
      AI     → text   (response spoken by Twilio TTS)
      Twilio → end    (call ended)
    """
    await websocket.accept()
    call_sid: Optional[str] = None
    chat = None

    logger.info(f"Voice WS connected: session={session_id}")

    try:
        # Pre-load session context
        from core.session import session_manager
        session = await session_manager.get(session_id)
        tech_data = session.metadata.get("chosen_tech", {}) if session else {}
        tech_name = tech_data.get("name", "the technician")
        part_number = (
            session.diagnosis.part_number
            if session and session.diagnosis else "N/A"
        )
        issue = (
            session.diagnosis.description
            if session and session.diagnosis else "appliance repair"
        )
        ticket_key = session.metadata.get("ticket_key", "N/A") if session else "N/A"

        from services.voice_service import voice_service
        system_prompt = voice_service.build_call_system_prompt(
            technician_name = tech_name,
            issue_summary   = issue,
            part_number     = part_number,
            arrival_window  = "2:30–4:30 PM today",
            ticket_key      = ticket_key,
        )

        # Initialise Gemini chat (real-time, low-latency)
        from config.settings import get_settings
        s = get_settings()
        if s.gemini_api_key:
            from google import genai as google_genai
            gemini = google_genai.Client(api_key=s.gemini_api_key)
            chat = gemini.chats.create(
                model  = "gemini-2.5-flash",
                config = {"system_instruction": system_prompt},
            )

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "setup":
                call_sid = msg.get("callSid")
                logger.info(f"Voice WS setup: CallSid={call_sid} session={session_id}")

                if chat:
                    opening = chat.send_message(
                        "Start the call now. Greet the technician and explain the issue."
                    )
                    reply = opening.text
                else:
                    reply = (
                        f"Hello {tech_name}, this is WashFix AI calling to confirm "
                        f"your repair appointment. Are you available today from 2:30 PM?"
                    )

                await websocket.send_text(json.dumps({
                    "type": "text", "token": reply, "last": True
                }))

            elif msg_type == "prompt":
                voice_text = msg.get("voicePrompt", "")
                logger.info(f"Technician said: '{voice_text[:80]}' (CallSid={call_sid})")

                if chat:
                    resp = chat.send_message(voice_text)
                    reply = resp.text
                else:
                    reply = (
                        "Thank you for confirming. We'll see you at 2:30 PM. "
                        "Please bring the inlet solenoid valve if you have it. Goodbye."
                    )

                await websocket.send_text(json.dumps({
                    "type": "text", "token": reply, "last": True
                }))

            elif msg_type == "interrupt":
                logger.debug(f"Barge-in interrupt: CallSid={call_sid}")

            elif msg_type == "end":
                logger.info(f"Voice call ended: CallSid={call_sid} session={session_id}")
                break

            else:
                logger.debug(f"Unknown WS message type: {msg_type}")

    except WebSocketDisconnect:
        logger.info(f"Voice WS disconnected: session={session_id} CallSid={call_sid}")
    except Exception as exc:
        logger.exception(f"Voice WS error: session={session_id}: {exc}")
    finally:
        if call_sid:
            from services.voice_service import voice_service
            voice_service.cleanup(call_sid)


# ── Auth0 Log Stream ──────────────────────────────────────────────────────

@router.post("/auth0-events")
async def auth0_events(request: Request) -> dict:
    """
    Receive Auth0 Log Streaming events.
    Configured under: Auth0 Dashboard → Monitoring → Log Streams → HTTP.

    Events are written to the audit trail for compliance.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    event_type = body.get("type", "unknown")
    user_id    = body.get("user_id") or body.get("data", {}).get("user_id")

    logger.info(f"Auth0 event: type={event_type} user={user_id}")
    audit_log.log(
        action   = f"auth0:{event_type}",
        user_id  = user_id,
        actor    = "auth0",
        details  = body,
    )
    return {"status": "received"}


# ── Stripe Webhook ────────────────────────────────────────────────────────

@router.post("/stripe")
async def stripe_webhook(request: Request) -> dict:
    """
    Handle Stripe webhook events.
    Validates the Stripe-Signature header before processing.
    """
    from config.settings import get_settings
    s = get_settings()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    event: dict
    if s.stripe_webhook_secret and s.stripe_webhook_secret != "whsec_":
        try:
            import stripe
            event = stripe.Webhook.construct_event(
                payload, sig, s.stripe_webhook_secret
            )
        except Exception as exc:
            logger.warning(f"Invalid Stripe signature: {exc}")
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
    else:
        event = json.loads(payload)

    event_type = event.get("type", "")
    logger.info(f"Stripe webhook: {event_type}")

    if event_type == "payment_intent.succeeded":
        pi = event["data"]["object"]
        logger.info(f"PaymentIntent succeeded: {pi['id']} — ${pi['amount'] / 100:.2f}")
        audit_log.log(
            action  = "stripe:payment_intent.succeeded",
            actor   = "stripe",
            details = {"payment_intent_id": pi["id"], "amount": pi["amount"]},
        )

    elif event_type == "payment_intent.payment_failed":
        pi = event["data"]["object"]
        logger.warning(f"PaymentIntent failed: {pi['id']}")
        audit_log.log(
            action  = "stripe:payment_intent.failed",
            actor   = "stripe",
            details = {"payment_intent_id": pi["id"]},
        )

    elif event_type == "setup_intent.succeeded":
        si = event["data"]["object"]
        logger.info(f"SetupIntent succeeded: {si['id']}")

    return {"status": "ok"}
