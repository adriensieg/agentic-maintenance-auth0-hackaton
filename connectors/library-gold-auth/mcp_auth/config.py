import os

AUTH0_DOMAIN        = os.environ.get("AUTH0_DOMAIN",        "xx.us.auth0.com")
AUTH0_AUDIENCE      = os.environ.get("AUTH0_AUDIENCE",      "https://mistralai.devailab.work/mcp")
AUTH0_CLIENT_ID     = os.environ.get("AUTH0_CLIENT_ID",     "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")
MCP_SERVER_URL      = os.environ.get("MCP_SERVER_URL",      "https://mistralai.devailab.work")

JWKS_URL            = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
ISSUER              = f"https://{AUTH0_DOMAIN}/"
CLAUDE_CALLBACK_URL = "https://claude.ai/api/mcp/auth_callback"

UNPROTECTED_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/oauth/register",
    "/health",
    "/debug-token",
}
