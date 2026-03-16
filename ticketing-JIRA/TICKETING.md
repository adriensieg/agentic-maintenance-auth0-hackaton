# Jira MCP Server — Full Setup & Runtime Flow

---

## Part 1 — One-time setup (you do this once, ever)

### Step 1 — Register your app with Atlassian
- Go to **developer.atlassian.com/console** → create an OAuth 2.0 (3LO) app
- Set the callback URL to your Auth0 domain: `https://dev-rcc43qlv8opam0co.us.auth0.com/login/callback`
- Add scopes: `read:jira-work` `write:jira-work` `read:jira-user` `read:me` `offline_access`
- Atlassian gives you:
  - `JIRA_CLIENT_ID` — your app's public name with Atlassian
  - `JIRA_CLIENT_SECRET` — your app's private password with Atlassian

> **`offline_access` is critical.** Without it Atlassian won't issue a refresh token and you'll need to re-login every hour.

---

### Step 2 — Create an Auth0 account and application
- Go to **auth0.com** → create a Regular Web Application
- Auth0 gives you:
  - `AUTH0_CLIENT_ID` — your app's identity inside Auth0
  - `AUTH0_CLIENT_SECRET` — used by your MCP server to talk to Auth0
  - `AUTH0_DOMAIN` — e.g. `dev-rcc43qlv8opam0co.us.auth0.com`

> These are completely separate from the Jira credentials. Auth0 credentials = your server's badge to talk to Auth0. Jira credentials = your app's badge to talk to Atlassian.

---

### Step 3 — Add Jira as a social connection inside Auth0
- Auth0 → Connections → Social → Custom OAuth2 → configure with:
  - Authorization URL: `https://auth.atlassian.com/authorize?audience=api.atlassian.com`
  - Token URL: `https://auth.atlassian.com/oauth/token`
  - Client ID: your `JIRA_CLIENT_ID`
  - Client Secret: your `JIRA_CLIENT_SECRET`
  - Scopes: `read:jira-work write:jira-work read:jira-user read:me offline_access`
  - Fetch User Profile Script: maps `account_id` → `user_id`, calls `https://api.atlassian.com/me`

> This teaches Auth0 how to redirect users to Atlassian's consent screen and how to exchange codes for tokens.

---

### Step 4 — Grant your Auth0 app access to the Management API
- Auth0 → Applications → APIs → Auth0 Management API → Machine to Machine Applications
- Authorize your app with scopes: `read:users` `read:user_idp_tokens`

> Without this your MCP server cannot ask Auth0 "give me the stored Jira token for this user." It will get a 403 Forbidden.

---

## Part 2 — One-time user login (done once per user)

### Step 5 — The user logs in via Auth0 and consents to Jira
- Auth0 → your social connection → click **Try** (or your app's login button in production)
- You are redirected to Atlassian's login page → click **Allow**
- Atlassian issues two tokens and sends them back to Auth0:
  - `access_token` — valid 1 hour — the actual key to call Jira
  - `refresh_token` — valid ~90 days — used to get new access tokens silently
- Both are stored in Auth0's token vault, attached to your user record

> Auth0 creates a user record: `oauth2|JIRA-MCP-AUTH0-SOCIAL|70121:69931a8d-bbbd-4843-b5a1-f7e0d0536a16` — this is just Auth0's internal ID for "the person who connected Jira." It's you.

---

### Step 6 — Find your Auth0 user ID
- Auth0 → User Management → Users → click your user → copy the **user_id** field
- `AUTH0_USER_ID` = `oauth2|JIRA-MCP-AUTH0-SOCIAL|70121:69931a8d-bbbd-4843-b5a1-f7e0d0536a16`

> Your MCP server needs this ID to ask Auth0 "give me the Jira tokens for this specific user."

---

### Step 7 — Note your Jira cloud ID and project key
- `CLOUD_ID` = `425cec19-43d0-48f8-94d9-333b351363c0` — stored in your Auth0 app_metadata
- `JIRA_PROJECT_KEY` = `PROJ` — the short key in your Jira project URL, not the full name "Auth0-hackaton"

---

## Part 3 — Every tool call in production (fully automatic)

### Step 8 — Claude calls a tool
- User says "create a Jira ticket for the login bug"
- Claude calls `create_ticket` on your MCP server
- Your MCP server wakes up and starts the token chain below

---

### Step 9 — MCP server gets an Auth0 management token
- MCP server sends `AUTH0_CLIENT_ID` + `AUTH0_CLIENT_SECRET` to Auth0
- Auth0 returns a short-lived management token (~24h)
- This proves to Auth0 that the request is coming from your legitimate server

---

### Step 10 — MCP server retrieves the Jira refresh token from the vault
- MCP server calls Auth0 Management API with the management token + `AUTH0_USER_ID`
- Auth0 returns the user record including the `refresh_token` stored in `identities[0].refresh_token`

> The refresh token is long-lived (~90 days). It is NOT the key to Jira — it is the key to GET a key to Jira.

---

### Step 11 — MCP server exchanges the refresh token for a Jira access token
- MCP server posts to `https://auth.atlassian.com/oauth/token` with:
  - `JIRA_CLIENT_ID` + `JIRA_CLIENT_SECRET` + `refresh_token`
- Atlassian returns:
  - A fresh `access_token` valid for 1 hour
  - A brand new `refresh_token` (the old one is now dead — Atlassian always rotates it)

> **The new refresh token must be saved back to Auth0 vault**, otherwise the next call will fail with "refresh_token is invalid."

---

### Step 12 — MCP server calls the Jira API
- MCP server calls:
  `https://api.atlassian.com/ex/jira/425cec19-43d0-48f8-94d9-333b351363c0/rest/api/3/issue`
- Uses the `access_token` in the Authorization header
- Jira creates the ticket and returns `PROJ-42`

> **Never call** `yourworkspace.atlassian.net/rest/api/3` with an OAuth token — that URL only works with basic auth (email + API token). Always use `api.atlassian.com/ex/jira/{cloudId}` for OAuth.

---

### Step 13 — When does the user need to re-authenticate?

Almost never. The only cases:

- User manually revokes access at `id.atlassian.com → connected apps`
- Refresh token chain expires (~90 days of complete inactivity)
- You rotate the `JIRA_CLIENT_SECRET` in the Atlassian developer console

Normal access token expiry (every hour) is handled automatically by steps 9–11. The user never sees it.

---

## Token cheat sheet

| Token | Issued by | Lives in | Valid for | Purpose |
|---|---|---|---|---|
| `JIRA_CLIENT_ID` | Atlassian | Your code | Forever | Identifies your app to Atlassian |
| `JIRA_CLIENT_SECRET` | Atlassian | Your code | Until rotated | Authenticates your app to Atlassian |
| `AUTH0_CLIENT_ID` | Auth0 | Your code | Forever | Identifies your server to Auth0 |
| `AUTH0_CLIENT_SECRET` | Auth0 | Your code | Until rotated | Authenticates your server to Auth0 |
| `management token` | Auth0 | In-memory | ~24h | Lets your server read from Auth0 vault |
| `refresh_token` | Atlassian | Auth0 vault | ~90 days | Gets new access tokens. Rotates on every use |
| `access_token` | Atlassian | In-memory | 1 hour | The actual key to call Jira API |
