import os
import logging
import json
import base64
import httpx

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUTH0_DOMAIN        = os.environ.get("AUTH0_DOMAIN",        "dev-rcc43qlv8opam0co.us.auth0.com")
AUTH0_AUDIENCE      = os.environ.get("AUTH0_AUDIENCE",      "https://mistralai.devailab.work/mcp")
AUTH0_CLIENT_ID     = os.environ.get("AUTH0_CLIENT_ID",     "xxx")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
MCP_SERVER_URL      = os.environ.get("MCP_SERVER_URL",      "https://mistralai.devailab.work")
MISTRAL_REDIRECT    = "https://callback.mistral.ai/v1/integrations_auth/oauth2_callback"
JWKS_URL            = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
ISSUER              = f"https://{AUTH0_DOMAIN}/"

# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------
_jwks_cache: TTLCache = TTLCache(maxsize=1, ttl=600)

async def get_jwks() -> dict:
    if "jwks" in _jwks_cache:
        return _jwks_cache["jwks"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
    jwks = resp.json()
    _jwks_cache["jwks"] = jwks
    logger.info("JWKS refreshed from Auth0")
    return jwks

# ---------------------------------------------------------------------------
# Token verification — accepts BOTH JWT and opaque tokens
# ---------------------------------------------------------------------------
async def verify_token(token: str) -> dict:
    """
    Try JWT verification first (fast, no network call).
    Fall back to Auth0 /userinfo for opaque tokens.
    This handles whatever token type Le Chat sends.
    """
    # ── 1. Try JWT verification ──────────────────────────────────────────────
    try:
        jwks = await get_jwks()
        payload = jwt.decode(
            token, jwks, algorithms=["RS256"],
            audience=AUTH0_AUDIENCE, issuer=ISSUER,
            options={"verify_at_hash": False},
        )
        logger.info("Token verified as JWT ✅")
        return payload
    except ExpiredSignatureError:
        raise ValueError("Token has expired")
    except JWTError as e:
        logger.info(f"Not a valid JWT ({e}), trying /userinfo...")
    except Exception as e:
        logger.info(f"JWT verification failed ({e}), trying /userinfo...")

    # ── 2. Fall back to Auth0 /userinfo for opaque tokens ────────────────────
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{AUTH0_DOMAIN}/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    logger.info(f"/userinfo response: HTTP {resp.status_code}")

    if resp.status_code == 401:
        raise ValueError("Token rejected by Auth0 /userinfo — invalid or expired")

    if resp.status_code != 200:
        raise ValueError(f"/userinfo request failed: HTTP {resp.status_code}")

    data = resp.json()
    logger.info(f"Token verified via /userinfo ✅ sub={data.get('sub')}")
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
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        logger.info(f">>> {request.method} {path} | "
                    f"Auth: {'Bearer …' + auth_header[-10:] if auth_header else 'NONE'}")

        if not auth_header.startswith("Bearer "):
            logger.warning(f"REJECTED (no token): {request.method} {path}")
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
                logger.info(f"TOKEN alg={h.get('alg')} kid={h.get('kid')}")
                logger.info(f"TOKEN iss={p.get('iss')} | aud={p.get('aud')} | sub={p.get('sub')}")
            else:
                logger.info(f"TOKEN appears opaque ({len(parts)} segments) — will use /userinfo")
        except Exception as e:
            logger.info(f"Could not peek into token: {e} — will use /userinfo")

        try:
            claims = await verify_token(token)
            request.state.claims = claims
            logger.info(f"AUTH OK ✅ sub={claims.get('sub')}")
        except ValueError as exc:
            logger.error(f"REJECTED ❌ {exc}")
            return JSONResponse(
                {"error": "unauthorized", "error_description": str(exc)},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )

        return await call_next(request)

# ---------------------------------------------------------------------------
# FastMCP tools
# ---------------------------------------------------------------------------
mcp = FastMCP("Mistral Hello World MCP Server")

@mcp.tool()
def search_products(query: str, max_price: float) -> str:
    """Search product catalog by natural language query and price ceiling. Returns JSON list of matching products with id, name, price, image_url."""

@mcp.tool()
def create_order(product_id: str, delivery_mode: str, customer_name: str, address: str) -> str:
    """Create a pending order in the system. Returns order_id and total amount."""

@mcp.tool()
def initiate_ciba_auth(order_id: str, phone_number: str) -> str:
    """Trigger a CIBA backchannel authentication request for a given order. Sends OTP via SMS. Returns auth_request_id."""

@mcp.tool()
def verify_ciba_otp(auth_request_id: str, otp: str) -> str:
    """Verify the OTP submitted by the user against the pending CIBA auth request. Returns verified: true/false."""

@mcp.tool()
def confirm_payment(order_id: str, auth_request_id: str) -> str:
    """Charge the customer's registered payment method once CIBA auth is confirmed. Returns confirmation_code and receipt_url."""

# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------
async def oauth_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer":                   MCP_SERVER_URL,
        "authorization_endpoint":   f"https://{AUTH0_DOMAIN}/authorize?audience={AUTH0_AUDIENCE}",
        "token_endpoint":           f"https://{AUTH0_DOMAIN}/oauth/token",
        "jwks_uri":                 JWKS_URL,
        "registration_endpoint":    f"{MCP_SERVER_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported":    ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported":         ["openid", "offline_access"],
    })

async def protected_resource_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource":                 AUTH0_AUDIENCE,
        "authorization_servers":    [f"https://{AUTH0_DOMAIN}/"],
        "bearer_methods_supported": ["header"],
        "scopes_supported":         ["openid", "offline_access"],
    })

async def dynamic_client_registration(request: Request) -> JSONResponse:
    body = await request.json()
    body["redirect_uris"] = [MISTRAL_REDIRECT]
    body.setdefault("grant_types",  ["authorization_code", "refresh_token"])
    body.setdefault("token_endpoint_auth_method", "client_secret_post")
    body.setdefault("response_types", ["code"])
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{AUTH0_DOMAIN}/oidc/register",
            json=body, headers={"Content-Type": "application/json"}, timeout=15,
        )
    if resp.status_code not in (200, 201):
        logger.error(f"DCR failed: {resp.status_code} {resp.text}")
        return JSONResponse({"error": "registration_failed", "detail": resp.text},
                            status_code=resp.status_code)
    return JSONResponse(resp.json(), status_code=resp.status_code)

async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

async def debug_token(request: Request) -> JSONResponse:
    """TEMPORARY — remove after debugging."""
    auth  = request.headers.get("Authorization", "NONE")
    token = auth.removeprefix("Bearer ").strip()
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
    logger.info(f"Starting secured MCP server on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
