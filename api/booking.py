"""
api/booking.py
───────────────
/api/booking — Technician availability and booking confirmation.

GET  /api/booking/technicians
  Return available technicians (DCR-secured field-ops API call).

POST /api/booking/confirm
  Confirm technician selection for a session.
  Triggers CIBA confirmation flow + Twilio voice call.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.middleware import get_subject, require_auth
from core.audit_log  import audit_log

logger = logging.getLogger("washfix.api.booking")
router = APIRouter(prefix="/api/booking", tags=["booking"])


class BookingRequest(BaseModel):
    session_id:    str
    technician_id: str


@router.get("/technicians")
async def list_technicians(
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """
    Return available technicians near the user's unit.
    Uses DCR (Dynamic Client Registration) to mint a short-lived OAuth
    client for the field-ops API, then deletes it after the call.
    """
    from services.technician_service import technician_service
    techs = await technician_service.get_available()
    return {
        "technicians": [t.model_dump() for t in techs],
        "count":       len(techs),
    }


@router.post("/confirm")
async def confirm_booking(
    body:   BookingRequest,
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """
    Confirm a technician booking for a session.

    1. Verify session ownership.
    2. Fetch technician details.
    3. Call booking API (DCR-secured).
    4. Log audit event.
    """
    user_id = get_subject(claims)
    from core.session import session_manager
    session = await session_manager.get(body.session_id)

    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found.")

    from services.technician_service import technician_service
    tech = await technician_service.get_by_id(body.technician_id)
    if not tech:
        raise HTTPException(status_code=404, detail="Technician not found.")

    confirmation = await technician_service.book(
        technician    = tech,
        session_id    = body.session_id,
        issue_summary = (
            session.diagnosis.description
            if session.diagnosis else "Appliance repair"
        ),
    )

    audit_log.technician_selected(
        body.session_id, tech.id, f"{tech.name} — {tech.company}"
    )

    # Store chosen technician in session metadata
    await session_manager.set_meta(body.session_id, "chosen_tech", tech.model_dump())

    return {
        "status":     "confirmed",
        "booking":    confirmation,
        "technician": tech.model_dump(),
    }
