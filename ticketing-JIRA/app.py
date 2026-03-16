import os
import logging
import json
import base64
import httpx

from urllib.parse import quote
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError
from cachetools import TTLCache

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger        = logging.getLogger("mcp.server")
logger_auth   = logging.getLogger("mcp.auth")
logger_jira   = logging.getLogger("mcp.jira")
logger_tools  = logging.getLogger("mcp.tools")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUTH0_DOMAIN        = os.environ.get("AUTH0_DOMAIN",        "")
AUTH0_AUDIENCE      = os.environ.get("AUTH0_AUDIENCE",      "https://mistralai.devailab.work/mcp")
AUTH0_CLIENT_ID     = os.environ.get("AUTH0_CLIENT_ID",     "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
MCP_SERVER_URL      = os.environ.get("MCP_SERVER_URL",      "https://mistralai.devailab.work")
JWKS_URL            = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
ISSUER              = f"https://{AUTH0_DOMAIN}/"
CLAUDE_CALLBACK_URL = "https://claude.ai/api/mcp/auth_callback"

# Jira / Auth0 vault config
AUTH0_MGMT_CLIENT_ID     = os.environ.get("AUTH0_MGMT_CLIENT_ID",     "")
AUTH0_MGMT_CLIENT_SECRET = os.environ.get("AUTH0_MGMT_CLIENT_SECRET", "")
AUTH0_JIRA_USER_ID       = os.environ.get("AUTH0_JIRA_USER_ID",       "oauth2|JIRA-MCP-AUTH0-SOCIAL|70121:-bbbd-4843-b5a1-")
JIRA_CLIENT_ID           = os.environ.get("JIRA_CLIENT_ID",           "")
JIRA_CLIENT_SECRET       = os.environ.get("JIRA_CLIENT_SECRET",       "")
JIRA_CLOUD_ID            = os.environ.get("JIRA_CLOUD_ID",            "")
JIRA_PROJECT_KEY         = os.environ.get("JIRA_PROJECT_KEY",         "PROJ")
JIRA_API                 = f"https://api.atlassian.com/ex/jira/{JIRA_CLOUD_ID}/rest/api/3"

logger.info("=" * 60)
logger.info("MCP SERVER STARTING")
logger.info(f"  AUTH0_DOMAIN       = {AUTH0_DOMAIN}")
logger.info(f"  AUTH0_AUDIENCE     = {AUTH0_AUDIENCE}")
logger.info(f"  MCP_SERVER_URL     = {MCP_SERVER_URL}")
logger.info(f"  JIRA_CLOUD_ID      = {JIRA_CLOUD_ID}")
logger.info(f"  JIRA_PROJECT_KEY   = {JIRA_PROJECT_KEY}")
logger.info(f"  JIRA_API           = {JIRA_API}")
logger.info("=" * 60)

# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------
_jwks_cache: TTLCache = TTLCache(maxsize=1, ttl=600)

async def get_jwks() -> dict:
    if "jwks" in _jwks_cache:
        logger_auth.debug("JWKS served from cache")
        return _jwks_cache["jwks"]
    logger_auth.info(f"Fetching JWKS from {JWKS_URL}")
    async with httpx.AsyncClient() as client:
        resp = await client.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
    jwks = resp.json()
    _jwks_cache["jwks"] = jwks
    logger_auth.info(f"JWKS refreshed — {len(jwks.get('keys', []))} key(s) cached")
    return jwks

# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------
async def verify_token(token: str) -> dict:
    logger_auth.info("Verifying token...")

    # Try JWT first
    try:
        jwks = await get_jwks()
        payload = jwt.decode(
            token, jwks, algorithms=["RS256"],
            audience=AUTH0_AUDIENCE, issuer=ISSUER,
            options={"verify_at_hash": False},
        )
        logger_auth.info(f"Token verified as JWT ✅  sub={payload.get('sub')}  aud={payload.get('aud')}")
        return payload
    except ExpiredSignatureError:
        logger_auth.warning("Token is expired ❌")
        raise ValueError("Token has expired")
    except JWTError as e:
        logger_auth.info(f"Not a valid JWT ({e}) — falling back to /userinfo")
    except Exception as e:
        logger_auth.info(f"JWT verification error ({e}) — falling back to /userinfo")

    # Fall back to /userinfo for opaque tokens
    logger_auth.info(f"Calling /userinfo at https://{AUTH0_DOMAIN}/userinfo")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{AUTH0_DOMAIN}/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    logger_auth.info(f"/userinfo response: HTTP {resp.status_code}")

    if resp.status_code == 401:
        logger_auth.warning("Token rejected by /userinfo ❌")
        raise ValueError("Token rejected by Auth0 /userinfo — invalid or expired")
    if resp.status_code != 200:
        logger_auth.warning(f"/userinfo failed with HTTP {resp.status_code} ❌")
        raise ValueError(f"/userinfo request failed: HTTP {resp.status_code}")

    data = resp.json()
    logger_auth.info(f"Token verified via /userinfo ✅  sub={data.get('sub')}")
    return data

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
UNPROTECTED_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/oauth/register",
    "/health",
    "/debug-token",
}

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in UNPROTECTED_PATHS or request.method == "OPTIONS":
            logger_auth.debug(f"Unprotected path — skipping auth: {path}")
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        logger_auth.info(f">>> {request.method} {path} | Auth: {'Bearer …' + auth_header[-10:] if auth_header else 'NONE'}")

        if not auth_header.startswith("Bearer "):
            logger_auth.warning(f"REJECTED — no Bearer token: {request.method} {path}")
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="MCP", resource="{AUTH0_AUDIENCE}"'},
            )

        token = auth_header.removeprefix("Bearer ").strip()

        # Peek at token structure for debug
        try:
            def _b64decode(s):
                s += "=" * (4 - len(s) % 4)
                return json.loads(base64.urlsafe_b64decode(s))
            parts = token.split(".")
            if len(parts) == 3:
                h = _b64decode(parts[0])
                p = _b64decode(parts[1])
                logger_auth.info(f"TOKEN alg={h.get('alg')} kid={h.get('kid')}")
                logger_auth.info(f"TOKEN iss={p.get('iss')} | aud={p.get('aud')} | sub={p.get('sub')}")
            else:
                logger_auth.info(f"TOKEN is opaque ({len(parts)} segments) — will use /userinfo")
        except Exception as e:
            logger_auth.info(f"Could not peek into token: {e}")

        try:
            claims = await verify_token(token)
            request.state.claims = claims
            logger_auth.info(f"AUTH OK ✅  sub={claims.get('sub')}")
        except ValueError as exc:
            logger_auth.error(f"AUTH REJECTED ❌  {exc}")
            return JSONResponse(
                {"error": "unauthorized", "error_description": str(exc)},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )

        return await call_next(request)

