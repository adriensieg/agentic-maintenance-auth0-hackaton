import json
import base64
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import AUTH0_AUDIENCE, UNPROTECTED_PATHS
from .token import verify_token

logger = logging.getLogger(__name__)


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
