"""
core/session.py
────────────────
Per-user conversation session state management.

Sessions are stored in Redis (with JSON serialization) so they survive
process restarts and work across multiple workers.

Falls back to an in-memory dict when Redis is not available.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from models import UserSession, SessionPhase, ApplianceInfo, DiagnosisResult

logger = logging.getLogger("washfix.core.session")

SESSION_TTL = 3600 * 4  # 4 hours


class SessionManager:
    """CRUD for UserSession objects backed by Redis."""

    def __init__(self) -> None:
        self._memory: dict[str, dict] = {}  # fallback
        self._redis = None

    async def _get_redis(self):
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis
            from config.settings import get_settings
            s = get_settings()
            self._redis = aioredis.from_url(s.redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("Redis connected for session store.")
        except Exception as exc:
            logger.warning(f"Redis unavailable ({exc}) — using in-memory session store.")
            self._redis = None
        return self._redis

    async def create(
        self,
        user_id: str,
        user_name: str,
        user_phone: Optional[str] = None,
    ) -> UserSession:
        session = UserSession(
            session_id = secrets.token_urlsafe(16),
            user_id    = user_id,
            user_name  = user_name,
            user_phone = user_phone,
        )
        await self._save(session)
        logger.info(f"Session created: {session.session_id} for user {user_id}")
        return session

    async def get(self, session_id: str) -> Optional[UserSession]:
        redis = await self._get_redis()
        if redis:
            raw = await redis.get(f"session:{session_id}")
            if raw:
                return UserSession(**json.loads(raw))
        elif session_id in self._memory:
            return UserSession(**self._memory[session_id])
        return None

    async def get_or_create(
        self,
        session_id: Optional[str],
        user_id: str,
        user_name: str,
        user_phone: Optional[str] = None,
    ) -> UserSession:
        if session_id:
            existing = await self.get(session_id)
            if existing:
                return existing
        return await self.create(user_id, user_name, user_phone)

    async def update_phase(self, session_id: str, phase: SessionPhase) -> None:
        session = await self.get(session_id)
        if session:
            session.phase = phase
            session.updated_at = datetime.now(timezone.utc)
            await self._save(session)
            logger.debug(f"Session {session_id} phase → {phase}")

    async def set_appliance(self, session_id: str, appliance: ApplianceInfo) -> None:
        session = await self.get(session_id)
        if session:
            session.appliance = appliance
            await self._save(session)

    async def set_diagnosis(self, session_id: str, diagnosis: DiagnosisResult) -> None:
        session = await self.get(session_id)
        if session:
            session.diagnosis = diagnosis
            await self._save(session)

    async def set_meta(self, session_id: str, key: str, value: Any) -> None:
        session = await self.get(session_id)
        if session:
            session.metadata[key] = value
            await self._save(session)

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        session = await self.get(session_id)
        if session:
            session.messages.append({
                "role":    role,
                "content": content,
                "ts":      datetime.now(timezone.utc).isoformat(),
            })
            await self._save(session)

    async def delete(self, session_id: str) -> None:
        redis = await self._get_redis()
        if redis:
            await redis.delete(f"session:{session_id}")
        self._memory.pop(session_id, None)

    async def _save(self, session: UserSession) -> None:
        redis = await self._get_redis()
        data = session.model_dump_json()
        if redis:
            await redis.setex(f"session:{session.session_id}", SESSION_TTL, data)
        else:
            self._memory[session.session_id] = json.loads(data)


# Singleton
session_manager = SessionManager()
