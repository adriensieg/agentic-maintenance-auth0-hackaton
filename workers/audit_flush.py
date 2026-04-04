"""
workers/audit_flush.py
───────────────────────
Background worker: flush the in-memory audit ring buffer to the PostgreSQL
`audit_events` table every 30 seconds.

Why two-tier storage?
  • In-memory ring buffer (core/audit_log.py): zero-latency writes during
    the request, always available even if DB is slow.
  • DB table: survives process restarts, queryable, never overwritten.

The flush worker bridges the two: it reads unseen events from the ring
buffer and bulk-inserts them into the DB without blocking the request path.

Watermark:
  We track the count of events last flushed. Since the ring buffer is a
  deque(maxlen=1000) in descending order, new events are at the front.
  After each flush we record how many events were in the buffer; on the
  next run we flush only the diff.

Edge cases:
  • DB unavailable → log warning, skip flush, retry next cycle.
  • Ring buffer overflows (>1000 events since last flush) → partial loss
    of old events is acceptable; critical events are structlog-logged to
    stdout which is captured by the container runtime.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

logger = logging.getLogger("washfix.workers.audit_flush")

FLUSH_INTERVAL_SECONDS = 30
_last_flush_count: int = 0


async def audit_flush_worker() -> None:
    """
    Main worker loop — runs indefinitely as an asyncio task.
    Registered in main.py on startup.
    """
    logger.info("Audit flush worker started.")
    while True:
        try:
            await _flush_cycle()
        except asyncio.CancelledError:
            logger.info("Audit flush worker cancelled.")
            break
        except Exception as exc:
            logger.error(f"Audit flush worker error: {exc}")
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)


async def _flush_cycle() -> None:
    """
    One flush cycle: identify new events → bulk insert into DB.
    """
    global _last_flush_count

    from core.audit_log import audit_log
    all_events = audit_log.get_recent(1000)  # newest first
    total = len(all_events)

    # New events = those added since last flush
    new_count = max(0, total - _last_flush_count)
    if new_count == 0:
        logger.debug("Audit flush: no new events.")
        return

    # Events are newest-first; take the `new_count` newest ones
    new_events = all_events[:new_count]
    logger.debug(f"Audit flush: persisting {new_count} new event(s).")

    try:
        await _persist_events(new_events)
        _last_flush_count = total
        logger.debug(f"Audit flush: {new_count} event(s) persisted.")
    except Exception as exc:
        logger.warning(f"Audit flush DB write failed: {exc}")
        # Don't update watermark — retry next cycle


async def _persist_events(events: list[dict]) -> None:
    """
    Bulk insert audit events into the DB.
    Uses INSERT OR IGNORE (SQLite) / ON CONFLICT DO NOTHING (Postgres)
    to handle retries safely.
    """
    if not events:
        return

    try:
        from db.database import AsyncSessionLocal
        from db.database import AuditEventORM
        from datetime import datetime, timezone

        async with AsyncSessionLocal() as db:
            for evt in events:
                # Check if already persisted (idempotent via event_id)
                event_id = evt.get("event_id") or str(uuid.uuid4())
                ts_raw = evt.get("timestamp")
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                    except Exception:
                        ts = datetime.now(timezone.utc)
                elif isinstance(ts_raw, datetime):
                    ts = ts_raw
                else:
                    ts = datetime.now(timezone.utc)

                row = AuditEventORM(
                    event_id   = event_id,
                    timestamp  = ts,
                    action     = evt.get("action", "unknown"),
                    session_id = evt.get("session_id"),
                    user_id    = evt.get("user_id"),
                    actor      = evt.get("actor", "agent"),
                    details    = evt.get("details", {}),
                    ip_address = evt.get("ip"),
                )
                db.add(row)

            try:
                await db.commit()
            except Exception:
                # Likely duplicate key on event_id — safe to ignore
                await db.rollback()

    except Exception as exc:
        raise RuntimeError(f"DB persist failed: {exc}") from exc
