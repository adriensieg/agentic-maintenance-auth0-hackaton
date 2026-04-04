"""
models/session.py
──────────────────
Pydantic models for the per-user conversation session.

UserSession is the central state object passed between agent phases.
It is serialized to JSON and stored in Redis (with DB fallback).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class SessionPhase(str, Enum):
    """
    State machine phases for the repair workflow.

    INIT         → load appliance via ReBAC, ask diagnostic questions
    DIAGNOSIS    → extract fault codes, run diagnosis engine
    PHOTO        → request/process appliance photo
    TECH_SELECT  → present technicians, await user choice
    BOOKING      → confirm booking, create calendar event + Jira ticket
    TICKET       → ticket created, calendar blocked
    WARRANTY     → check warranty status
    COST         → present cost breakdown, await approval
    PAYMENT_CIBA → CIBA backchannel auth initiated
    PAYMENT_MFA  → MFA/OTP sent, awaiting user code
    COMPLETE     → all done, audit trail closed
    """
    INIT         = "init"
    DIAGNOSIS    = "diagnosis"
    PHOTO        = "photo"
    TECH_SELECT  = "tech_select"
    BOOKING      = "booking"
    TICKET       = "ticket"
    WARRANTY     = "warranty"
    COST         = "cost"
    PAYMENT_CIBA = "payment_ciba"
    PAYMENT_MFA  = "payment_mfa"
    COMPLETE     = "complete"


class ApplianceInfo(BaseModel):
    """Appliance registry entry — loaded via ReBAC-gated DB query."""

    model:        str
    serial:       Optional[str] = None
    unit:         str
    address:      str
    installed_at: Optional[datetime] = None
    warranty_end: Optional[datetime] = None


class UserSession(BaseModel):
    """
    Full in-flight session state.

    Sensitive fields (OTP codes) are stored in `metadata` but NEVER
    returned to the client via the GET /api/chat/session endpoint.
    """
    session_id:  str
    user_id:     str                      # Auth0 `sub`
    user_name:   str
    user_phone:  Optional[str] = None

    phase:       SessionPhase = SessionPhase.INIT
    appliance:   Optional[ApplianceInfo] = None

    # Diagnosis is imported here to avoid circular imports
    # The actual DiagnosisResult model lives in models/ticket.py
    diagnosis:   Optional[Any] = None

    # Conversation history (last N messages for LLM context)
    messages:    list[dict[str, Any]] = Field(default_factory=list)

    # Arbitrary key-value store for phase-specific data
    # e.g. pending_otp, ticket_key, chosen_tech, ciba_auth_req_id
    metadata:    dict[str, Any] = Field(default_factory=dict)

    created_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
