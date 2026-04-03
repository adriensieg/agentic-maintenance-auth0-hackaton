# A multi-modal tech AI Agent

A multimodal AI agent that detects and **troubleshoots home issues on your behalf**, **escalates to maintenance**, **books service appointments**, **make any payment** and **generates repair tickets** — seamlessly **integrated** with your **existing favorite AI Assistant** such as ChatGPT, Claude or Mistral AI Le Chat. 

- Here is the demo of the multimodal AI agent: **https://www.youtube.com/watch?v=Kjp29AaKjts**
- You can try it out here: **https://techie.devailab.work/mcp** but you must (**1**) connect to your AI assistant (ChatGPT, Claude, or Mistral AI Le Chat) and (**2**) be authenticated.
  
To make it connect to your existing AI Assitant - you can follow this tutorial: **https://www.youtube.com/watch?v=qwtwGqpXluE&feature=youtu.be**

**No more apps**. **All from the AI assistant you already use**: ChatGPT, Claude or Le Chat.

The main security question is then how do we enable **AI assistants** (such as *Open AI ChatGPT*, *Mistral AI Le Chat* or *Anthropic Claude*) to execute end‑to‑end actions **on behalf of users** — in **real time** and **transparently** — while **preserving identity**, **consent**, and **trust** across **multiple providers**?

Who **owns the transaction** when ChatGPT (or others) becomes the **interface** and **every app becomes a backend** — and how do we monetize that securely?

This is **not** a **UX convenience story** - it's a **multi-party authorization problem**: connecting these 3 systems into a single seamless user action — "repair my Washing machine" — requires solving an **identity chain** that does not exist out of the box. The chain breaks in 3 specific places:

# The vision: 
You wake up. Coffee. Breakfast. You load the washing machine and press Start. Nothing happens.
The situation:
- You’re renting a condo.
- Your washing machine breaks.

You **don’t want** to:
- download another app
- chase your landlord
- call repair companies
- pay first and deal with admin later

You open your favorite AI Assistant such as - ChatGPT, Claude or Mistral AI and explains the situation. 

A **multimodal AI agent** instantly analyzes the issue — it *detects the problem*, *diagnoses the issue*, and creates a *repair ticket automatically*.
It can **call the technician on your behalf**, explain the problem, share diagnostics, and **book the service appointment**.
You don’t troubleshoot it. You don’t call anyone.
Your AI agent handles it — **seamlessly**, **silently**. 
It can even **approve payments** and track the repair.

**No apps**. **No phone calls**. **No hassle**.  **No switching**, **No friction**.
- **ChatGPT** becomes the **interface**.
- Any **3rd parties API or application** become the **backends**.
- The **AI agent** **orchestrates** everything.

Within seconds it:
- Creates a repair ticket
- Contacts maintenance via phone
- Books the earliest appointment
- Sends diagnostics to the technician
- Processes the payment if needed

# What has it been developed for this hackaton?

1. The user says the washing machine is broken.
2. The AI loads only their machine data using **ReBAC** (**Relationship-Based Access Control**).
3. It checks the **user identity** with **Auth0 authentication**.
4. The AI asks questions to understand the problem.
5. The user uploads a photo; access is verified using **token-based permissions**.
6. The AI identifies the faulty part.
7. It contacts external technician APIs using a **secure Token Vault**.
8. The AI fetches available technicians, secured with **dynamic client credentials**.
9. The user selects a technician.
10. The AI confirms the selection using **DCR** (**Dynamic Client Registration**).
11. A repair appointment is booked through a **client-initiated backchannel authentication (CIBA)** flow.
12. A service ticket is created in the system with **auditable logs**.
13. The AI updates the user’s calendar with **scoped access tokens**.
14. It checks if the machine is under warranty using secure **3rd-party API tokens**.
15. The AI shows the repair cost to the user.
16. The user approves **payment via CIBA backchannel flow**.
17. A **one-time code** is sent via **MFA/SMS** for verification.
18. The user enters the code to **confirm payment**.
19. The AI verifies the **CIBA token with Auth0**.
20. Payment is processed securely using **tokenized payment credentials**.
21. All actions are logged for **auditing** and **traceability**.
22. Access tokens expire after use to prevent leaks (**token lifecycle management**).
23. The AI confirms the repair is **booked** and **paid**.

