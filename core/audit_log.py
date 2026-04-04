"""
core/audit_log.py
──────────────────
Immutable structured audit trail.

Every significant action is logged with:
  • timestamp (UTC ISO)
  • session_id
  • user_id (Auth0 sub)
  • actor  ("agent" | "user" | "system")
  • action (e.g. "token_verified", "ticket_created", "payment_charged")
  • details (arbitrary JSON-serializable dict)
  • ip_address (if available)

Backends:
  1. structlog → stdout (always, captured by container logging)
  2. In-memory ring buffer (for /api/audit endpoint, last 1000 events)
  3. (Optional) Async write to DB via audit_flush worker

This provides the auditable log required by CIBA/MFA compliance.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

# Configure structlog for JSON output
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

_structlog = structlog.get_logger("washfix.audit")
_ring: deque = deque(maxlen=1000)  # In-memory ring buffer


class AuditEvent:
    def __init__(
        self,
        action: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        actor: str = "agent",
        details: Optional[dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        self.timestamp  = datetime.now(timezone.utc).isoformat()
        self.action     = action
        self.session_id = session_id
        self.user_id    = user_id
        self.actor      = actor
        self.details    = details or {}
        self.ip_address = ip_address

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp":  self.timestamp,
            "action":     self.action,
            "session_id": self.session_id,
            "user_id":    self.user_id,
            "actor":      self.actor,
            "details":    self.details,
            "ip":         self.ip_address,
        }


class AuditLogger:
    """Central audit logger — write once, never delete."""

    def log(
        self,
        action: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        actor: str = "agent",
        details: Optional[dict[str, Any]] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        event = AuditEvent(
            action     = action,
            session_id = session_id,
            user_id    = user_id,
            actor      = actor,
            details    = details,
            ip_address = ip_address,
        )
        # Structured JSON log to stdout
        _structlog.info(
            action,
            session_id = session_id,
            user_id    = user_id,
            actor      = actor,
            **event.details,
        )
        # Ring buffer for API queries
        _ring.appendleft(event.to_dict())

    # Convenience methods for common events

    def token_verified(self, user_id: str, method: str, session_id: Optional[str] = None) -> None:
        self.log("token_verified", session_id=session_id, user_id=user_id,
                 details={"method": method})

    def rebac_check(self, user_id: str, relation: str, object_ref: str, allowed: bool) -> None:
        self.log("rebac_check", user_id=user_id,
                 details={"relation": relation, "object": object_ref, "allowed": allowed})

    def diagnosis_complete(self, session_id: str, fault_code: str, part_number: str) -> None:
        self.log("diagnosis_complete", session_id=session_id,
                 details={"fault_code": fault_code, "part_number": part_number})

    def photo_analysed(self, session_id: str, user_id: str) -> None:
        self.log("photo_analysed", session_id=session_id, user_id=user_id)

    def technician_selected(self, session_id: str, technician_id: str, technician_name: str) -> None:
        self.log("technician_selected", session_id=session_id,
                 details={"technician_id": technician_id, "technician_name": technician_name})

    def dcr_used(self, client_name: str, audience: str) -> None:
        self.log("dcr_client_used", details={"client_name": client_name, "audience": audience})

    def ciba_initiated(self, session_id: str, user_id: str, scope: str, binding_msg: str) -> None:
        self.log("ciba_initiated", session_id=session_id, user_id=user_id,
                 details={"scope": scope, "binding_message": binding_msg})

    def ciba_granted(self, session_id: str, user_id: str) -> None:
        self.log("ciba_granted", session_id=session_id, user_id=user_id)

    def ciba_denied(self, session_id: str, user_id: str) -> None:
        self.log("ciba_denied", session_id=session_id, user_id=user_id)

    def mfa_sent(self, session_id: str, user_id: str, channel: str) -> None:
        self.log("mfa_otp_sent", session_id=session_id, user_id=user_id,
                 details={"channel": channel})

    def mfa_verified(self, session_id: str, user_id: str) -> None:
        self.log("mfa_otp_verified", session_id=session_id, user_id=user_id)

    def mfa_failed(self, session_id: str, user_id: str) -> None:
        self.log("mfa_otp_failed", session_id=session_id, user_id=user_id)

    def ticket_created(self, session_id: str, ticket_key: str) -> None:
        self.log("ticket_created", session_id=session_id,
                 details={"ticket_key": ticket_key})

    def calendar_blocked(self, session_id: str, event_id: Optional[str]) -> None:
        self.log("calendar_blocked", session_id=session_id,
                 details={"event_id": event_id})

    def payment_charged(self, session_id: str, user_id: str, amount_cents: int, pi_id: str) -> None:
        self.log("payment_charged", session_id=session_id, user_id=user_id,
                 details={"amount_cents": amount_cents, "payment_intent_id": pi_id})

    def token_revoked(self, user_id: str, service: str) -> None:
        self.log("token_revoked", user_id=user_id, details={"service": service})

    def repair_complete(self, session_id: str, ticket_key: str) -> None:
        self.log("repair_complete", session_id=session_id,
                 details={"ticket_key": ticket_key})

    def get_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent audit events from the ring buffer."""
        return list(_ring)[:limit]

    def get_by_session(self, session_id: str) -> list[dict[str, Any]]:
        return [e for e in _ring if e.get("session_id") == session_id]


# Singleton
audit_log = AuditLogger()
