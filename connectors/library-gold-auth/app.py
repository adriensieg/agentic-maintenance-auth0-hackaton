import logging
import os

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route, Mount

from mcp_auth import (
    BearerAuthMiddleware,
    oauth_metadata,
    protected_resource_metadata,
    dynamic_client_registration,
    health_check,
    debug_token,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP tools
# ---------------------------------------------------------------------------
mcp = FastMCP("Hello World MCP Server")


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
    ],
)

app.add_middleware(BearerAuthMiddleware)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting MCP server (Claude-compatible) on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
