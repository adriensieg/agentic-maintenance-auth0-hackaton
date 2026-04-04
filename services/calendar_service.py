"""
services/calendar_service.py
─────────────────────────────
Add repair appointments to the resident's Google Calendar.

Uses a scoped OAuth access token stored in the Token Vault
(scope: https://www.googleapis.com/auth/calendar.events).

Token lifecycle:
  • Token is retrieved from vault (Auth0 app_metadata).
  • After the event is created the access token is INVALIDATED (single-use).
  • The refresh token remains for the next call — rotation handled by vault.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("washfix.services.calendar")

CALENDAR_API = "https://www.googleapis.com/calendar/v3"


class CalendarService:
    """
    Create / update Google Calendar events for repair appointments.
    All API calls use scoped tokens from the Token Vault.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    async def _get_token(self, user_id: str) -> Optional[str]:
        """
        Retrieve a Google Calendar access token from the Token Vault.
        Falls back to a service-account token if no per-user token exists.
        """
        try:
            from auth.token_vault import token_vault
            bundle = await token_vault.get(user_id, "google_calendar")
            if bundle and bundle.get("access_token"):
                return bundle["access_token"]
        except Exception as exc:
            logger.warning(f"Vault token fetch failed: {exc}")

        # Fallback: Auth0 client-credentials for a shared calendar service account
        s = self._settings
        if not s.google_client_id:
            logger.warning("No Google Calendar token available — event creation skipped.")
            return None

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(s.auth0_token_url, json={
                    "grant_type":    "client_credentials",
                    "client_id":     s.auth0_client_id,
                    "client_secret": s.auth0_client_secret,
                    "audience":      "https://www.googleapis.com/",
                })
            if resp.status_code == 200:
                return resp.json().get("access_token")
        except Exception as exc:
            logger.warning(f"Fallback token fetch failed: {exc}")

        return None

    async def create_event(
        self,
        user_id: str,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        location: Optional[str] = None,
        timezone_str: str = "America/Chicago",
    ) -> Optional[str]:
        """
        Create a calendar event.

        Returns the Google event ID on success, or a demo ID on failure.
        Access token is invalidated after a successful create (single-use).
        """
        token = await self._get_token(user_id)
        if not token:
            demo_id = f"demo-event-{secrets.token_hex(4)}"
            logger.info(f"No Calendar token — returning demo event ID: {demo_id}")
            return demo_id

        body: dict[str, Any] = {
            "summary":     summary,
            "description": description,
            "start":       {"dateTime": start.isoformat(), "timeZone": timezone_str},
            "end":         {"dateTime": end.isoformat(),   "timeZone": timezone_str},
        }
        if location:
            body["location"] = location

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{CALENDAR_API}/calendars/primary/events",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type":  "application/json",
                    },
                    json=body,
                )

            if resp.status_code in (200, 201):
                event_id = resp.json().get("id")
                logger.info(f"Calendar event created: {event_id}")
                # Invalidate single-use access token
                try:
                    from auth.token_vault import token_vault
                    await token_vault.invalidate_access_token(user_id, "google_calendar")
                except Exception:
                    pass
                return event_id
            else:
                logger.error(
                    f"Calendar API error: HTTP {resp.status_code} — {resp.text[:200]}"
                )
        except Exception as exc:
            logger.error(f"Calendar create_event failed: {exc}")

        return f"demo-event-{secrets.token_hex(4)}"

    async def block_repair_window(
        self,
        user_id: str,
        technician_name: str,
        ticket_key: str,
        address: str,
        arrival_start: datetime,
        arrival_end: datetime,
    ) -> Optional[str]:
        """
        Convenience: create a repair appointment block on the user's calendar.
        """
        summary = f"Appliance Repair — {technician_name}"
        description = (
            f"Technician: {technician_name}\n"
            f"Service Ticket: {ticket_key}\n"
            f"Location: {address}\n"
            f"Booked by WashFix AI Agent."
        )
        return await self.create_event(
            user_id     = user_id,
            summary     = summary,
            description = description,
            start       = arrival_start,
            end         = arrival_end,
            location    = address,
        )

    async def delete_event(self, user_id: str, event_id: str) -> bool:
        """Delete a calendar event (e.g. if booking is cancelled)."""
        if event_id.startswith("demo-"):
            return True

        token = await self._get_token(user_id)
        if not token:
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(
                    f"{CALENDAR_API}/calendars/primary/events/{event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code in (200, 204):
                logger.info(f"Calendar event deleted: {event_id}")
                return True
        except Exception as exc:
            logger.warning(f"Calendar delete failed: {exc}")

        return False


# Singleton
calendar_service = CalendarService()
