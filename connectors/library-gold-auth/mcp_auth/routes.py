import json
import base64
import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import (
    AUTH0_DOMAIN,
    AUTH0_AUDIENCE,
    MCP_SERVER_URL,
    JWKS_URL,
    CLAUDE_CALLBACK_URL,
)

logger = logging.getLogger(__name__)


async def oauth_metadata(request: Request) -> JSONResponse:
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
    return JSONResponse({
        "resource":                 AUTH0_AUDIENCE,
        "authorization_servers":    [f"https://{AUTH0_DOMAIN}/"],
        "bearer_methods_supported": ["header"],
        "scopes_supported":         ["openid", "offline_access"],
    })


async def dynamic_client_registration(request: Request) -> JSONResponse:
    body = await request.json()

    body["redirect_uris"] = [CLAUDE_CALLBACK_URL]
    body.setdefault("grant_types",                ["authorization_code", "refresh_token"])
    body.setdefault("token_endpoint_auth_method", "client_secret_post")
    body.setdefault("response_types",             ["code"])
    body.setdefault("client_name",                "Claude")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{AUTH0_DOMAIN}/oidc/register",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"DCR failed: {resp.status_code} {resp.text}")
        return JSONResponse(
            {"error": "registration_failed", "detail": resp.text},
            status_code=resp.status_code,
        )
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
