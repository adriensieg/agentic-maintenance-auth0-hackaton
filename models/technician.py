"""
models/technician.py
─────────────────────
Pydantic models for technician data returned from the field-ops API.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TechnicianStatus(str, Enum):
    AVAILABLE = "available"
    BUSY      = "busy"
    OFFLINE   = "offline"


class Technician(BaseModel):
    """
    A certified appliance repair technician.
    Data sourced from the DCR-secured field-ops API (or demo fallback).
    """
    id:              str
    name:            str
    company:         str
    phone:           str
    rating:          float
    review_count:    int
    distance_miles:  float
    certifications:  list[str] = Field(default_factory=list)
    availability:    str                           # e.g. "Today 2–5 PM"
    status:          TechnicianStatus = TechnicianStatus.AVAILABLE
    eta_minutes:     Optional[int] = None


class BookingConfirmation(BaseModel):
    """
    Confirmed booking — returned after the agent calls the technician
    and the booking API acknowledges.
    """
    booking_id:         str
    technician:         Technician
    arrival_start:      datetime
    arrival_end:        datetime
    calendar_event_id:  Optional[str] = None
    jira_ticket_key:    Optional[str] = None
    status:             str = "confirmed"
