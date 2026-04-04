"""
services/warranty_service.py
─────────────────────────────
Check appliance warranty status via Samsung's warranty registry API.

The API call is secured with a DCR-minted ephemeral OAuth client:
  1. Register a short-lived client scoped to `read:warranty`.
  2. Get a token using client-credentials grant.
  3. Call the Samsung warranty API.
  4. Delete the ephemeral client.

Falls back to a local heuristic (2-year standard warranty) when the
external API is unreachable or DCR is not configured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("washfix.services.warranty")

# Illustrative — replace with the real Samsung developer API endpoint
SAMSUNG_WARRANTY_API = "https://api.samsung.com/warranty/v1/status"


class WarrantyService:
    """
    Warranty status check with DCR-secured external API call.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    async def check(
        self,
        model: str,
        serial: Optional[str] = None,
        purchase_date: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """
        Check the warranty status of an appliance.

        Returns::
            {
                "in_warranty": bool,
                "expiry_date": "YYYY-MM-DD",
                "coverage":    "Samsung Standard Limited Warranty" | "Expired",
                "source":      "api" | "heuristic"
            }
        """
        # Attempt DCR-secured API call
        try:
            from auth.dcr import dcr_client
            token = await dcr_client.use_once(
                client_name = "washfix-warranty-check",
                scopes      = ["read:warranty"],
                audience    = SAMSUNG_WARRANTY_API,
            )
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    SAMSUNG_WARRANTY_API,
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "model":  model,
                        "serial": serial or "",
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"Warranty API response for {model}: {data}")
                data["source"] = "api"
                return data

            logger.warning(
                f"Warranty API returned HTTP {resp.status_code} — using heuristic."
            )
        except Exception as exc:
            logger.warning(f"Warranty API unavailable ({exc}) — falling back to heuristic.")

        # Local heuristic: Samsung standard 2-year limited warranty
        return self._heuristic(model, purchase_date)

    def _heuristic(
        self,
        model: str,
        purchase_date: Optional[datetime],
    ) -> dict[str, Any]:
        """
        Estimate warranty status from the purchase date.
        Samsung standard warranty: 2 years (parts + labor).
        """
        if not purchase_date:
            return {
                "in_warranty": False,
                "expiry_date": "Unknown",
                "coverage":    "Unknown — no purchase date provided",
                "source":      "heuristic",
            }

        warranty_years = 2
        expiry = purchase_date + timedelta(days=365 * warranty_years)
        now = datetime.now(timezone.utc)

        # Ensure both are timezone-aware for comparison
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        in_warranty = now < expiry
        logger.info(
            f"Warranty heuristic: model={model} "
            f"purchase={purchase_date.date()} "
            f"expiry={expiry.date()} in_warranty={in_warranty}"
        )

        return {
            "in_warranty": in_warranty,
            "expiry_date": expiry.strftime("%Y-%m-%d"),
            "coverage":    "Samsung Standard Limited Warranty (2 yr)" if in_warranty else "Expired",
            "source":      "heuristic",
        }

    def build_cost_message(self, warranty_result: dict[str, Any]) -> str:
        """
        Return a human-readable warranty status message for the agent.
        """
        if warranty_result.get("in_warranty"):
            return (
                f"✅ **Under warranty** (expires {warranty_result.get('expiry_date', 'N/A')}).\n"
                f"Parts and labor are covered under {warranty_result.get('coverage', 'warranty')}."
            )
        return (
            f"⚠️ **Out of warranty** — expired {warranty_result.get('expiry_date', 'N/A')}.\n"
            "Standard service fees apply."
        )


# Singleton
warranty_service = WarrantyService()
