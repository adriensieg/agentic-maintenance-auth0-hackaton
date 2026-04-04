"""
services/sms_service.py
────────────────────────
Send SMS messages via Twilio (plain messages — not Verify OTP).

For OTP / MFA verification use auth/mfa.py (Twilio Verify Service).
This module handles:
  • Payment authorisation SMS (OTP code delivery)
  • Technician arrival reminders (15 min notice)
  • Booking confirmation receipts
  • General notifications

All phone numbers are masked in logs (last 4 digits only).
"""
from __future__ import annotations

import logging
from typing import Optional

from config.settings import get_settings

logger = logging.getLogger("washfix.services.sms")


class SMSService:
    """
    Send SMS via Twilio REST API.
    Gracefully degrades when Twilio is not configured.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def _mask(self, phone: str) -> str:
        """Mask a phone number for log output."""
        return f"***{phone[-4:]}" if len(phone) >= 4 else "***"

    async def send(self, to: str, body: str) -> Optional[str]:
        """
        Send a plain SMS message.

        Returns the Twilio message SID on success, None on failure.
        """
        s = self._settings
        if not s.twilio_account_sid or not s.twilio_auth_token:
            logger.warning(
                f"Twilio not configured — SMS to {self._mask(to)} not sent. "
                f"Message: {body[:60]}..."
            )
            return None

        try:
            from twilio.rest import Client as TwilioClient
            tw = TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
            msg = tw.messages.create(
                to    = to,
                from_ = s.twilio_phone_number,
                body  = body,
            )
            logger.info(f"SMS sent to {self._mask(to)} — sid={msg.sid}")
            return msg.sid
        except Exception as exc:
            logger.error(f"SMS send to {self._mask(to)} failed: {exc}")
            return None

    async def send_otp(
        self,
        to: str,
        otp_code: str,
        ticket_key: str,
        amount: str,
    ) -> Optional[str]:
        """
        Send the payment authorisation OTP to the user's phone.

        Message format matches the demo UI SMS overlay exactly.
        """
        body = (
            f"WashFix: Your AI assistant is requesting payment of {amount}.\n"
            f"Your one-time code: {otp_code}\n"
            f"Ref: Jira #{ticket_key} — Expires in 3 minutes. Do not share."
        )
        return await self.send(to, body)

    async def send_booking_confirmation(
        self,
        to: str,
        technician_name: str,
        arrival_start: str,
        arrival_end: str,
        ticket_key: str,
        address: str,
    ) -> Optional[str]:
        """Send booking confirmation SMS to the resident."""
        body = (
            f"WashFix Booking Confirmed ✓\n"
            f"Technician: {technician_name}\n"
            f"Arrival: {arrival_start}–{arrival_end}\n"
            f"Address: {address}\n"
            f"Ticket: #{ticket_key}\n"
            f"Reply HELP for support."
        )
        return await self.send(to, body)

    async def send_arrival_reminder(
        self,
        to: str,
        technician_name: str,
        minutes_away: int,
        ticket_key: str,
    ) -> Optional[str]:
        """
        Send a '15 minutes away' arrival reminder SMS.
        Called by a background worker before the arrival window.
        """
        body = (
            f"WashFix: {technician_name} is {minutes_away} minutes away. "
            f"Ref: #{ticket_key}. "
            f"Reply STOP to cancel."
        )
        return await self.send(to, body)

    async def send_payment_receipt(
        self,
        to: str,
        amount: str,
        technician_name: str,
        ticket_key: str,
    ) -> Optional[str]:
        """Send payment receipt SMS after successful charge."""
        body = (
            f"WashFix Receipt ✓\n"
            f"Amount: {amount} charged\n"
            f"Service: Appliance repair — {technician_name}\n"
            f"Ref: #{ticket_key}\n"
            f"Questions? Reply HELP."
        )
        return await self.send(to, body)

    async def send_cancellation(
        self,
        to: str,
        ticket_key: str,
    ) -> Optional[str]:
        """Notify the resident that a booking has been cancelled."""
        body = (
            f"WashFix: Your repair appointment #{ticket_key} has been cancelled. "
            f"Contact your building manager to rebook."
        )
        return await self.send(to, body)


# Singleton
sms_service = SMSService()