Full-stack Python implementation of the agentic AI demo with Auth0-backed security,
Jira ticketing, CIBA payment flows, MFA/SMS verification, and Twilio voice calls.

### Architecture Overview
```
washfix/
├── auth/               # All Auth0 / identity flows
│   ├── auth0_client.py     — Management API client, token exchange
│   ├── ciba.py             — CIBA backchannel authentication
│   ├── dcr.py              — Dynamic Client Registration
│   ├── mfa.py              — MFA / OTP / SMS dispatch
│   ├── rebac.py            — Relationship-Based Access Control
│   ├── token_vault.py      — Secure third-party token storage
│   └── middleware.py       — JWT / Bearer middleware (FastAPI)
│
├── services/           # External integrations
│   ├── jira_service.py     — Jira ticket creation & updates
│   ├── technician_service.py — Technician lookup & availability
│   ├── calendar_service.py — Google Calendar scoped-token updates
│   ├── warranty_service.py — Samsung warranty registry check
│   ├── payment_service.py  — Stripe Connect tokenized payment
│   ├── sms_service.py      — Twilio SMS one-time codes
│   └── voice_service.py    — Twilio outbound voice calls + WebSocket
│
├── core/               # Business logic / orchestration
│   ├── agent.py            — Main AI agent loop (Claude / Gemini)
│   ├── diagnosis.py        — Fault code analysis & part lookup
│   ├── audit_log.py        — Immutable audit trail (structlog)
│   └── session.py          — Per-user session state management
│
├── api/                # FastAPI routers
│   ├── chat.py             — /api/chat  (WebSocket + REST)
│   ├── photo.py            — /api/photo (upload + vision analysis)
│   ├── booking.py          — /api/booking (appointment + calendar)
│   ├── payment.py          — /api/payment (CIBA + MFA + Stripe)
│   └── webhooks.py         — /api/webhooks (Twilio, Auth0 events)
│
├── models/             # Pydantic data models
│   ├── session.py
│   ├── technician.py
│   ├── ticket.py
│   └── payment.py
│
├── workers/            # Background / async workers
│   ├── token_refresh.py    — Proactive token rotation
│   └── audit_flush.py      — Periodic log flush to cold storage
│
├── utils/
│   ├── crypto.py           — AES-256 encryption helpers
│   └── http.py             — Retry-aware httpx client factory
│
├── config/
│   └── settings.py         — Pydantic-Settings (env vars, secrets)
│
├── templates/          — Jinja2 HTML for the demo UI
│   └── index.html
│
├── main.py             — FastAPI app entry point
├── .env.example        — All required env vars
└── requirements.txt
```
## Auth0 Flows Implemented

| Flow | Location | Purpose |
|------|----------|---------|
| **JWT / JWKS verification** | `auth/middleware.py` | Verifies every API request using signed JWTs and JWKS validation |
| **ReBAC (FGA)** | `auth/rebac.py` | Ensures each user can only load appliance data they are authorized to access |
| **Token Vault** | `auth/token_vault.py` | Securely stores and retrieves third-party refresh/access tokens |
| **DCR (Dynamic Client Registration)** | `auth/dcr.py` | Registers ephemeral OAuth clients for partner APIs |
| **CIBA** | `auth/ciba.py` | Handles out-of-band payment and booking authorization |
| **MFA / OTP** | `auth/mfa.py` | Sends SMS one-time passcodes for payment confirmation |
| **Scoped Access Tokens** | `services/calendar_service.py` | Requests minimal Google Calendar permissions based on least-privilege access |
| **Token Lifecycle / Expiry** | `auth/token_vault.py` | Invalidates tokens after single use or expiration |

## Flow Details

### 1. JWT / JWKS Verification
**File:** `auth/middleware.py`

Used to validate incoming bearer tokens on every protected API request.

**Responsibilities:**
- Extract JWT from the `Authorization` header
- Verify token signature using JWKS
- Validate issuer, audience, and expiration
- Reject unauthorized or malformed requests

**Why it matters:**  
This is the first line of defense for API security and ensures only trusted, signed tokens are accepted.


### 2. ReBAC (Fine-Grained Authorization)
**File:** `auth/rebac.py`

