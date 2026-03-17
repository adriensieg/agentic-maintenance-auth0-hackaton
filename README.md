# A multi-modal tech AI Agent

A multimodal AI agent that detects and troubleshoots home issues, escalates to maintenance, books service appointments, make any payment and generates repair tickets —seamlessly integrated with your favorite AI Assistant such as ChatGPT, Claude or Mistral AI Le Chat. 

It's a **multi-party authorization problem** on how to raise a ticket in a 3rd party system, contact a maintenair and pay inside a 3rd party conversational AI interface.

How do we enable **AI assistants** (such as *Open AI ChatGPT*, *Mistral AI Le Chat* or *Anthropic Claude*)to execute end‑to‑end actions **on behalf of users** — in **real time** and **transparently** — while **preserving identity**, **consent**, and **trust** across **multiple providers**?

# The vision: 
You wake up. Coffee. Breakfast. You load the washing machine and press Start. Nothing happens.
You open your favorite AI Assistant such as - ChatGPT, Claude or Mistral AI and explains the situation. 

A **multimodal AI agent** instantly analyzes the issue — it *detects the problem*, *diagnoses the issue*, and creates a *repair ticket automatically*.
It can **call the technician on your behalf**, explain the problem, share diagnostics, and **book the service appointment**.
You don’t troubleshoot it. You don’t call anyone.
Your AI agent handles it.
It can even **approve payments** and track the repair.
**All from the AI assistant you already use: ChatGPT, Claude or Le Chat**

**No apps**. **No phone calls**. **No hassle**.

Within seconds it:
- Creates a repair ticket
- Contacts maintenance via phone
- Books the earliest appointment
- Sends diagnostics to the technician
- Processes the payment if needed


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
