"""
models/ticket.py
─────────────────
Pydantic models for Jira service tickets and fault diagnosis.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    OPEN        = "Open"
    IN_PROGRESS = "In Progress"
    RESOLVED    = "Resolved"
    CLOSED      = "Closed"


class DiagnosisResult(BaseModel):
    """
    Output of the DiagnosisEngine — identifies the faulty component.

    fault_code:  Samsung / LG error code (e.g. "4E", "5E")
    part_number: OEM part number needed for repair
    confidence:  0.0–1.0 — 1.0 from fault code, lower from symptoms/photo
    photo_url:   If a photo was uploaded, its storage URL
    """
    fault_code:   str
    description:  str
    part_number:  str
    part_name:    str
    confidence:   float = 1.0
    photo_url:    Optional[str] = None


class JiraTicket(BaseModel):
    """
    A Jira service ticket created for a repair job.
    Written once, updated via comments (never overwritten).
    """
    key:         str
    url:         str
    summary:     str
    status:      TicketStatus = TicketStatus.OPEN
    assignee:    Optional[str] = None
    description: Optional[str] = None
    created_at:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