Implements relationship-based access control (ReBAC), likely backed by FGA (Fine-Grained Authorization).

**Responsibilities:**
- Determine whether a user can access a specific appliance or resource
- Enforce per-user data boundaries
- Prevent cross-account or cross-tenant data leakage

**Why it matters:**  
Authentication proves *who* the user is; ReBAC determines *what* they are allowed to access.

### 3. Token Vault
**File:** `auth/token_vault.py`

Provides secure storage and retrieval of third-party OAuth tokens.

**Responsibilities:**
- Store refresh/access tokens for external providers
- Retrieve tokens when calling partner APIs
- Support secure token rotation and lifecycle enforcement

**Why it matters:**  
Centralizing token handling reduces accidental leakage and makes external API integrations safer and easier to manage.

### 4. Dynamic Client Registration (DCR)
**File:** `auth/dcr.py`

Registers temporary or per-session OAuth clients for partner systems.

**Responsibilities:**
- Dynamically register clients with partner APIs
- Support ephemeral client credentials
- Reduce manual client provisioning overhead

**Why it matters:**  
Useful when integrating with APIs that require dynamic onboarding or per-tenant/per-session OAuth registration.

### 5. CIBA (Client-Initiated Backchannel Authentication)
**File:** `auth/ciba.py`

Implements out-of-band authorization flows for sensitive actions like payments or bookings.

**Responsibilities:**
- Initiate a backchannel authentication request
- Trigger user approval on a separate trusted device or channel
- Poll or receive confirmation once the user approves

**Why it matters:**  
CIBA is ideal when the user should confirm a high-risk action outside the current browser or app session.

### 6. MFA / OTP
**File:** `auth/mfa.py`

Adds a second authentication factor using SMS one-time passcodes.

**Responsibilities:**
- Generate one-time passcodes
- Deliver OTPs via SMS
- Validate submitted codes during confirmation flows

**Primary use case:**
- Payment confirmation
- Step-up authentication for sensitive actions

**Why it matters:**  
Provides an extra layer of user verification for high-risk transactions.

### 7. Scoped Access Tokens
**File:** `services/calendar_service.py`

Uses OAuth access tokens with the smallest required permission set.

**Responsibilities:**
- Request only the necessary Google Calendar scopes
- Minimize exposure to unnecessary user data
- Follow least-privilege access principles

**Why it matters:**  
Restricting scopes improves user trust and reduces blast radius if a token is compromised.

### 8. Token Lifecycle / Expiry
**File:** `auth/token_vault.py`

Enforces expiration and one-time-use rules for sensitive tokens.

**Responsibilities:**
- Invalidate tokens after use
- Prevent replay attacks
- Enforce TTL / expiry windows
- Support revocation and cleanup

**Why it matters:**  
Short-lived and single-use tokens significantly reduce risk in payment, booking, and delegated access flows.

# Bonus Blog Post - Agentic AI That Actually Gets Work Done

Most AI today is still trapped in the chat window. It can answer, summarize, and recommend — but it rarely **completes** real-world tasks. We’re building the next layer: **AI that can securely take action across fragmented systems and deliver outcomes, not just conversation**.

In this demo, a user reports a broken washing machine. From that single prompt, the AI verifies identity through Auth0, retrieves only the user’s authorized appliance data using fine-grained access controls, asks diagnostic questions, analyzes an uploaded image, identifies the likely failed component, finds an available certified technician, books the repair, creates a service ticket, updates the user’s calendar, checks warranty status, and securely completes payment authorization.

What looks like a simple consumer experience is actually a **trust and orchestration platform** underneath.

The system is built with **ReBAC authorization, secure token vaulting, dynamic client registration, CIBA backchannel authentication, MFA confirmation, scoped access tokens, auditable workflows, and strict token lifecycle controls**. This gives AI the ability to act across third-party APIs and transactional systems **without compromising identity, permissions, or user trust**.

The market opportunity extends far beyond appliance repair. The same architecture can power **home services, healthcare, travel, insurance, financial operations, and enterprise support** — any category where users want problems solved, not just explained.

The real opportunity isn’t another chatbot.  
It’s building the **trusted execution layer for the agent economy**.

We’re not just making AI more helpful.  
We’re making it **operational**.
