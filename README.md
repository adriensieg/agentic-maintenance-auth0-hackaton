# A multi-modal tech AI Agent

A multimodal AI agent that detects and **troubleshoots home issues on your behalf**, **escalates to maintenance**, **books service appointments**, **make any payment** and **generates repair tickets** — seamlessly **integrated** with your **existing favorite AI Assistant** such as ChatGPT, Claude or Mistral AI Le Chat. NO more apps. 

How do we enable **AI assistants** (such as *Open AI ChatGPT*, *Mistral AI Le Chat* or *Anthropic Claude*) to execute end‑to‑end actions **on behalf of users** — in **real time** and **transparently** — while **preserving identity**, **consent**, and **trust** across **multiple providers**?

Who **owns the transaction** when ChatGPT (or others) becomes the **interface** and **every app becomes a backend** — and how do we monetize that securely?

This is **not** a **UX convenience story** - it's a **multi-party authorization problem**: connecting these 3 systems into a single seamless user action — "repair my Washing machine" — requires solving an **identity chain** that does not exist out of the box. The chain breaks in 3 specific places:

Break 1 — **ChatGPT is authenticated** - via DCR and OAuth 2.1 Authorization Code Flow with PKCE - but **the user is not**.
Break 2 — Our MCP server has **no standing** with **other 3rd party applications** - such as ServiceNow and 3rd party APIs.
Break 3 — A **financial transaction** requires explicit **user confirmation**

# The vision: 
You wake up. Coffee. Breakfast. You load the washing machine and press Start. Nothing happens.
You open your favorite AI Assistant such as - ChatGPT, Claude or Mistral AI and explains the situation. 

A **multimodal AI agent** instantly analyzes the issue — it *detects the problem*, *diagnoses the issue*, and creates a *repair ticket automatically*.
It can **call the technician on your behalf**, explain the problem, share diagnostics, and **book the service appointment**.
You don’t troubleshoot it. You don’t call anyone.
Your AI agent handles it — **seamlessly**, **silently**. 
It can even **approve payments** and track the repair.
**All from the AI assistant you already use: ChatGPT, Claude or Le Chat**

**No apps**. **No phone calls**. **No hassle**.

Within seconds it:
- Creates a repair ticket
- Contacts maintenance via phone
- Books the earliest appointment
- Sends diagnostics to the technician
- Processes the payment if needed

- **ChatGPT** becomes the **interface**.
- Any **3rd parties API or application** become the **backends**.
- The **AI agent** **orchestrates** everything.

**No apps**, **No switching**, **No friction**.

# The Core Problem We're Solving

# The solution
You can try the Maintenance Agent out here - https://mistralai.devailab.work/mcp.
 
Auth0 is the security backbone of our multimodal AI agent, allowing assistants like ChatGPT, Claude, and Le Chat to safely act on behalf of the user.

1. When the assistant connects, it automatically registers through **Auth0 DCR** and authenticates using the **OAuth 2.1 Authorization Code Flow with PKCE**.
2. Auth0 then issues a **signed JWT access token**, representing the **authenticated user session**.
3. Our MCP server verifies every request using **Auth0’s JWKS**, enforcing **scopes** and **permissions** before any tool can run.
4. **Refresh tokens** keep the session secure and continuous while the AI agent works in the background.
5. To link the **assistant session to the real user**, Auth0 performs **OAuth Token Exchange** (**RFC 8693**) to generate a **user-identity-bearing token**.
6. This allows the AI agent to securely *create repair tickets*, *contact technicians*, and *book appointment** through our **OAuth-protected MCP tools**.
7. When external services are needed, **Auth0’s Token Vault** stores and automatically refreshes **per-user third-party OAuth tokens**, retrieved only on demand.
8. For high-risk actions like *booking maintenance* or *charging a payment method*, we trigger **CIBA** (Client-Initiated Backchannel Authentication) to send a secure push approval to the user’s phone.
9. With **Auth0 managing identity**, **delegated authorization**, **token lifecycle**, and **out-of-band transaction confirmation**, the AI agent can safely complete end-to-end home repair workflows directly from the user’s AI assistant.

# What has it been developed for this hackaton? 

### Solution 1 — ChatGPT is authenticated with any AI Assistant

The **AI assistant SDK** - known as **connectors** lets developers bring their **own products** directly into AI Assistant interface with **custom Ul components**, **API access**, and **user context** that can **persist** across chats. It's built on Model Context Protocol (**MCP**), which defines how ChatGPT communicates with our app through **tools**, **resources**, and **structured data**.

OpenAI chatgpt integrates with our **OAuth-protected MCP** server by performing **resource** and **authorization server discovery**, **dynamic client registration** (**DCR**), and a **PKCE-based authorization code flow** with **Auth0** to obtain a **JWT access token**, which our server verifies via **JWKS public keys** before allowing **SSE-based MCP tool execution** and seamless **token refresh** for ongoing secure access.

- Here is the explanation: https://youtu.be/qwtwGqpXluE

# The Limits of Today, The Blueprint for Tomorrow

#### OpenAI (or any AI assistant) does not expose user identity through the MCP layer.
**RFC 8693 Token Exchange** works only if Auth0 can resolve the incoming ChatGPT token to a known user. 
Currently, **OpenAI does not pass verifiable user identity claims through the MCP connection**. 
We can work around this — but it requires either **OpenAI adding OIDC support**, or **a separate user-linking** step during onboarding that correlates the **ChatGPT session** to our **internal user record**. Doable, but not clean.

- The **OAuth flow** authenticates **OpenAI chatgpt** (**the client**) to our **MCP service** (**the resource provider**). 
- It does **NOT** **authenticate** or **identify the individual human** (OpenAI chatgpt's user) to us.
- We **won’t receive any user identity info** unless OpenAI chatgpt explictly passes it.
- OAuth by itself **does not identify a user**; it just **delegates authorization**.

In traditional web apps, we often combine **OAuth + OpenID Connect (OIDC)** to both **authenticate** and **authorize users**.
In the OpenAI chatgpt SDK integration, **only OAuth 2.1 is used** — **not OIDC.** So there’s **no user identity payload** (**no ID token**, **no claims** about the user).

#### Most of 3rd parties API access requires business approval.
External 3rd parties API are **not publicly open**. 
Vendors must **explicitly grant our application access to perform actions on behalf of users**. This is a commercial and legal dependency — not a technical one. Without it, Boundary 2 cannot go to production regardless of how well everything else is built.
