"""
api/chat.py
────────────
/api/chat — REST + WebSocket endpoints for the agent conversation.

REST POST /api/chat/message
  Processes one user turn.  Requires Bearer auth.
  Accepts multipart/form-data for photo uploads.

WebSocket /api/chat/ws/{session_id}
  Streaming interface for real-time SSE-style updates.

GET /api/chat/session/{session_id}
  Return the current session state (phase, messages, etc.).

POST /api/chat/session
  Create a new session (called on first page load after auth).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException,
    UploadFile, WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.middleware import get_subject, require_auth
from core.agent      import agent
from core.audit_log  import audit_log
from core.session    import session_manager
from models          import SessionPhase

logger = logging.getLogger("washfix.api.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Request / Response models ─────────────────────────────────────────────

class NewSessionRequest(BaseModel):
    user_name:  str
    user_phone: Optional[str] = None


class MessageRequest(BaseModel):
    session_id:   str
    message:      str


class MessageResponse(BaseModel):
    response:   str
    phase:      str
    actions:    list[dict]
    session_id: str


# ── Routes ────────────────────────────────────────────────────────────────

@router.post("/session", response_model=dict)
async def create_session(
    body:   NewSessionRequest,
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """
    Create a new WashFix session for the authenticated user.
    """
    user_id = get_subject(claims)
    audit_log.token_verified(user_id, method="jwt")

    session = await session_manager.create(
        user_id    = user_id,
        user_name  = body.user_name,
        user_phone = body.user_phone,
    )
    logger.info(f"New session {session.session_id} created for user {user_id}.")
    return {
        "session_id": session.session_id,
        "phase":      session.phase.value,
        "user_name":  session.user_name,
    }


@router.post("/message", response_model=MessageResponse)
async def send_message(
    session_id: str  = Form(...),
    message:    str  = Form(...),
    photo:      Optional[UploadFile] = File(default=None),
    claims:     dict = Depends(require_auth),
) -> MessageResponse:
    """
    Send a text message (and optional photo) to the agent.
    Returns the agent's response and any side-effect actions.
    """
    user_id = get_subject(claims)
    logger.info(f"Message from {user_id}: session={session_id} msg='{message[:60]}'")

    photo_bytes: Optional[bytes] = None
    if photo:
        photo_bytes = await photo.read()
        logger.info(f"Photo received: {photo.filename} ({len(photo_bytes)} bytes)")

    result = await agent.process_message(
        session_id  = session_id,
        user_message = message,
        user_id     = user_id,
        photo_bytes = photo_bytes,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return MessageResponse(**result)


@router.get("/session/{session_id}")
async def get_session(
    session_id: str,
    claims: dict = Depends(require_auth),
) -> dict[str, Any]:
    """Return the current session state."""
    user_id = get_subject(claims)
    session = await session_manager.get(session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {
        "session_id": session.session_id,
        "phase":      session.phase.value,
        "user_name":  session.user_name,
        "appliance":  session.appliance.model_dump() if session.appliance else None,
        "diagnosis":  session.diagnosis.model_dump() if session.diagnosis else None,
        "messages":   session.messages[-20:],  # Last 20 messages
        "metadata":   {k: v for k, v in session.metadata.items()
                       if k not in ("pending_otp",)},  # Never expose OTP
    }


@router.delete("/session/{session_id}")
async def end_session(
    session_id: str,
    claims: dict = Depends(require_auth),
) -> dict[str, str]:
    """End and clean up a session."""
    user_id = get_subject(claims)
    session = await session_manager.get(session_id)
    if session and session.user_id == user_id:
        await session_manager.delete(session_id)
    return {"status": "deleted", "session_id": session_id}


# ── WebSocket ─────────────────────────────────────────────────────────────

@router.websocket("/ws/{session_id}")
async def websocket_chat(
    websocket:  WebSocket,
    session_id: str,
) -> None:
    """
    WebSocket endpoint for real-time streaming chat.

    Client sends JSON: {"message": "...", "token": "<bearer>"}
    Server sends JSON: {"type": "chunk"|"done"|"error", "content": "..."}
    """
    await websocket.accept()
    logger.info(f"WebSocket connected for session {session_id}")

    try:
        while True:
            raw = await websocket.receive_json()
            token   = raw.get("token", "")
            message = raw.get("message", "").strip()

            if not token or not message:
                await websocket.send_json({"type": "error", "content": "Missing token or message."})
                continue

            # Verify token
            try:
                from auth.middleware import verify_bearer_token
                claims  = await verify_bearer_token(token)
                user_id = claims.get("sub", "unknown")
            except Exception:
                await websocket.send_json({"type": "error", "content": "Invalid token."})
                continue

            # Send "thinking" signal
            await websocket.send_json({"type": "thinking"})

            result = await agent.process_message(
                session_id   = session_id,
                user_message = message,
                user_id      = user_id,
            )

            await websocket.send_json({
                "type":     "done",
                "content":  result.get("response", ""),
                "phase":    result.get("phase", ""),
                "actions":  result.get("actions", []),
            })

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as exc:
        logger.exception(f"WebSocket error for session {session_id}: {exc}")
        try:
            await websocket.send_json({"type": "error", "content": str(exc)})
        except Exception:
            pass
