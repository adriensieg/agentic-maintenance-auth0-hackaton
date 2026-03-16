import json
import logging
import os
import re
import urllib.parse
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from google import genai
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException

from .lib_twillio.config import (
    PORT, APP_BASE_URL,
    GOOGLE_API_KEY,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER,
    MAINTAINERS,
)
from .lib_twillio.prompts import CREW_SYSTEM_PROMPT, build_maintainer_system_prompt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("restaurant-agent")

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
logger.info("Gemini client initialised.")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
logger.info("Twilio client initialised.")

logger.info("Maintainers configured: %s", list(MAINTAINERS.keys()))

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
active_chat_sessions: dict = {}
call_contexts: dict = {}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Restaurant AI Agent")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    logger.info("Serving frontend to %s", request.client.host)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "maintainers": MAINTAINERS,
    })


@app.post("/chat")
async def crew_chat(request: Request):
    body = await request.json()
    session_id: str = body.get("session_id", "default")
    user_message: str = body.get("message", "").strip()

    logger.info("[CREW CHAT] session=%s | message='%s'", session_id, user_message)

    if not user_message:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    if session_id not in active_chat_sessions:
        logger.info("[CREW CHAT] Creating new Gemini chat session for session_id=%s", session_id)
        active_chat_sessions[session_id] = gemini_client.chats.create(
            model="gemini-2.5-flash",
            config={"system_instruction": CREW_SYSTEM_PROMPT},
        )

    chat = active_chat_sessions[session_id]

    try:
        response = chat.send_message(user_message)
        reply_text: str = response.text
        logger.info("[CREW CHAT] session=%s | reply='%s'", session_id, reply_text)
    except Exception as exc:
        logger.exception("[CREW CHAT] Gemini error for session=%s: %s", session_id, exc)
        raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

    call_placed = False
    call_info = {}
    maintainer_key = _extract_call_tag(reply_text)

    if maintainer_key and maintainer_key in MAINTAINERS:
        maintainer = MAINTAINERS[maintainer_key]
        logger.info("[CREW CHAT] Agent decided to call maintainer '%s' (%s)", maintainer_key, maintainer["name"])
        issue_summary = _summarise_issue(chat, user_message)
        call_sid = await _place_outbound_call(
            to_phone=maintainer["phone"],
            issue_description=issue_summary,
            maintainer=maintainer,
        )
        if call_sid:
            call_placed = True
            call_info = {
                "call_sid": call_sid,
                "maintainer_name": maintainer["name"],
                "maintainer_phone": maintainer["phone"],
            }

    clean_reply = reply_text.replace(f"[CALL:{maintainer_key}]", "").strip() if maintainer_key else reply_text

    return {
        "reply": clean_reply,
        "call_placed": call_placed,
        "call_info": call_info,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/twiml")
async def twiml_endpoint(request: Request):
    call_sid = "unknown"
    try:
        raw_body = await request.body()
        params = urllib.parse.parse_qs(raw_body.decode("utf-8"))
        call_sid = params.get("CallSid", ["unknown"])[0]
    except Exception as exc:
        logger.warning("[TWIML] Could not parse CallSid from body: %s", exc)

    logger.info("[TWIML] Twilio requested TwiML for CallSid=%s", call_sid)

    ws_url = APP_BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    logger.info("[TWIML] WebSocket URL: %s", ws_url)

    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{ws_url}"
      welcomeGreeting="Hello, this is the restaurant AI assistant calling. Please hold for one moment."
      ttsProvider="Google"
      voice="en-US-Journey-O"
    />
  </Connect>
</Response>"""

    logger.info("[TWIML] Returning ConversationRelay TwiML for CallSid=%s", call_sid)
    return Response(content=xml_response, media_type="text/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    call_sid: Optional[str] = None
    maintainer_chat = None

    logger.info("[WS] New WebSocket connection accepted.")

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            msg_type = message.get("type")

            if msg_type == "setup":
                call_sid = message.get("callSid")
                logger.info("[WS] Setup received for CallSid=%s", call_sid)
                context = call_contexts.get(call_sid, {})
                issue = context.get("issue", "a maintenance issue (no further details provided)")
                maintainer = context.get("maintainer", {"name": "the maintainer"})
                system_prompt = build_maintainer_system_prompt(issue, maintainer)
                maintainer_chat = gemini_client.chats.create(
                    model="gemini-2.5-flash",
                    config={"system_instruction": system_prompt},
                )
                opening = maintainer_chat.send_message(
                    "Start the call now. Greet the maintainer and explain the issue."
                )
                opening_text = opening.text
                logger.info("[WS] Opening message generated: '%s'", opening_text)
                await websocket.send_text(json.dumps({
                    "type": "text",
                    "token": opening_text,
                    "last": True,
                }))

            elif msg_type == "prompt":
                if not call_sid or maintainer_chat is None:
                    logger.warning("[WS] Received prompt before setup. Ignoring.")
                    continue
                voice_prompt = message.get("voicePrompt", "")
                logger.info("[WS] Maintainer said: '%s' (CallSid=%s)", voice_prompt, call_sid)
                try:
                    response = maintainer_chat.send_message(voice_prompt)
                    reply_text = response.text
                    logger.info("[WS] Gemini reply to maintainer: '%s'", reply_text)
                except Exception as exc:
                    logger.exception("[WS] Gemini error during call %s: %s", call_sid, exc)
                    reply_text = "I'm sorry, I encountered a technical issue. Please call the restaurant directly. Thank you and goodbye."
                await websocket.send_text(json.dumps({
                    "type": "text",
                    "token": reply_text,
                    "last": True,
                }))

            elif msg_type == "interrupt":
                logger.info("[WS] Barge-in / interrupt received for CallSid=%s", call_sid)

            elif msg_type == "end":
                logger.info("[WS] Call ended. CallSid=%s", call_sid)
                break

            else:
                logger.warning("[WS] Unknown message type='%s' for CallSid=%s", msg_type, call_sid)

    except WebSocketDisconnect:
        logger.info("[WS] WebSocket disconnected. CallSid=%s", call_sid)
    except Exception as exc:
        logger.exception("[WS] Unexpected error on WebSocket. CallSid=%s | error=%s", call_sid, exc)
    finally:
        if call_sid and call_sid in call_contexts:
            call_contexts.pop(call_sid)
            logger.info("[WS] Cleaned up call context for CallSid=%s", call_sid)


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "active_crew_sessions": len(active_chat_sessions),
        "active_call_contexts": len(call_contexts),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_call_tag(text: str) -> Optional[str]:
    match = re.search(r"\[CALL:(\w+)\]", text)
    if match:
        key = match.group(1)
        logger.info("[HELPER] Extracted CALL tag: '%s'", key)
        return key
    return None


def _summarise_issue(chat, latest_message: str) -> str:
    return latest_message


async def _place_outbound_call(to_phone: str, issue_description: str, maintainer: dict) -> Optional[str]:
    twiml_url = f"{APP_BASE_URL}/twiml"
    logger.info(
        "[TWILIO] Placing outbound call to %s (%s) | TwiML URL: %s",
        maintainer["name"], to_phone, twiml_url,
    )
    try:
        call = twilio_client.calls.create(
            to=to_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
            method="POST",
        )
        call_sid = call.sid
        logger.info("[TWILIO] Call created successfully. CallSid=%s", call_sid)
        call_contexts[call_sid] = {
            "issue": issue_description,
            "maintainer": maintainer,
        }
        return call_sid
    except TwilioRestException as exc:
        logger.exception("[TWILIO] Failed to place call to %s: %s", to_phone, exc)
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting Restaurant AI Agent on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
