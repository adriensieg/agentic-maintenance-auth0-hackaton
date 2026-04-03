"""
main.py
────────
WashFix — FastAPI application entry point.

Registers all routers, configures middleware, serves the demo UI,
and starts background workers on startup.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config.settings import get_settings
from api.chat        import router as chat_router
from api.payment     import router as payment_router
from api             import (
    booking_router,
    photo_router,
    webhooks_router,
    audit_router,
)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("washfix.main")

# ── App ───────────────────────────────────────────────────────────────────
s = get_settings()

app = FastAPI(
    title       = "WashFix — Agentic Appliance Repair",
    description = "AI-powered repair booking with Auth0, CIBA, ReBAC, Jira & Stripe.",
    version     = "1.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],   # Tighten in production
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────
app.include_router(chat_router)
app.include_router(payment_router)
app.include_router(booking_router)
app.include_router(photo_router)
app.include_router(webhooks_router)
app.include_router(audit_router)

# ── Static files ──────────────────────────────────────────────────────────
import os
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── Lifecycle ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    logger.info("=" * 60)
    logger.info("WashFix starting up.")
    logger.info(f"  Auth0 domain  : {s.auth0_domain}")
    logger.info(f"  App base URL  : {s.app_base_url}")
    logger.info(f"  MCP server    : {s.mcp_server_url}")
    logger.info(f"  Jira project  : {s.jira_project_key}")
    logger.info("=" * 60)

    # Start background workers
    from workers import token_refresh_worker, audit_flush_worker
    asyncio.create_task(token_refresh_worker())
    asyncio.create_task(audit_flush_worker())
    logger.info("Background workers started.")


@app.on_event("shutdown")
async def shutdown() -> None:
    logger.info("WashFix shutting down.")


# ── Health & discovery ────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status":  "ok",
        "version": "1.0.0",
        "auth0":   s.auth0_domain,
    }


@app.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata() -> dict:
    return {
        "resource":                 s.auth0_audience,
        "authorization_servers":    [f"https://{s.auth0_domain}/"],
        "bearer_methods_supported": ["header"],
        "scopes_supported":         ["openid", "offline_access", "payment:approve"],
    }


# ── Demo UI ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_demo(request: Request) -> HTMLResponse:
    """Serve the demo UI (the HTML from the original demo, enhanced)."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path) as f:
            return HTMLResponse(content=f.read())

    # Minimal fallback
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head><title>WashFix</title></head>
    <body style="background:#1a1a1a;color:#ebebeb;font-family:sans-serif;padding:40px">
        <h1>WashFix API</h1>
        <p>See <a href="/docs" style="color:#cc785c">/docs</a> for the API reference.</p>
        <p>Configure your <code>.env</code> and start chatting via <code>/api/chat</code>.</p>
    </body>
    </html>
    """)


# ── Exception handlers ────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code = 500,
        content     = {"error": "Internal server error", "detail": str(exc)},
    )


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting WashFix on :{port}")
    uvicorn.run(
        "main:app",
        host       = "0.0.0.0",
        port       = port,
        reload     = True,
        log_level  = "info",
    )
