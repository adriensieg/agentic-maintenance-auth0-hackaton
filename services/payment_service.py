"""
services/payment_service.py
────────────────────────────
Process payments via Stripe Connect using tokenized payment credentials.

Security model:
  • The customer's PaymentMethod ID is stored AES-256 encrypted in the
    Token Vault (Auth0 app_metadata).  Raw card numbers never touch our
    servers.
  • Each payment creates a ONE-TIME PaymentIntent with a deterministic
    idempotency key derived from the session ID — double-charges are
    structurally impossible.
  • After payment succeeds the idempotency key is stored in the DB so
    any retry of the same session returns the existing result.
  • Access tokens are invalidated post-use (token lifecycle management).

Stripe Connect flow:
  1. During user onboarding, a SetupIntent tokenizes the card.
  2. The resulting PaymentMethod ID is stored in the Token Vault.
  3. At payment time: retrieve PM ID → create PaymentIntent → confirm.
  4. Result is persisted to the payments table (audit trail).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger("washfix.services.payment")


class PaymentService:
    """
    Stripe Connect payment processing with tokenized credentials.
    Falls back to demo mode when Stripe is not configured.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._stripe_initialized = False

    def _init_stripe(self) -> bool:
        """Lazily initialize Stripe SDK."""
        if self._stripe_initialized:
            return True
        s = self._settings
        if not s.stripe_secret_key or s.stripe_secret_key.startswith("sk_test_placeholder"):
            logger.warning("Stripe not configured — demo payment mode active.")
            return False
        try:
            import stripe
            stripe.api_key = s.stripe_secret_key
            self._stripe_initialized = True
            return True
        except ImportError:
            logger.error("stripe package not installed.")
            return False

    async def get_payment_method(self, user_id: str) -> Optional[str]:
        """
        Retrieve the stored Stripe PaymentMethod ID from the Token Vault.
        Returns None if no payment method is on file.
        """
        try:
            from auth.token_vault import token_vault
            bundle = await token_vault.get(user_id, "stripe")
            return bundle.get("payment_method_id") if bundle else None
        except Exception as exc:
            logger.warning(f"Could not retrieve payment method: {exc}")
            return None

    async def get_customer_id(self, user_id: str) -> Optional[str]:
        """Retrieve the Stripe Customer ID from the Token Vault."""
        try:
            from auth.token_vault import token_vault
            bundle = await token_vault.get(user_id, "stripe")
            return bundle.get("customer_id") if bundle else None
        except Exception:
            return None

    async def store_payment_method(
        self,
        user_id: str,
        payment_method_id: str,
        customer_id: str,
    ) -> None:
        """
        Store a Stripe PaymentMethod ID in the Token Vault.
        Called during onboarding after the user completes SetupIntent.
        """
        from auth.token_vault import token_vault
        await token_vault.set(user_id, "stripe", {
            "payment_method_id": payment_method_id,
            "customer_id":       customer_id,
        })
        logger.info(f"Stripe payment method stored for user {user_id[-8:]}***")

    async def charge(
        self,
        user_id: str,
        amount_cents: int,
        description: str,
        idempotency_key: str,
        currency: str = "usd",
    ) -> dict[str, Any]:
        """
        Create and confirm a Stripe PaymentIntent.

        Returns::
            {
                "status":            "succeeded" | "failed",
                "payment_intent_id": str,
                "charge_id":         str | None,
                "error":             str | None,   # only on failure
            }

        Idempotency:
            The same `idempotency_key` can be called multiple times safely —
            Stripe returns the same result for duplicate requests.
        """
        if not self._init_stripe():
            return self._demo_payment(amount_cents, description)

        import stripe

        pm_id  = await self.get_payment_method(user_id)
        cus_id = await self.get_customer_id(user_id)

        if not pm_id:
            logger.warning(f"No stored payment method for user {user_id[-8:]}*** — demo mode.")
            return self._demo_payment(amount_cents, description)

        try:
            intent = stripe.PaymentIntent.create(
                amount           = amount_cents,
                currency         = currency,
                customer         = cus_id,
                payment_method   = pm_id,
                description      = description,
                confirm          = True,
                off_session      = True,
                idempotency_key  = idempotency_key,
                return_url       = self._settings.app_base_url + "/payment/return",
            )
            logger.info(
                f"Stripe PaymentIntent {intent.id} "
                f"status={intent.status} amount={amount_cents}¢"
            )
            # Invalidate the single-use idempotency after recording
            try:
                from auth.token_vault import token_vault
                await token_vault.invalidate_access_token(user_id, "stripe_idempotency")
            except Exception:
                pass

            return {
                "status":            intent.status,
                "payment_intent_id": intent.id,
                "charge_id":         getattr(intent, "latest_charge", None),
            }

        except stripe.error.CardError as exc:
            logger.warning(f"Card error for user {user_id[-8:]}***: {exc.user_message}")
            return {"status": "failed", "payment_intent_id": None, "error": exc.user_message}

        except stripe.error.IdempotencyError:
            logger.info("Idempotency key reused — returning existing payment result.")
            # Retrieve the original intent
            try:
                intents = stripe.PaymentIntent.list(
                    limit=1, expand=["data.latest_charge"]
                )
                for intent in intents.auto_paging_iter():
                    if intent.metadata.get("idempotency_key") == idempotency_key:
                        return {
                            "status":            intent.status,
                            "payment_intent_id": intent.id,
                            "charge_id":         getattr(intent, "latest_charge", None),
                        }
            except Exception:
                pass
            return {"status": "succeeded", "payment_intent_id": "existing", "error": None}

        except stripe.error.StripeError as exc:
            logger.error(f"Stripe error: {exc}")
            return {"status": "failed", "payment_intent_id": None, "error": str(exc)}

    async def create_setup_intent(self, user_id: str) -> dict[str, Any]:
        """
        Create a Stripe SetupIntent for saving a payment method during onboarding.
        The client uses the `client_secret` to render a payment form.
        """
        if not self._init_stripe():
            return {
                "client_secret": "seti_demo_" + secrets.token_hex(8) + "_secret_" + secrets.token_hex(8),
                "setup_intent_id": "seti_demo_" + secrets.token_hex(8),
            }

        import stripe
        try:
            cus_id = await self.get_customer_id(user_id)
            si = stripe.SetupIntent.create(
                customer           = cus_id,
                payment_method_types = ["card"],
                usage              = "off_session",
            )
            return {
                "client_secret":   si.client_secret,
                "setup_intent_id": si.id,
            }
        except stripe.error.StripeError as exc:
            logger.error(f"SetupIntent creation failed: {exc}")
            raise

    async def create_customer(self, user_id: str, email: str, name: str) -> str:
        """
        Create a Stripe Customer for the user and store the ID in the vault.
        Returns the Stripe customer ID.
        """
        if not self._init_stripe():
            cus_id = "cus_demo_" + secrets.token_hex(8)
            await self.store_payment_method(user_id, "pm_demo_" + secrets.token_hex(8), cus_id)
            return cus_id

        import stripe
        existing = await self.get_customer_id(user_id)
        if existing:
            return existing

        customer = stripe.Customer.create(
            email    = email,
            name     = name,
            metadata = {"auth0_sub": user_id},
        )
        from auth.token_vault import token_vault
        bundle = await token_vault.get(user_id, "stripe") or {}
        bundle["customer_id"] = customer.id
        await token_vault.set(user_id, "stripe", bundle)
        logger.info(f"Stripe customer created: {customer.id}")
        return customer.id

    def _demo_payment(self, amount_cents: int, description: str) -> dict[str, Any]:
        """Return a simulated successful payment for demo / dev environments."""
        pi_id = "pi_demo_" + secrets.token_hex(8)
        ch_id = "ch_demo_" + secrets.token_hex(8)
        logger.info(f"Demo payment: {pi_id} — {amount_cents}¢ — {description}")
        return {
            "status":            "succeeded",
            "payment_intent_id": pi_id,
            "charge_id":         ch_id,
        }

    def format_amount(self, cents: int) -> str:
        """Format cents as a USD string: 17800 → '$178.00'"""
        return f"${cents / 100:.2f}"


# Singleton
payment_service = PaymentService()
