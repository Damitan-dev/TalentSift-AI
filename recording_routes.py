"""Recording transport is independent of the OpenAI interview connection."""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from database import load_session
from recording_storage import RecordingError, recording_store

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/recording/{session_id}")
async def save_recording(websocket: WebSocket, session_id: str):
    writer = None
    try:
        session = await asyncio.to_thread(load_session, session_id)
        if not session.consent_given or session.status not in ("pending", "in_progress"):
            await websocket.close(code=1008)
            return
    except (KeyError, ValueError):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        # Keep the token out of URLs, request logs, and local storage.
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if not isinstance(hello, dict) or hello.get("type") != "start":
            raise RecordingError("Invalid recording request.")
        writer = await asyncio.to_thread(
            recording_store.begin, session_id, hello.get("token"), hello.get("mime_type")
        )
        await websocket.send_json({"type": "recording_ready"})
        deadline = time.monotonic() + 15 * 60
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecordingError("Audio recording reached its time limit.")
            message = await asyncio.wait_for(websocket.receive(), timeout=min(120, remaining))
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await asyncio.to_thread(writer.append, message["bytes"])
                continue
            event = json.loads(message.get("text") or "{}")
            if not isinstance(event, dict) or event.get("type") != "finish":
                raise RecordingError("Invalid recording message.")
            result = await asyncio.to_thread(writer.finish, event.get("complete") is True)
            await websocket.send_json({
                "type": "recording_saved",
                "status": result["status"],
                "bytes": result["bytes"],
            })
            await websocket.close(code=1000)
            return
    except WebSocketDisconnect:
        pass
    except (RecordingError, json.JSONDecodeError, OSError, TimeoutError) as error:
        # Don't expose server paths, tokens, or audio contents in client errors.
        logger.warning("Recording could not finish for %s (%s)", session_id, type(error).__name__)
        try:
            await websocket.send_json({"type": "recording_error"})
            await websocket.close(code=1008)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
    finally:
        if writer and not writer.closed:
            try:
                await asyncio.shield(asyncio.to_thread(writer.finish, False))
            except OSError:
                logger.exception("Unable to finalize recording for %s", session_id)


@router.get("/recruiter/session/{session_id}/audio")
def interview_audio(session_id: str, download: bool = False):
    # Apply the same recruiter authentication as the review page when adding
    # production access control. The current local MVP has no recruiter login.
    try:
        load_session(session_id)
        recording = recording_store.details(session_id)
    except (KeyError, RecordingError):
        raise HTTPException(status_code=404, detail="Recording not found.") from None
    if not recording["available"]:
        raise HTTPException(status_code=404, detail="No audio was saved for this interview.")
    return FileResponse(
        path=recording["path"],
        media_type=recording["mime_type"],
        filename=f"talentsift-interview-{session_id}{recording['path'].suffix}",
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
