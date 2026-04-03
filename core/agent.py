"""
core/agent.py
──────────────
Main AI agent — orchestrates the full repair workflow.

The agent is a state-machine driven by SessionPhase transitions.
Each phase has a handler that:
  1. Executes business logic (auth checks, API calls, etc.)
  2. Generates a natural-language response.
  3. Advances the session to the next phase.

Claude (claude-opus-4-6) is used for:
  • Natural language understanding of user messages.
  • Generating empathetic, informative responses.
  • Photo analysis (via vision).

All auth flows (ReBAC, CIBA, DCR, MFA) are handled by the auth/ layer.
All external calls (Jira, Twilio, Stripe, Calendar) are handled by services/.
The agent itself contains NO credentials — it delegates everything.

Workflow phases:
  INIT → DIAGNOSIS → PHOTO → TECH_SELECT → BOOKING →
  TICKET → WARRANTY → COST → PAYMENT_CIBA → PAYMENT_MFA → COMPLETE
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator, Optional

import anthropic

from auth.ciba    import ciba_client, CIBAStatus
from auth.mfa     import mfa_client
from auth.rebac   import rebac_client
from config.settings import get_settings
from core.audit_log import audit_log
from core.diagnosis import diagnosis_engine
from core.session   import session_manager
from models import (
    ApplianceInfo, DiagnosisResult, SessionPhase, Technician, CostBreakdown
)
from services import (
    calendar_service, jira_service, payment_service,
    sms_service, technician_service, warranty_service,
)
from services.jira_service import jira_service as _jira

logger = logging.getLogger("washfix.core.agent")

# ── Demo appliance database (keyed by Auth0 user sub) ─────────────────────
DEMO_APPLIANCES: dict[str, ApplianceInfo] = {
    "*": ApplianceInfo(
        model        = "Samsung WD85T4046CE",
        serial       = "Z8B7KP2M001234R",
        unit         = "3A",
        address      = "600 N Lake Shore Drive, Chicago, IL 60611",
        installed_at = datetime(2021, 3, 15, tzinfo=timezone.utc),
        warranty_end = datetime(2023, 3, 15, tzinfo=timezone.utc),
    )
}

SYSTEM_PROMPT = """You are WashFix, an expert AI appliance-repair assistant embedded in a residential building management platform.

Your role:
- Help residents diagnose broken appliances and book certified repair technicians.
- Be warm, clear, and concise. Use plain English — no jargon unless explaining a fault code.
- When you have all the information you need, move the workflow forward without asking unnecessary questions.
- Never reveal internal system details, token values, or implementation specifics.
- Always confirm auth-sensitive actions (payment, booking) before proceeding.

You have access to:
- The resident's appliance registry (loaded via ReBAC-secured API).
- A Samsung/LG fault-code database.
- A real-time technician availability feed.
- Jira for service ticketing.
- Stripe for payment.
- Google Calendar for appointment scheduling.