# ---------------------------------------------------------------------------
# Jira token helper
# ---------------------------------------------------------------------------
async def get_jira_access_token() -> str:
    """
    Three-step token chain:
      1. Auth0 client credentials → management token
      2. Auth0 Management API     → refresh token from vault
      3. Atlassian token endpoint → fresh Jira access token
    """
    logger_jira.info("── Jira token chain starting ──")

    async with httpx.AsyncClient(timeout=15) as client:

        # Step 1: Auth0 management token
        logger_jira.info(f"[1/3] Requesting Auth0 management token")
        r = await client.post(f"https://{AUTH0_DOMAIN}/oauth/token", json={
            "grant_type":    "client_credentials",
            "client_id":     AUTH0_MGMT_CLIENT_ID,
            "client_secret": AUTH0_MGMT_CLIENT_SECRET,
            "audience":      f"https://{AUTH0_DOMAIN}/api/v2/",
        })
        logger_jira.info(f"[1/3] Response: HTTP {r.status_code}")
        if r.status_code != 200:
            logger_jira.error(f"[1/3] FAILED — {r.text}")
            r.raise_for_status()
        mgmt_token = r.json()["access_token"]
        logger_jira.info(f"[1/3] Management token obtained ✅")

        # Step 2: Refresh token from Auth0 vault
        encoded_user_id = quote(AUTH0_JIRA_USER_ID, safe="")
        logger_jira.info(f"[2/3] Fetching user from Auth0 vault — user_id={AUTH0_JIRA_USER_ID}")
        r = await client.get(
            f"https://{AUTH0_DOMAIN}/api/v2/users/{encoded_user_id}",
            headers={"Authorization": f"Bearer {mgmt_token}"}
        )
        logger_jira.info(f"[2/3] Response: HTTP {r.status_code}")
        if r.status_code != 200:
            logger_jira.error(f"[2/3] FAILED — {r.text}")
            r.raise_for_status()

        user       = r.json()
        identities = user.get("identities", [])
        logger_jira.info(f"[2/3] Identities found: {[i.get('connection') for i in identities]}")

        jira_identity = next(
            (i for i in identities if
             "atlassian" in i.get("connection", "").lower() or
             "jira"      in i.get("connection", "").lower()),
            identities[0] if identities else None,
        )
        if not jira_identity:
            raise ValueError("No Jira/Atlassian identity found for this user in Auth0.")

        refresh_token = jira_identity.get("refresh_token")
        logger_jira.info(f"[2/3] Refresh token: {'found ✅' if refresh_token else 'MISSING ❌'}  connection={jira_identity.get('connection')}")
        if not refresh_token:
            raise ValueError(
                "No refresh_token in Auth0 vault. "
                "Re-authenticate: Auth0 → Social → JIRA connection → Try (incognito)."
            )

        # Step 3: Exchange for Jira access token
        logger_jira.info("[3/3] Exchanging refresh token with Atlassian")
        r = await client.post("https://auth.atlassian.com/oauth/token", json={
            "grant_type":    "refresh_token",
            "client_id":     JIRA_CLIENT_ID,
            "client_secret": JIRA_CLIENT_SECRET,
            "refresh_token": refresh_token,
        })
        logger_jira.info(f"[3/3] Response: HTTP {r.status_code}")
        if r.status_code != 200:
            logger_jira.error(f"[3/3] FAILED — {r.text}")
            r.raise_for_status()

        access_token = r.json()["access_token"]
        logger_jira.info(f"[3/3] Jira access token obtained ✅")
        logger_jira.info("── Jira token chain complete ──")
        return access_token

