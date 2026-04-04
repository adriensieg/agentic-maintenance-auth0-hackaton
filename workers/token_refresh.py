"""
workers/token_refresh.py
─────────────────────────
Background worker: proactively refresh third-party OAuth tokens before
they expire, so no API call ever fails mid-workflow due to a stale token.

Strategy:
  • Runs every 5 minutes as an asyncio background task.
  • Scans the active session list in Redis for users with tokens expiring
    within the next 10 minutes.
  • For each expiring token, calls Token Vault `get_access_token()` which
    handles rotation transparently.
  • Logs each rotation for the audit trail.

In production with many users this would be a Celery beat task or a
separate process scanning the DB `sessions` table for active users.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("washfix.workers.token_refresh")

# Services whose tokens should be proactively rotated
MANAGED_SERVICES = ["jira", "google_calendar", "stripe"]

# Rotate if token expires within this many seconds
REFRESH_THRESHOLD_SECONDS = 600  # 10 minutes

# Run interval
RUN_INTERVAL_SECONDS = 300  # 5 minutes


async def token_refresh_worker() -> None:
    """
    Main worker loop — runs indefinitely as an asyncio task.
    Registered in main.py on startup.
    """
    logger.info("Token refresh worker started.")
    while True:
        try:
            await _refresh_cycle()
        except asyncio.CancelledError:
            logger.info("Token refresh worker cancelled.")
            break
        except Exception as exc:
            logger.error(f"Token refresh worker error: {exc}")
        await asyncio.sleep(RUN_INTERVAL_SECONDS)


async def _refresh_cycle() -> None:
    """
    One refresh cycle: scan active sessions → rotate expiring tokens.
    """
    active_user_ids = await _get_active_user_ids()
    if not active_user_ids:
        logger.debug("Token refresh: no active users.")
        return

    rotated = 0
    for user_id in active_user_ids:
        for service in MANAGED_SERVICES:
            if await _should_refresh(user_id, service):
                refreshed = await _rotate_token(user_id, service)
                if refreshed:
                    rotated += 1

    if rotated:
        logger.info(f"Token refresh: rotated {rotated} token(s).")
    else:
        logger.debug("Token refresh: all tokens fresh.")


async def _get_active_user_ids() -> list[str]:
    """
    Return a list of user IDs with active sessions.
    In production: query the DB sessions table for recently-active sessions.
    For now: scan Redis keys.
    """
    try:
        import redis.asyncio as aioredis
        from config.settings import get_settings
        s = get_settings()
        r = aioredis.from_url(s.redis_url, decode_responses=True)
        keys = await r.keys("session:*")
        await r.aclose()

        user_ids: list[str] = []
        for key in keys[:50]:  # cap at 50 to avoid thundering herd
            try:
                import json
                raw = await _redis_get(key)
                if raw:
                    data = json.loads(raw)
                    uid = data.get("user_id")
                    if uid and uid not in user_ids:
                        user_ids.append(uid)
            except Exception:
                pass
        return user_ids
    except Exception as exc:
        logger.debug(f"Could not enumerate active users (Redis unavailable): {exc}")
        return []


async def _redis_get(key: str) -> Optional[str]:
    try:
        import redis.asyncio as aioredis
        from config.settings import get_settings
        r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        val = await r.get(key)
        await r.aclose()
        return val
    except Exception:
        return None


async def _should_refresh(user_id: str, service: str) -> bool:
    """
    Check if the stored access token for `service` expires soon.
    """
    import time
    try:
        from auth.token_vault import token_vault
        bundle = await token_vault.get(user_id, service)
        if not bundle:
            return False
        expire_at = bundle.get("expire_at", 0)
        if not expire_at:
            return False
        return time.time() > (expire_at - REFRESH_THRESHOLD_SECONDS)
    except Exception:
        return False


async def _rotate_token(user_id: str, service: str) -> bool:
    """
    Rotate a token by calling `get_access_token` which triggers refresh + storage.
    """
    try:
        from auth.token_vault import token_vault
        token = await token_vault.get_access_token(user_id, service)
        if token:
            logger.info(f"Token rotated proactively: service={service} user=***{user_id[-6:]}")
            return True
        return False
    except Exception as exc:
        logger.warning(f"Token rotation failed: service={service} user=***{user_id[-6:]}: {exc}")
        return False
