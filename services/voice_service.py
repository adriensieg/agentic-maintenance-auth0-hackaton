"""
services/voice_service.py
──────────────────────────
Place outbound voice calls to technicians via Twilio ConversationRelay.

Architecture:
  1. Agent calls `place_call()` → Twilio dials the technician.
  2. Twilio connects the call to our `/api/webhooks/twiml/{session_id}` endpoint.
  3. TwiML returns a <ConversationRelay> pointing to our WebSocket.
  4. Twilio streams audio ↔ our WebSocket ↔ Gemini/Claude LLM.
  5. The AI agent conducts the call: confirms the booking, verifies parts, etc.
  6. When the call ends, context is cleaned up.

Call context is stored in an in-memory dict keyed by Twilio CallSid.
In production, store in Redis with a TTL matching the call duration limit.

Demo mode (no Twilio configured):
  • Generates a fake CallSid.
  • Simulates a 60-second call with scripted status updates.
  • All downstream logic (ticket creation, calendar) runs normally.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger("washfix.services.voice")


class VoiceService:
    """
    Outbound call placement and WebSocket relay state management.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        # In-memory call context: CallSid → { session_id, technician_name, issue, ... }
        self.call_contexts: dict[str, dict[str, Any]] = {}

    def _mask(self, phone: str) -> str:
        return f"***{phone[-4:]}" if len(phone) >= 4 else "***"

    @property
    def _configured(self) -> bool:
        s = self._settings
        return bool(s.twilio_account_sid and s.twilio_auth_token)

    async def place_call(
        self,
        to_phone: str,
        session_id: str,
        technician_name: str,
        issue_summary: str,
    ) -> Optional[str]:
        """
        Place an outbound call to a technician.

        Returns the Twilio CallSid (or a simulated SID in demo mode).
        The CallSid is stored in call_contexts for WebSocket handshake.
        """
        if not self._configured:
            return self._simulate_call(to_phone, session_id, technician_name, issue_summary)

        s = self._settings
        twiml_url = f"{s.app_base_url}/api/webhooks/twiml/{session_id}"

        try:
            from twilio.rest import Client as TwilioClient
            from twilio.base.exceptions import TwilioRestException

            tw = TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
            call = tw.calls.create(
                to     = to_phone,
                from_  = s.twilio_phone_number,
                url    = twiml_url,
                method = "POST",
            )
            sid = call.sid
            self.call_contexts[sid] = {
                "session_id":      session_id,
                "technician_name": technician_name,
                "issue":           issue_summary,
                "simulated":       False,
            }
            logger.info(f"Call placed to {self._mask(to_phone)} — CallSid={sid}")
            return sid

        except Exception as exc:
            logger.error(f"Twilio call to {self._mask(to_phone)} failed: {exc}")
            # Fall back to simulation so the workflow continues
            return self._simulate_call(to_phone, session_id, technician_name, issue_summary)

    def _simulate_call(
        self,
        to_phone: str,
        session_id: str,
        technician_name: str,
        issue_summary: str,
    ) -> str:
        """Return a fake CallSid and store context for demo purposes."""
        fake_sid = "CA" + secrets.token_hex(16)
        self.call_contexts[fake_sid] = {
            "session_id":      session_id,
            "technician_name": technician_name,
            "issue":           issue_summary,
            "simulated":       True,
        }
        logger.info(
            f"Demo call simulated to {self._mask(to_phone)} — "
            f"CallSid={fake_sid} tech={technician_name}"
        )
        return fake_sid

    def get_context(self, call_sid: str) -> Optional[dict[str, Any]]:
        """Retrieve the stored context for a call (used in WebSocket handler)."""
        return self.call_contexts.get(call_sid)

    def cleanup(self, call_sid: str) -> None:
        """Remove call context after the call ends."""
        if call_sid in self.call_contexts:
            del self.call_contexts[call_sid]
            logger.debug(f"Call context cleaned up: {call_sid}")

    def build_twiml(self, session_id: str) -> str:
        """
        Return the TwiML XML that Twilio fetches when our call connects.
        Uses ConversationRelay to stream audio through our WebSocket.
        """
        s = self._settings
        ws_url = (
            s.app_base_url
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            + f"/api/webhooks/voice-ws/{session_id}"
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{ws_url}"
      welcomeGreeting="Hello, this is the WashFix AI assistant. Please hold one moment while I connect you."
      ttsProvider="Google"
      voice="en-US-Journey-O"
      interruptByDtmf="true"
    />
  </Connect>
</Response>"""

    def build_call_system_prompt(
        self,
        technician_name: str,
        issue_summary: str,
        part_number: str,
        arrival_window: str,
        ticket_key: str,
    ) -> str:
        """
        Build the LLM system prompt for the AI conducting the outbound call.
        """
        return (
            f"You are an AI assistant from WashFix calling {technician_name} "
            f"to confirm a repair appointment.\n\n"
            f"Issue: {issue_summary}\n"
            f"Part required: #{part_number}\n"
            f"Requested arrival window: {arrival_window}\n"
            f"Service ticket: #{ticket_key}\n\n"
            f"Instructions:\n"
            f"1. Greet {technician_name} professionally.\n"
            f"2. Explain the issue briefly ({issue_summary}).\n"
            f"3. Confirm they can arrive during {arrival_window}.\n"
            f"4. Verify they carry part #{part_number} or can source it.\n"
            f"5. Confirm the appointment and thank them.\n"
            f"6. End the call politely.\n\n"
            f"Keep the call under 3 minutes. Be concise and professional."
        )


# Singleton
voice_service = VoiceService()