# ---------------------------------------------------------------------------
# FastMCP tools
# ---------------------------------------------------------------------------
mcp = FastMCP("Jira MCP Server")

@mcp.tool()
async def list_tickets() -> str:
    """List all Jira tickets in the project. Returns a JSON list with key, summary, status, type, assignee and URL."""
    logger_tools.info(f"TOOL list_tickets — project={JIRA_PROJECT_KEY}")
    try:
        token = await get_jira_access_token()
    except Exception as e:
        logger_tools.error(f"list_tickets — token error: {e}")
        return json.dumps({"error": str(e)})

    url    = f"{JIRA_API}/search/jql"
    params = {
        "jql":        f"project = \"{JIRA_PROJECT_KEY}\" ORDER BY created DESC",
        "maxResults": 50,
        "fields":     "summary,status,issuetype,assignee",
    }
    logger_tools.info(f"list_tickets — GET {url}  jql={params['jql']}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
        )
    logger_tools.info(f"list_tickets — Jira response: HTTP {r.status_code}")
    if r.status_code != 200:
        logger_tools.error(f"list_tickets — error: {r.text}")
        return json.dumps({"error": f"Jira returned HTTP {r.status_code}", "detail": r.text})

    issues = r.json().get("issues", [])
    total  = r.json().get("total", 0)
    logger_tools.info(f"list_tickets — found {total} ticket(s), returning {len(issues)}")
    result = [{
        "key":      i["key"],
        "summary":  i["fields"]["summary"],
        "status":   i["fields"]["status"]["name"],
        "type":     i["fields"]["issuetype"]["name"],
        "assignee": i["fields"]["assignee"]["displayName"] if i["fields"].get("assignee") else "Unassigned",
        "url":      f"https://siegadrien.atlassian.net/browse/{i['key']}",
    } for i in issues]
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_ticket(ticket_key: str) -> str:
    """Get a single Jira ticket by its key (e.g. PROJ-1). Returns summary, status, type, assignee, description and URL."""
    ticket_key = ticket_key.strip().upper()
    logger_tools.info(f"TOOL get_ticket — key={ticket_key}")
    try:
        token = await get_jira_access_token()
    except Exception as e:
        logger_tools.error(f"get_ticket — token error: {e}")
        return json.dumps({"error": str(e)})

    url = f"{JIRA_API}/issue/{ticket_key}"
    logger_tools.info(f"get_ticket — GET {url}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    logger_tools.info(f"get_ticket — Jira response: HTTP {r.status_code}")
    if r.status_code == 404:
        logger_tools.warning(f"get_ticket — {ticket_key} not found")
        return json.dumps({"error": f"Ticket {ticket_key} not found."})
    if r.status_code != 200:
        logger_tools.error(f"get_ticket — error: {r.text}")
        return json.dumps({"error": f"Jira returned HTTP {r.status_code}", "detail": r.text})

    d = r.json()
    f = d["fields"]
    logger_tools.info(f"get_ticket — returning {d['key']}: {f['summary']}")
    return json.dumps({
        "key":         d["key"],
        "summary":     f["summary"],
        "status":      f["status"]["name"],
        "type":        f["issuetype"]["name"],
        "assignee":    f["assignee"]["displayName"] if f.get("assignee") else "Unassigned",
        "description": f.get("description") or "No description",
        "url":         f"https://siegadrien.atlassian.net/browse/{d['key']}",
    }, indent=2)


@mcp.tool()
async def create_ticket(summary: str, description: str = "", issue_type: str = "Task") -> str:
    """Create a new Jira ticket. issue_type can be Task, Bug, or Story. Returns the new ticket key and URL."""
    logger_tools.info(f"TOOL create_ticket — summary={summary!r}  type={issue_type}  project={JIRA_PROJECT_KEY}")
    try:
        token = await get_jira_access_token()
    except Exception as e:
        logger_tools.error(f"create_ticket — token error: {e}")
        return json.dumps({"error": str(e)})

    url = f"{JIRA_API}/issue"
    payload = {
        "fields": {
            "project":     {"key": JIRA_PROJECT_KEY},
            "summary":     summary,
            "issuetype":   {"name": issue_type},
            "description": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": description}
                ]}]
            },
        }
    }
    logger_tools.info(f"create_ticket — POST {url}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/json",
                "Content-Type":  "application/json",
            },
            json=payload,
        )
    logger_tools.info(f"create_ticket — Jira response: HTTP {r.status_code}")
    if r.status_code not in (200, 201):
        logger_tools.error(f"create_ticket — error: {r.text}")
        return json.dumps({"error": f"Jira returned HTTP {r.status_code}", "detail": r.text})

    key        = r.json()["key"]
    url_browse = f"https://siegadrien.atlassian.net/browse/{key}"
    logger_tools.info(f"create_ticket — created ✅  key={key}  url={url_browse}")
    return json.dumps({
        "key":     key,
        "url":     url_browse,
        "message": "Ticket created successfully",
    }, indent=2)

# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------
async def oauth_metadata(request: Request) -> JSONResponse:
    logger.info(f"oauth_metadata requested")
    return JSONResponse({
        "issuer":                                MCP_SERVER_URL,
        "authorization_endpoint":                f"https://{AUTH0_DOMAIN}/authorize",
        "token_endpoint":                        f"https://{AUTH0_DOMAIN}/oauth/token",
        "jwks_uri":                              JWKS_URL,
        "registration_endpoint":                 f"{MCP_SERVER_URL}/oauth/register",
        "response_types_supported":              ["code"],
        "grant_types_supported":                 ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "code_challenge_methods_supported":      ["S256"],
        "scopes_supported":                      ["openid", "offline_access"],
    })

async def protected_resource_metadata(request: Request) -> JSONResponse:
    logger.info(f"protected_resource_metadata requested")
    return JSONResponse({
        "resource":                 AUTH0_AUDIENCE,
        "authorization_servers":    [f"https://{AUTH0_DOMAIN}/"],
        "bearer_methods_supported": ["header"],
        "scopes_supported":         ["openid", "offline_access"],
    })

async def dynamic_client_registration(request: Request) -> JSONResponse:
    # Read raw body first so we can log it before parsing
    raw = await request.body()
    logger.info(f"DCR request received — body length={len(raw)} bytes")
    logger.info(f"DCR raw body: {raw[:500]}")

    # Handle empty or non-JSON body gracefully
    if not raw or not raw.strip():
        logger.warning("DCR received empty body — using empty dict")
        body = {}
    else:
        try:
            body = json.loads(raw)
            logger.info(f"DCR parsed body: {json.dumps(body)}")
        except json.JSONDecodeError as e:
            logger.error(f"DCR body is not valid JSON: {e} — raw={raw[:200]}")
            body = {}

    # Override / set required fields
    body["redirect_uris"] = [CLAUDE_CALLBACK_URL]
    body.setdefault("grant_types",  ["authorization_code", "refresh_token"])
    body.setdefault("token_endpoint_auth_method", "client_secret_post")
    body.setdefault("response_types", ["code"])
    body.setdefault("client_name", "Claude")

    logger.info(f"DCR forwarding to Auth0 — body: {json.dumps(body)}")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{AUTH0_DOMAIN}/oidc/register",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )

    logger.info(f"DCR Auth0 response: HTTP {resp.status_code}")
    logger.info(f"DCR Auth0 body: {resp.text[:500]}")

    if resp.status_code not in (200, 201):
        logger.error(f"DCR FAILED: {resp.status_code} — {resp.text}")
        return JSONResponse(
            {"error": "registration_failed", "detail": resp.text},
            status_code=resp.status_code,
        )

    logger.info("DCR succeeded ✅")
    return JSONResponse(resp.json(), status_code=resp.status_code)

