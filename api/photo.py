"""
api/photo.py
─────────────
/api/photo — Appliance photo upload and AI vision analysis.

POST /api/photo/analyse
  Upload a photo of the broken appliance.
  Requires token-based permission: the session must belong to the caller.

  1. Verify session ownership (Bearer token bound to user_id).
  2. Read photo bytes.
  3. Run DiagnosisEngine with photo_bytes.
  4. Store result in session.
  5. Log audit event.
  6. Return diagnosis.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from auth.middleware import get_subject, require_auth
from core.audit_log  import audit_log

logger = logging.getLogger("washfix.api.photo")
router = APIRouter(prefix="/api/photo", tags=["photo"])

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/analyse")
async def analyse_photo(
    session_id: str        = Form(...),
    photo:      UploadFile = File(...),
    claims:     dict       = Depends(require_auth),
) -> dict[str, Any]:
    """
    Upload a photo of the appliance and run AI vision diagnosis.

    Access control:
      • The session_id must belong to the authenticated user (token-based
        permission check — not just a JWT scope, but object-level binding).

    Returns the DiagnosisResult JSON on success.
    """
    user_id = get_subject(claims)

    # Token-based permission: session must be owned by this user
    from core.session import session_manager
    session = await session_manager.get(session_id)
    if not session or session.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Access denied — session does not belong to this user.",
        )

    # Size guard
    photo_bytes = await photo.read()
    if len(photo_bytes) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Photo exceeds maximum size of {MAX_PHOTO_BYTES // (1024*1024)} MB.",
        )

    if len(photo_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty photo file.")

    logger.info(
        f"Photo uploaded: session={session_id} "
        f"file={photo.filename!r} "
        f"size={len(photo_bytes):,} bytes "
        f"content_type={photo.content_type}"
    )
    audit_log.photo_analysed(session_id, user_id)

    # Run diagnosis engine with photo
    from core.diagnosis import diagnosis_engine
    appliance_model = session.appliance.model if session.appliance else "Samsung"

    # Include any fault codes already extracted from text conversation
    existing_fault_codes: list[str] = []
    if session.diagnosis and session.diagnosis.fault_code not in ("UNKNOWN", ""):
        existing_fault_codes = [session.diagnosis.fault_code]

    diagnosis = await diagnosis_engine.diagnose(
        fault_codes     = existing_fault_codes,
        symptoms        = "",
        appliance_model = appliance_model,
        photo_bytes     = photo_bytes,
    )

    # Update session with enriched diagnosis
    await session_manager.set_diagnosis(session_id, diagnosis)

    return {
        "fault_code":  diagnosis.fault_code,
        "description": diagnosis.description,
        "part_number": diagnosis.part_number,
        "part_name":   diagnosis.part_name,
        "confidence":  round(diagnosis.confidence, 3),
        "source":      "vision+db" if existing_fault_codes else "vision",
    }
