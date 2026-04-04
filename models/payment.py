"""
models/payment.py
──────────────────
Pydantic models for cost breakdowns and payment results.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, computed_field


class CostBreakdown(BaseModel):
    """
    Itemised repair cost shown to the user before payment confirmation.
    All amounts in USD.
    """
    labor_usd:      float = 95.00
    part_usd:       float = 48.00
    diagnostic_usd: float = 35.00
    currency:       str   = "USD"

    @computed_field
    @property
    def total_usd(self) -> float:
        return round(self.labor_usd + self.part_usd + self.diagnostic_usd, 2)

    @computed_field
    @property
    def total_cents(self) -> int:
        return int(self.total_usd * 100)

    def as_markdown_table(self) -> str:
        return (
            f"| Item | Cost |\n"
            f"|---|---|\n"
            f"| Labor | ${self.labor_usd:.2f} |\n"
            f"| Part | ${self.part_usd:.2f} |\n"
            f"| Diagnostic & travel | ${self.diagnostic_usd:.2f} |\n"
            f"| **Total** | **${self.total_usd:.2f}** |"
        )


class PaymentStatus(str, Enum):
    PENDING   = "pending"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"
    REFUNDED  = "refunded"


class PaymentResult(BaseModel):
    """
    Result of a Stripe PaymentIntent — stored in the DB payments table.
    Returned to the agent after charge() completes.
    """
    payment_intent_id: str
    amount_usd:        float
    currency:          str = "USD"
    status:            PaymentStatus
    stripe_charge_id:  Optional[str] = None
    paid_at:           Optional[datetime] = None
    mfa_verified:      bool = False
    ciba_auth_req_id:  Optional[str] = None