async def health_check(request: Request) -> JSONResponse:
    logger.info("Health check called")
    return JSONResponse({"status": "ok", "server": "Jira MCP Server"})

async def debug_token(request: Request) -> JSONResponse:
    """TEMPORARY — remove after debugging."""
    auth  = request.headers.get("Authorization", "NONE")
    token = auth.removeprefix("Bearer ").strip()
    logger.info(f"debug_token called — token length={len(token)}")
    result = {
        "raw_token_length": len(token),
        "raw_token_head":   token[:40],
        "raw_token_tail":   token[-20:],
    }
    try:
        parts = token.split(".")
        result["segment_count"] = len(parts)
        if len(parts) == 3:
            def dec(s):
                s += "=" * (4 - len(s) % 4)
                return json.loads(base64.urlsafe_b64decode(s))
            result["header"]  = dec(parts[0])
            result["payload"] = dec(parts[1])
        else:
            result["note"] = "NOT a JWT — opaque token, will use /userinfo"
    except Exception as e:
        result["decode_error"] = str(e)
    return JSONResponse(result)

# ---------------------------------------------------------------------------
# Assemble ASGI app
# ---------------------------------------------------------------------------
mcp_asgi = mcp.http_app(path="/mcp", stateless_http=True)

app = Starlette(
    lifespan=mcp_asgi.router.lifespan_context,
    routes=[
        Route("/.well-known/oauth-authorization-server",   oauth_metadata,              methods=["GET"]),
        Route("/.well-known/openid-configuration",         oauth_metadata,              methods=["GET"]),
        Route("/.well-known/oauth-protected-resource",     protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata, methods=["GET"]),
        Route("/oauth/register",                           dynamic_client_registration, methods=["POST"]),
        Route("/health",                                   health_check,                methods=["GET"]),
        Route("/debug-token",                              debug_token,                 methods=["GET", "POST"]),
        Mount("/", app=mcp_asgi),
    ]
)

app.add_middleware(BearerAuthMiddleware)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting Jira MCP server on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