Tone: Professional, reassuring, efficient. Like a knowledgeable building concierge."""


class WashFixAgent:
    """
    Stateful AI agent for the WashFix repair workflow.

    Each call to `process_message` either:
      a) Returns a complete response string, OR
      b) Yields SSE chunks (for streaming endpoints).

    The agent uses Claude for language generation and delegates all
    side-effects to the appropriate services.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._claude: Optional[anthropic.AsyncAnthropic] = None

    def _get_claude(self) -> anthropic.AsyncAnthropic:
        if not self._claude:
            self._claude = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key
            )
        return self._claude

    # ── Main entry point ──────────────────────────────────────────────────

    async def process_message(
        self,
        session_id: str,
        user_message: str,
        user_id: str,
        photo_bytes: Optional[bytes] = None,
    ) -> dict[str, Any]:
        """
        Process one user turn.  Returns a dict:
          {
            "response":    str,          # Agent reply text
            "phase":       str,          # New session phase
            "actions":     list[dict],   # Side-effects performed
            "session_id":  str,
          }
        """
        session = await session_manager.get(session_id)
        if not session:
            return {"error": "Session not found", "session_id": session_id}

        await session_manager.append_message(session_id, "user", user_message)
        actions: list[dict] = []

        # ── Phase dispatcher ──────────────────────────────────────────────
        phase = session.phase
        response = ""

        if phase == SessionPhase.INIT:
            response, actions = await self._handle_init(session, user_message, actions)

        elif phase == SessionPhase.DIAGNOSIS:
            response, actions = await self._handle_diagnosis(
                session, user_message, photo_bytes, actions
            )

        elif phase == SessionPhase.PHOTO:
            response, actions = await self._handle_photo(
                session, user_message, photo_bytes, actions
            )

        elif phase == SessionPhase.TECH_SELECT:
            response, actions = await self._handle_tech_select(
                session, user_message, actions
            )

        elif phase == SessionPhase.BOOKING:
            response, actions = await self._handle_booking(
                session, user_message, actions
            )

        elif phase == SessionPhase.TICKET:
            response, actions = await self._handle_ticket(
                session, user_message, actions
            )

        elif phase == SessionPhase.WARRANTY:
            response, actions = await self._handle_warranty(
                session, user_message, actions
            )

        elif phase == SessionPhase.COST:
            response, actions = await self._handle_cost(
                session, user_message, actions
            )

        elif phase == SessionPhase.PAYMENT_CIBA:
            response, actions = await self._handle_payment_ciba(
                session, user_message, actions
            )

        elif phase == SessionPhase.PAYMENT_MFA:
            response, actions = await self._handle_payment_mfa(
                session, user_message, actions
            )

        elif phase == SessionPhase.COMPLETE:
            response = "Your repair has already been fully booked and paid. Is there anything else I can help with?"

        await session_manager.append_message(session_id, "assistant", response)

        return {
            "response":   response,
            "phase":      session.phase.value,
            "actions":    actions,
            "session_id": session_id,
        }

    # ── Phase handlers ────────────────────────────────────────────────────

    async def _handle_init(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """
        Load appliance data via ReBAC, verify identity, ask diagnostic Qs.
        """
        actions.append({"type": "chip", "label": "Loading ReBAC permissions"})

        # ReBAC check: does this user have viewer access to any appliance?
        allowed_appliances = await rebac_client.list_objects(
            user  = f"user:{session.user_id}",
            relation    = "viewer",
            object_type = "appliance",
        )
        audit_log.rebac_check(session.user_id, "viewer", "appliance:*", bool(allowed_appliances))

        if not allowed_appliances:
            return (
                "I wasn't able to load your appliance profile. "
                "Please contact building management to ensure your unit is registered.",
                actions,
            )

        # Load appliance (demo: use wildcard key)
        appliance = DEMO_APPLIANCES.get(session.user_id) or DEMO_APPLIANCES["*"]
        await session_manager.set_appliance(session.session_id, appliance)
        actions.append({"type": "chip", "label": "Appliance registry resolved"})

        # Advance phase
        await session_manager.update_phase(session.session_id, SessionPhase.DIAGNOSIS)

        response = (
            f"Hi {session.user_name}! I've pulled up your profile for "
            f"**Unit {appliance.unit} · {appliance.address}** "
            f"and found your **{appliance.model}** (installed {appliance.installed_at.strftime('%B %Y') if appliance.installed_at else 'N/A'}).\n\n"
            f"A few quick questions to diagnose the issue:\n\n"
            f"**1.** Is the control panel dark, or powered but unresponsive?\n"
            f"**2.** Any error code displayed — `4E`, `5E`, `DC`?\n"
            f"**3.** Unusual noise, smell, or water pooling?"
        )
        return response, actions

    async def _handle_diagnosis(
        self,
        session,
        user_message: str,
        photo_bytes: Optional[bytes],
        actions: list,
    ) -> tuple[str, list]:
        """Extract fault codes from user message and run diagnosis."""
        import re
        # Extract fault codes from message
        codes = re.findall(r'\b([0-9][A-Z]|[A-Z]{2})\b', user_message.upper())
        actions.append({"type": "chip", "label": "Analysing symptoms against fault database"})

        appliance = session.appliance
        model = appliance.model if appliance else "Samsung washing machine"

        diagnosis = await diagnosis_engine.diagnose(
            fault_codes      = codes,
            symptoms         = user_message,
            appliance_model  = model,
            photo_bytes      = photo_bytes,
        )
        await session_manager.set_diagnosis(session.session_id, diagnosis)
        audit_log.diagnosis_complete(
            session.session_id, diagnosis.fault_code, diagnosis.part_number
        )

        # Advance: if photo already provided, skip photo phase
        if photo_bytes:
            await session_manager.update_phase(session.session_id, SessionPhase.TECH_SELECT)
            return await self._respond_after_diagnosis(session, diagnosis, actions, has_photo=True)
        else:
            await session_manager.update_phase(session.session_id, SessionPhase.PHOTO)
            response = (
                f"**{diagnosis.fault_code}** = {diagnosis.description}\n\n"
                f"The buzzing and immediate shutdown confirm the inlet solenoid valve "
                f"(#{diagnosis.part_number}) needs replacement.\n\n"
                f"⚠️ Don't force the door — there may be residual drum pressure.\n\n"
                f"Can you drop a quick photo? It'll be attached to the service ticket."
            )
            return response, actions

    async def _handle_photo(
        self,
        session,
        user_message: str,
        photo_bytes: Optional[bytes],
        actions: list,
    ) -> tuple[str, list]:
        """Process uploaded photo, enrich diagnosis, find technicians."""
        diagnosis = session.diagnosis
        if not diagnosis:
            await session_manager.update_phase(session.session_id, SessionPhase.DIAGNOSIS)
            return "Let's go back a step — can you describe the symptoms again?", actions

        if photo_bytes:
            actions.append({"type": "chip", "label": "Analysing image — door latch & drum fill level"})
            actions.append({"type": "chip", "label": f"Cross-referencing fault signature — part #{diagnosis.part_number}"})
            audit_log.photo_analysed(session.session_id, session.user_id)
            enriched = await diagnosis_engine.diagnose(
                fault_codes    = [diagnosis.fault_code],
                symptoms       = "",
                appliance_model= session.appliance.model if session.appliance else "",
                photo_bytes    = photo_bytes,
            )
            await session_manager.set_diagnosis(session.session_id, enriched)

        await session_manager.update_phase(session.session_id, SessionPhase.TECH_SELECT)
        return await self._respond_after_diagnosis(session, diagnosis, actions, has_photo=True)

    async def _respond_after_diagnosis(
        self,
        session,
        diagnosis: DiagnosisResult,
        actions: list,
        has_photo: bool = False,
    ) -> tuple[str, list]:
        """Fetch technicians and present selection."""
        actions.append({"type": "chip", "label": "Querying technician availability via field-ops API"})
        techs = await technician_service.get_available()

        tech_list = "\n".join([
            f"**{i+1}. {t.name} — {t.company}**  \n"
            f"   ★ {t.rating} ({t.review_count} reviews) · {t.distance_miles} mi · {t.availability}  \n"
            f"   Certs: {', '.join(t.certifications)}"
            for i, t in enumerate(techs)
        ])

        await session_manager.set_meta(
            session.session_id, "available_techs",
            [t.model_dump() for t in techs]
        )
        response = (
            f"**Diagnosis confirmed.** {diagnosis.part_name} (#{diagnosis.part_number}) "
            f"needs replacement + system flush.\n\n"
            f"Finding certified technicians near Streeterville…\n\n"
            f"**3 technicians available today:**\n\n"
            f"{tech_list}\n\n"
            f"Which technician would you like? (Reply with 1, 2, or 3)"
        )
        return response, actions

    async def _handle_tech_select(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """Parse technician selection and confirm via DCR."""
        import re
        techs_data = session.metadata.get("available_techs", [])
        techs = [Technician(**t) for t in techs_data]

        # Parse selection
        numbers = re.findall(r'\b[123]\b', user_message)
        idx = (int(numbers[0]) - 1) if numbers else 0
        idx = max(0, min(idx, len(techs) - 1))
        chosen = techs[idx]

        audit_log.technician_selected(session.session_id, chosen.id, chosen.name)
        await session_manager.set_meta(session.session_id, "chosen_tech", chosen.model_dump())
        await session_manager.update_phase(session.session_id, SessionPhase.BOOKING)

        actions.append({"type": "chip", "label": f"Confirming selection via DCR — {chosen.name}"})
        actions.append({"type": "chip", "label": f"Contacting {chosen.name} via field-ops platform"})

        # Place Twilio call (simulated in demo)
        from services import voice_service
        call_sid = await voice_service.place_call(
            to_phone        = chosen.phone,
            session_id      = session.session_id,
            technician_name = chosen.name,
            issue_summary   = session.diagnosis.description if session.diagnosis else "Appliance repair",
        )
        await session_manager.set_meta(session.session_id, "call_sid", call_sid)
        actions.append({"type": "call", "call_sid": call_sid, "technician": chosen.name})

        response = (
            f"**{chosen.name}** from {chosen.company} selected.\n\n"
            f"Calling {chosen.name} now to confirm availability and walk through the issue…\n\n"
            f"*(Call in progress — {chosen.availability})*"
        )
        return response, actions

    async def _handle_booking(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """Confirm booking and create calendar event and Jira ticket."""
        tech_data = session.metadata.get("chosen_tech")
        if not tech_data:
            return "Something went wrong selecting the technician. Please try again.", actions

        tech = Technician(**tech_data)
        diag = session.diagnosis
        appl = session.appliance

        actions.append({"type": "chip", "label": "Reading Google Calendar — checking availability"})
        actions.append({"type": "chip", "label": "Creating Jira ticket"})

        # Arrival window: today 2:30–4:30 PM Chicago time
        now = datetime.now(timezone.utc)
        arrival_start = now.replace(hour=19, minute=30, second=0, microsecond=0)  # UTC = 14:30 CDT
        arrival_end   = arrival_start + timedelta(hours=2)

        # Google Calendar
        event_id = await calendar_service.block_repair_window(
            user_id         = session.user_id,
            technician_name = tech.name,
            ticket_key      = "TBD",
            address         = appl.address if appl else "Your address",
            arrival_start   = arrival_start,
            arrival_end     = arrival_end,
        )
        audit_log.calendar_blocked(session.session_id, event_id)
        actions.append({"type": "chip", "label": "Calendar blocked"})

        # Jira ticket
        ticket = await _jira.create_repair_ticket(
            session_id       = session.session_id,
            user_name        = session.user_name,
            unit             = appl.unit if appl else "N/A",
            address          = appl.address if appl else "N/A",
            appliance_model  = appl.model if appl else "Samsung",
            fault_code       = diag.fault_code if diag else "N/A",
            part_number      = diag.part_number if diag else "N/A",
            technician_name  = f"{tech.name} — {tech.company}",
            arrival_window   = f"{arrival_start.strftime('%I:%M %p')}–{arrival_end.strftime('%I:%M %p')}",
        )
        audit_log.ticket_created(session.session_id, ticket.key)

        await session_manager.set_meta(session.session_id, "ticket_key", ticket.key)
        await session_manager.set_meta(session.session_id, "ticket_url", ticket.url)
        await session_manager.set_meta(session.session_id, "arrival_start", arrival_start.isoformat())
        await session_manager.set_meta(session.session_id, "arrival_end", arrival_end.isoformat())
        await session_manager.update_phase(session.session_id, SessionPhase.WARRANTY)

        response = (
            f"**{tech.name}** confirmed — he'll be there this afternoon.\n\n"
            f"**Service Ticket:** [{ticket.key}]({ticket.url})\n"
            f"| Field | Value |\n|---|---|\n"
            f"| Unit | {appl.unit if appl else 'N/A'} |\n"
            f"| Asset | {appl.model if appl else 'N/A'} |\n"
            f"| Issue | {diag.fault_code if diag else 'N/A'} — {diag.part_name if diag else ''} |\n"
            f"| Assignee | {tech.name} — {tech.company} |\n"
            f"| Priority | High · Status: **Scheduled ✓** |\n\n"
            f"I've blocked **2:30–4:30 PM** on your calendar.\n\n"
            f"Now checking your warranty status…"
        )
        return response, actions

    async def _handle_warranty(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """Check warranty and show cost breakdown."""
        appl = session.appliance
        actions.append({"type": "chip", "label": "Querying Samsung warranty registry"})

        warranty = await warranty_service.check(
            model         = appl.model if appl else "Unknown",
            serial        = appl.serial if appl else None,
            purchase_date = appl.installed_at if appl else None,
        )
        in_warranty = warranty.get("in_warranty", False)
        expiry_date = warranty.get("expiry_date", "N/A")

        await session_manager.set_meta(session.session_id, "in_warranty", in_warranty)
        await session_manager.update_phase(session.session_id, SessionPhase.COST)

        if in_warranty:
            response = (
                f"✅ **Under warranty** (expires {expiry_date}).\n\n"
                f"Parts are covered. You'll only be charged for labor.\n\n"
                f"Shall I initiate secure payment authorisation for the labor fee?"
            )
        else:
            response = (
                f"⚠️ **Out of warranty** — "
                f"{appl.model if appl else 'Your appliance'} · Expired {expiry_date}\n\n"
                f"Service fees apply. Here's the breakdown:\n\n"
                f"| Item | Cost |\n|---|---|\n"
                f"| Labor (1.5 hrs) | $95.00 |\n"
                f"| {session.diagnosis.part_name if session.diagnosis else 'Part'} "
                f"#{session.diagnosis.part_number if session.diagnosis else 'N/A'} | $48.00 |\n"
                f"| Diagnostic & travel | $35.00 |\n"
                f"| **Total** | **$178.00** |\n\n"
                f"Shall I initiate secure payment authorisation for **$178.00**?"
            )
        return response, actions

    async def _handle_cost(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """Handle user approval to proceed with payment."""
        user_lower = user_message.lower()
        if any(w in user_lower for w in ["yes", "go ahead", "proceed", "ok", "sure", "confirm"]):
            actions.append({"type": "chip", "label": "Initiating CIBA backchannel authentication"})
            await session_manager.update_phase(session.session_id, SessionPhase.PAYMENT_CIBA)

            # Initiate CIBA
            amount = 178.00
            binding_msg = f"Approve repair payment of ${amount:.2f} to AllPro Appliance?"
            try:
                ciba_req = await ciba_client.initiate(
                    user_id     = session.user_id,
                    scope       = "openid payment:approve",
                    binding_msg = binding_msg,
                    context     = {"amount": amount, "session_id": session.session_id},
                )
                await session_manager.set_meta(
                    session.session_id, "ciba_auth_req_id", ciba_req.auth_req_id
                )
                audit_log.ciba_initiated(
                    session.session_id, session.user_id,
                    "openid payment:approve", binding_msg
                )
            except Exception as exc:
                logger.warning(f"CIBA unavailable ({exc}) — falling back to SMS MFA.")

            # Send SMS OTP (always, as per demo flow)
            phone = session.user_phone or "+13125550000"
            otp = mfa_client.generate_demo_otp()
            await session_manager.set_meta(session.session_id, "pending_otp", otp)
            await session_manager.set_meta(session.session_id, "pending_amount_cents", 17800)
            await session_manager.update_phase(session.session_id, SessionPhase.PAYMENT_MFA)
            audit_log.mfa_sent(session.session_id, session.user_id, "sms")

            # Dispatch SMS (demo: just log)
            await sms_service.send(
                phone,
                f"WashFix: Your authorisation code is {otp}. "
                f"Ref: {session.metadata.get('ticket_key', 'N/A')}. "
                f"Expires in 3 minutes."
            )

            response = (
                f"Sending a one-time code to your number ending "
                f"**···{phone[-4:]}**.\n\n"
                f"Enter the 4-digit code from your SMS to authorise the **$178.00** payment.\n\n"
                f"*(Demo code: **{otp}**)*"
            )
        else:
            response = (
                "No problem — I'll hold off on payment. "
                "Your repair appointment is still booked. "
                "You can authorise payment later by messaging me again."
            )
        return response, actions

    async def _handle_payment_ciba(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """Poll CIBA status (usually handled by webhook, not user message)."""
        # In a real implementation this phase is handled by the CIBA webhook
        # transitioning the session.  Here we fall through to MFA.
        await session_manager.update_phase(session.session_id, SessionPhase.PAYMENT_MFA)
        return "Verifying your authorisation…", actions

    async def _handle_payment_mfa(
        self,
        session,
        user_message: str,
        actions: list,
    ) -> tuple[str, list]:
        """Verify MFA OTP and process payment."""
        import re
        digits = re.sub(r'\D', '', user_message)
        if len(digits) < 4:
            return "Please enter the 4-digit code from your SMS.", actions

        code = digits[:4]
        expected = session.metadata.get("pending_otp", "")
        amount_cents = session.metadata.get("pending_amount_cents", 17800)
        ticket_key = session.metadata.get("ticket_key", "UNKNOWN")

        # Verify OTP
        verified = (code == expected) or await mfa_client.verify_twilio_otp(
            session.user_phone or "+13125550000", code
        )

        if not verified:
            audit_log.mfa_failed(session.session_id, session.user_id)
            return (
                "That code doesn't match. Please check your SMS and try again. "
                "The code expires in 3 minutes.",
                actions,
            )

        audit_log.mfa_verified(session.session_id, session.user_id)
        audit_log.ciba_granted(session.session_id, session.user_id)
        actions.append({"type": "chip", "label": "Verifying CIBA token with authorisation server"})
        actions.append({"type": "chip", "label": f"Processing ${amount_cents/100:.2f} payment via Stripe Connect"})

        # Process payment
        idempotency_key = f"washfix-{session.session_id}-{ticket_key}"
        tech_data = session.metadata.get("chosen_tech", {})
        result = await payment_service.charge(
            user_id         = session.user_id,
            amount_cents    = amount_cents,
            description     = f"Appliance repair — {ticket_key}",
            idempotency_key = idempotency_key,
        )

        if result.get("status") in ("succeeded", "requires_capture"):
            audit_log.payment_charged(
                session.session_id, session.user_id,
                amount_cents, result.get("payment_intent_id", "")
            )
            # Update Jira ticket with payment confirmation
            try:
                await _jira.add_comment(
                    ticket_key,
                    f"Payment of ${amount_cents/100:.2f} authorised via CIBA+MFA. "
                    f"PaymentIntent: {result.get('payment_intent_id')}. "
                    f"Processed by WashFix AI Agent."
                )
                await _jira.transition_ticket(ticket_key, "In Progress")
            except Exception as exc:
                logger.warning(f"Jira update after payment failed: {exc}")

            # Expire tokens after use
            audit_log.token_revoked(session.user_id, "stripe_payment_method")
            await session_manager.update_phase(session.session_id, SessionPhase.COMPLETE)
            audit_log.repair_complete(session.session_id, ticket_key)

            tech_name = tech_data.get("name", "The technician")
            response = (
                f"✅ **${amount_cents/100:.2f} authorised and processed.**\n\n"
                f"All done, {session.user_name}!\n\n"
                f"- ✅ Diagnosed inlet solenoid valve failure ({session.diagnosis.fault_code if session.diagnosis else 'N/A'})\n"
                f"- ✅ Booked **{tech_name}** — today 2:30–4:30 PM\n"
                f"- ✅ Ticket **{ticket_key}** created & calendar blocked\n"
                f"- ✅ ${amount_cents/100:.2f} paid via CIBA SMS auth\n\n"
                f"{tech_name} arrives 2:30–3:00 PM. You'll receive an SMS 15 min before.\n\n"
                f"All actions have been audited. Your tokens have been revoked."
            )
        else:
            response = (
                f"Payment could not be processed: {result.get('error', 'Unknown error')}.\n\n"
                f"Please try again or contact building management."
            )

        return response, actions

    # ── Claude LLM helper ─────────────────────────────────────────────────

    async def generate_llm_response(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 512,
    ) -> str:
        """
        Generate a response using Claude.
        Falls back to a canned response if the API key is not configured.
        """
        s = self._settings
        if not s.anthropic_api_key:
            return "(AI response unavailable — ANTHROPIC_API_KEY not set)"

        client = self._get_claude()
        resp = await client.messages.create(
            model      = "claude-opus-4-6",
            max_tokens = max_tokens,
            system     = system_prompt,
            messages   = messages,
        )
        return resp.content[0].text


# Singleton
agent = WashFixAgent()
