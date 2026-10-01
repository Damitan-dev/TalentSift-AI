import asyncio
from contextlib import asynccontextmanager
import json
import os
import time
from pathlib import Path

import websockets
from live_captions import LiveCaptionRelay
from transcription_config import build_transcription_settings, interview_language
from recording_routes import router as recording_router
from recording_storage import recording_store, RecordingError
from interview_turns import InterviewTurns, FinalTranscripts, build_turn_detection
from interview_coverage import InterviewCoverage, NEXT_TOPIC_TOOL, FINISH_TOOL
from relay_lifecycle import run_relay_pair, connection_failure_details, is_billing_failure

from models import (
    Candidate,
    JobListing,
    Session,
    TranscriptTurn,
    utc_now,
)
from pydantic import BaseModel, ConfigDict
from access_control import AccessStore, AccessError, AudioBudget, AudioRateError
from access_routes import install_access_routes
from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Request,
    Form,
)

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse, RedirectResponse

from database import (
    initialize_database,
    list_jobs,
    load_job,
    save_job,
    save_candidate,
    load_candidate,
    load_session,
    save_session,
    save_transcript_turn,
)

from recruiter_dashboard import (
    build_fairness_summary,
    build_job_snapshot,
    build_session_detail,
)
from scoring.session_scoring import (
    apply_recruiter_override,
    restore_ai_score,
    score_database_session,
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(recording_router)

# Make sure the SQLite tables exist whenever
# TalentSift starts.
initialize_database()
app.state.access = AccessStore()
app.state.access.initialize()
app.state.access.bootstrap_from_env()


# ---------------------------------------------------------
# OPENAI REALTIME CONFIGURATION
# ---------------------------------------------------------

# Load variables from our .env file.
#
# This is where OPENAI_API_KEY should live.
load_dotenv()


# Read the API key from the environment.
#
# IMPORTANT:
# The API key lives ONLY on our Python server.
#
# We NEVER put this key inside index.html or browser JavaScript.
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]


# This is the OpenAI Realtime WebSocket address.
#
# For now we keep the same model URL that our working
# interviewer.py already uses.
ENGINE_URL = (
    "wss://api.openai.com/v1/realtime"
    "?model=gpt-realtime"
)


# When FastAPI connects to OpenAI,
# it sends this Authorization header.
ENGINE_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}"
}


# Our browser is sending PCM16 audio at 24 kHz,
# so the Realtime session must expect the same format.
MIC_RATE = 24000
SPK_RATE = 24000


# ---------------------------------------------------------
# BIANCA'S PROMPT
# ---------------------------------------------------------

# Find the folder containing app.py.
BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(
    directory=str(
        BASE_DIR / "templates"
    )
)

# Build the path to our existing interviewer prompt.
PROMPT_FILE = (
    BASE_DIR
    / "prompts"
    / "interviewer_prompt.txt"
)


# Load the same Bianca prompt that interviewer.py uses.
prompt_template = PROMPT_FILE.read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------
# INTERVIEW FINISHING RULE
# ---------------------------------------------------------

# The server validates topic coverage before accepting a normal finish.
#
# But Bianca does not invent the final closing.
# She calls our finish_interview tool instead.
FINISHING_INSTRUCTIONS = """
Conduct a focused and consistently structured interview.

INTERVIEW BOUNDARIES

- The main interview should fit within a maximum
  interview window of approximately 10 minutes.
- Do not deliberately stretch the interview to fill
  the entire available time.
- Cover the required job-related competency areas
  defined in the interview instructions.
- Ask concise questions.
- Ask only one question at a time.
- Use follow-up questions only when they are needed
  to obtain useful job-related evidence.
- Do not repeatedly probe the same competency after
  enough evidence has already been obtained.
- Use no more than one substantive follow-up for the
  same main question unless clarification is essential.
- If a candidate gives a weak, unclear, or very short
  answer, make one reasonable attempt to clarify and
  then move on.
- Do not keep interviewing indefinitely because a
  candidate's answer is incomplete.
- If an area could not be meaningfully explored,
  move on rather than repeatedly forcing an answer.

Use the server question plan for all core questions. After readiness is
confirmed, call next_interview_topic to start. After each answer and at most
one useful follow-up, call it again to obtain the next required question.
Do not select or skip core topics yourself. Collaboration and feedback/ownership
are separate required questions even though both use Culture & Values Fit.
Only call finish_interview when the server checklist is complete, unless
the server explicitly requests completion because the time limit was reached.
If completion is rejected, continue with the question supplied by the server;
do not say goodbye or treat the rejected request as the end of the interview.

Do not create your own closing statement.
Do not tell the candidate their score or whether
they passed or failed.
"""



# ---------------------------------------------------------
# INTERVIEW TIME LIMIT
# ---------------------------------------------------------
#
# All candidates for this MVP receive the same maximum
# interview window.
#
# The timer begins AFTER Bianca's opening disclosure has
# finished playing, so consent/opening time does not reduce
# the candidate's interview opportunity.

INTERVIEW_MAX_SECONDS = app.state.access.settings.interview_seconds

INTERVIEW_WARNING_SECONDS = min(60, max(1, INTERVIEW_MAX_SECONDS // 4))

# ---------------------------------------------------------
# LANGUAGE MAPPING
# ---------------------------------------------------------
#
# Where do "en" and "fr" come from?
#
# The candidate chooses one of them on the consent screen.
#
# Browser
#   ↓
# POST /api/session
#   ↓
# Session.language
#   ↓
# SQLite
LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
}


def build_instructions(
    language_code: str
) -> str:
    """
    Build Bianca's prompt for one interview Session.
    """

    # Example:
    #
    # "en" -> "English"
    # "fr" -> "French"
    language_name = LANGUAGE_NAMES[interview_language(language_code)]


    # Our interviewer prompt already contains:
    #
    # {LANGUAGE}
    #
    # Replace that placeholder with the language
    # belonging to THIS interview session.
    return (
        prompt_template.replace(
            "{LANGUAGE}",
            language_name,
        )
        + "\n\n"
        + FINISHING_INSTRUCTIONS
    )

# ---------------------------------------------------------
# FIXED CLOSING MESSAGES
# ---------------------------------------------------------
#
# These words are owned by TalentSift code,
# not invented freely by Bianca.

CLOSING_MESSAGES = {

    "en": (
        "Thank you for your time. "
        "That concludes your TalentSift interview. "
        "The hiring team will review your responses "
        "and contact you about next steps. "
        "You can now close this page."
    ),

    "fr": (
        "Merci pour votre temps. "
        "Ceci conclut votre entretien TalentSift. "
        "L'équipe de recrutement examinera vos réponses "
        "et vous contactera au sujet des prochaines étapes. "
        "Vous pouvez maintenant fermer cette page."
    ),
}


class CreateSessionRequest(BaseModel):
    # Job and identity come from the invitation, never from browser fields.
    model_config = ConfigDict(extra="forbid")
    language: str
    consent: bool
    record_audio: bool = False

# ---------------------------------------------------------
# NORMAL HTTP HEALTH CHECK
# ---------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "TalentSift",
    }


# ---------------------------------------------------------
# BUILD OPENAI REALTIME SESSION CONFIG
# ---------------------------------------------------------

def build_session_config(language_code: str, *, candidate_name=None, job_title=None):
    """
    Build the session.update event that tells OpenAI
    how this realtime interview session should behave.
    """

    noise_profile = os.getenv("TALENTSIFT_NOISE_PROFILE", "near_field")
    if noise_profile not in ("near_field", "far_field"):
        noise_profile = "near_field"

    return {
        "type": "session.update",

        "session": {
            "type": "realtime",

            "audio": {

                # -----------------------------------------
                # CANDIDATE AUDIO COMING INTO OPENAI
                # -----------------------------------------
                "input": {

                    # Browser sends PCM16 at 24 kHz.
                    "format": {
                        "type": "audio/pcm",
                        "rate": MIC_RATE,
                    },

                    # Optional noise reduction for
                    # close microphone speech.
                    "noise_reduction": {
                        "type": noise_profile,
                    },

                    # Ask OpenAI to turn candidate
                    # speech into text.
                    "transcription": build_transcription_settings(
                        language_code, "gpt-transcribe",
                        candidate_name=candidate_name, job_title=job_title,
                    ),

                    # Server-side Voice Activity Detection.
                    #
                    # OpenAI decides when the candidate
                    # starts and stops talking.
                    "turn_detection": build_turn_detection(),
                },


                # -----------------------------------------
                # BIANCA AUDIO
                # -----------------------------------------
                #
                # We configure this now even though we
                # will not forward Bianca audio until
                # the next stage.
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": SPK_RATE,
                    },

                    "voice": "marin",
                },
            },


            # Same interview instructions used by
            # our existing CLI interview.
           "instructions": build_instructions(language_code),
            "tools": [NEXT_TOPIC_TOOL, FINISH_TOOL],

        "tool_choice": "auto",

            },
        }


@app.get("/api/jobs/{job_id}")
def interview_job(job_id: str):
    """Return only the title needed to prepare an interview link."""
    try:
        job = load_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Interview job not found.")
    return {"job_id": job.id, "title": job.title}


@app.post("/api/session")
async def create_session(payload: CreateSessionRequest, request: Request):
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Consent is required before continuing.")
    if payload.language not in ("en", "fr"):
        raise HTTPException(status_code=400, detail="Language must be 'en' or 'fr'.")
    store = app.state.access
    invite_id = request.state.access["invitation_id"]
    await asyncio.to_thread(store.rate_limit, "prepare:" + invite_id, 10, 60)
    session_id, candidate_id = await asyncio.to_thread(store.prepare_interview, invite_id, payload.language)
    recording_token, recording_error = None, False
    if payload.record_audio:
        try:
            recording_token = await asyncio.to_thread(recording_store.prepare, session_id)
        except (OSError, RecordingError, ValueError):
            # Never overwrite an upload or make a recording error stop the interview.
            recording_error = True
    return {"session_id": session_id, "candidate_id": candidate_id,
            "recording_token": recording_token, "recording_error": recording_error,
            "status": "pending", "language": payload.language}


@asynccontextmanager
async def connect_to_engine_with_retry(
    session_config
):
    """
    Connect to OpenAI Realtime and make sure the
    Realtime session is actually ready before yielding
    the WebSocket to the interview.

    We retry STARTUP only.

    We do not automatically reconnect in the middle
    of an interview because that could lose or duplicate
    conversation state.
    """

    max_attempts = 3

    ws_engine = None

    last_error = None


    for attempt in range(
        1,
        max_attempts + 1
    ):

        candidate_ws = None


        try:

            print(
                "🔌 Starting OpenAI Realtime:",
                f"attempt {attempt}/{max_attempts}"
            )


            # -----------------------------------------
            # 1. OPEN THE WEBSOCKET
            # -----------------------------------------

            candidate_ws = await websockets.connect(
                ENGINE_URL,
                additional_headers=ENGINE_HEADERS,
                ping_interval=20,
                ping_timeout=60,
                open_timeout=12,
                close_timeout=3,
            )


            print(
                "🔗 OpenAI WebSocket connected"
            )


            # -----------------------------------------
            # 2. CONFIGURE THE REALTIME SESSION
            # -----------------------------------------

            await candidate_ws.send(
                json.dumps(
                    session_config
                )
            )


            print(
                "⚙️ Realtime session configuration sent"
            )


            # -----------------------------------------
            # 3. WAIT FOR OPENAI TO ACCEPT IT
            # -----------------------------------------
            #
            # A successful WebSocket connection alone
            # does not mean the interview session is ready.
            #
            # We require session.updated before this
            # startup attempt is considered successful.

            while True:

                raw = await asyncio.wait_for(
                    candidate_ws.recv(),
                    timeout=12,
                )


                event = json.loads(
                    raw
                )


                event_type = event.get(
                    "type",
                    "",
                )


                if (
                    event_type
                    == "session.updated"
                ):

                    print(
                        "✅ OpenAI session ready"
                    )


                    ws_engine = (
                        candidate_ws
                    )


                    break


                # If OpenAI explicitly reports an error
                # during startup, retry the entire startup.
                if event_type == "error":

                    raise RuntimeError(
                        "OpenAI Realtime startup error: "
                        + json.dumps(
                            event
                        )
                    )


            # session.updated was received.
            if ws_engine is not None:

                break


        except Exception as error:

            last_error = error


            print(
                "⚠️ OpenAI startup attempt failed:",
                attempt,
            )


            print(
                error
            )


            if candidate_ws is not None:

                try:

                    await candidate_ws.close()

                except Exception:

                    pass


            if is_billing_failure(error):
                # Adding credits/adjusting billing is required; retries cannot fix it.
                raise

            if attempt < max_attempts:

                wait_seconds = attempt


                print(
                    "⏳ Retrying full Realtime startup in",
                    wait_seconds,
                    "second(s)...",
                )


                await asyncio.sleep(
                    wait_seconds
                )


    # ---------------------------------------------
    # ALL THREE STARTUP ATTEMPTS FAILED
    # ---------------------------------------------

    if ws_engine is None:

        if last_error is not None:

            raise last_error


        raise RuntimeError(
            "OpenAI Realtime startup failed."
        )


    # ---------------------------------------------
    # INTERVIEW MAY NOW USE THE READY SOCKET
    # ---------------------------------------------

    try:

        yield ws_engine


    finally:

        try:

            await ws_engine.close()

        except Exception:

            pass

# ---------------------------------------------------------
# BROWSER <-> TALENTSIFT <-> OPENAI
# ---------------------------------------------------------

@app.websocket("/ws/interview/{session_id}")
async def interview(ws_browser: WebSocket, session_id: str):
    store = app.state.access
    grant = ws_browser.state.candidate_access
    try:
        # One atomic admission decision owns the invitation and the quota slot.
        await asyncio.to_thread(store.claim_interview, grant["invitation_id"], session_id)
    except AccessError as error:
        await ws_browser.accept()
        await ws_browser.send_json({"type": "interview_error", "code": "access_denied", "message": str(error)})
        await ws_browser.close(code=1008)
        return
    try:
        async with asyncio.timeout(store.settings.hard_seconds):
            await interview_relay(ws_browser, session_id)
    except TimeoutError:
        try:
            await ws_browser.send_json({"type": "interview_error", "code": "time_limit",
                "message": "This interview reached its time limit. Your saved responses are available to the recruiter."})
            await ws_browser.close(code=1000)
        except Exception:
            pass
    finally:
        # Includes server shutdown/cancellation, startup failure, and normal finish.
        # A used invitation is never silently reset: a new attempt needs a new invitation.
        try:
            current = load_session(session_id)
            if current.status == "in_progress":
                current.status = "failed"
                current.failure_reason = "Interview stopped before completion."
                current.ended_at = utc_now()
                save_session(current)
        finally:
            store.finish_interview(session_id)


async def interview_relay(ws_browser: WebSocket, session_id: str):
    """The established audio relay; only the authenticated route above admits users."""
    # -----------------------------------------------------
    # CONNECTION #1: BROWSER -> FASTAPI
    # -----------------------------------------------------

    # Chrome requested a WebSocket connection.
    # Accept it.
    await ws_browser.accept()

    # -----------------------------------------------------
    # MARK THIS INTERVIEW AS STARTED
    # -----------------------------------------------------

    session = load_session(
        session_id
    )


    session.status = "in_progress"


    if session.started_at is None:

        session.started_at = utc_now()


    save_session(
        session
    )


    print(
        "▶️ Interview started:",
        session.id,
        session.started_at,
    )



    print(
        f"\n🌐 Browser connected: {session_id}"
    )


    # Tell Chrome that TalentSift accepted the connection.
    await ws_browser.send_json(
        {
            "type": "status",
            "value": "connecting_to_engine",
        }
    )

    relay_stats = {
        "phase": "startup", "last_engine_event": None,
        "browser_audio_chunks": 0, "forwarded_audio_chunks": 0,
        "opening_complete": False, "browser_disconnected": False,
    }
    relay_started_at = time.monotonic()

    try:

        # ---------------------------------------------
        # BUILD THE OPENAI SESSION CONFIG ONCE
        # ---------------------------------------------

        session_language = interview_language(session.language)
        candidate = (await asyncio.to_thread(load_candidate, session.candidate_id)
                     if getattr(session, "candidate_id", None) else None)
        job = (await asyncio.to_thread(load_job, session.job_id)
               if getattr(session, "job_id", None) else None)
        recognition_context = {
            "candidate_name": getattr(candidate, "full_name", None),
            "job_title": getattr(job, "title", None),
        }
        session_config = build_session_config(session_language, **recognition_context)


        print(
            "📝 Interview instructions length:",
            len(
                session_config[
                    "session"
                ][
                    "instructions"
                ]
            ),
        )


        print(
            "📝 Interview instructions preview:"
        )


        print(
            session_config[
                "session"
            ][
                "instructions"
            ][:500]
        )


        # ---------------------------------------------
        # CONNECT + CONFIGURE + VERIFY READINESS
        # ---------------------------------------------

        async with connect_to_engine_with_retry(
            session_config
        ) as ws_engine:

            relay_stats["phase"] = "opening"
            print(
                "🤖 Connected to ready OpenAI Realtime session"
            )    

            # =================================================
            # OPENING STATE
            # =================================================
            #
            # Do not let microphone noise or candidate speech
            # trigger another response while Bianca's opening
            # disclosure is still playing.

            interview_state = {
                "opening_complete": False,
                "opening_generated": False,
                "closing_generated": False,
                "closing_response_id": None,
                "awaiting_opening_response": False,
                "opening_response_id": None,
                "opening_audio_items": set(),
                "audio_items_by_response": {},

                # True once the hard 10-minute interview
                # window has been reached.
                "time_limit_reached": False,

                # Becomes True once Bianca has started
                # the intentional finishing process.
                "finish_requested": False,

                # Holds the background timer task.
                "timer_task": None,
            }

            coverage = InterviewCoverage(session_language)
            handled_tool_calls = set()
            interview_state["clock_started_at"] = None

            def candidate_response_options():
                started = interview_state["clock_started_at"]
                remaining = (INTERVIEW_MAX_SECONDS if started is None else
                             INTERVIEW_MAX_SECONDS - (time.monotonic() - started))
                return coverage.response_options(
                    session_config["session"]["instructions"], remaining)

            final_transcripts = FinalTranscripts(
                lambda turn: save_transcript_turn(session_id, turn)
            )
            turns = InterviewTurns(
                ws_engine,
                candidate_response=candidate_response_options,
                allow_reply=lambda: (
                    interview_state["opening_complete"]
                    and not interview_state["finish_requested"]
                    and not interview_state["time_limit_reached"]
                ),
            )

            # A second, optional speech-to-text socket gives the candidate
            # provisional captions. Bianca and saved scoring still use the
            # original Realtime session and its completed transcript.
            live_captions = LiveCaptionRelay(
                ws_browser,
                OPENAI_API_KEY,
                session_language,
                enabled=os.getenv(
                    "TALENTSIFT_LIVE_CAPTIONS", "1"
                ).lower() not in ("0", "false", "off"),
                **recognition_context,
            )


            # =================================================
            # INTERVIEW TIME LIMIT
            # =================================================

            async def enforce_interview_time_limit():
                """
                Enforce TalentSift's maximum interview window.

                The clock begins only after Bianca's opening
                has finished playing.
                """

                try:

                    # -----------------------------------------
                    # WAIT UNTIL ONE MINUTE REMAINS
                    # -----------------------------------------

                    await asyncio.sleep(
                        INTERVIEW_MAX_SECONDS
                        - INTERVIEW_WARNING_SECONDS
                    )


                    # Bianca may already have completed the
                    # interview naturally.
                    if interview_state[
                        "finish_requested"
                    ]:

                        return


                    print(
                        "⏳ Interview has 1 minute remaining"
                    )


                    await ws_browser.send_json(
                        {
                            "type":
                                "time_warning",

                            "remaining_seconds":
                                INTERVIEW_WARNING_SECONDS,
                        }
                    )


                    # -----------------------------------------
                    # WAIT FOR THE FINAL MINUTE
                    # -----------------------------------------

                    await asyncio.sleep(
                        INTERVIEW_WARNING_SECONDS
                    )


                    if interview_state[
                        "finish_requested"
                    ]:

                        return


                    # -----------------------------------------
                    # HARD LIMIT REACHED
                    # -----------------------------------------

                    interview_state[
                        "time_limit_reached"
                    ] = True

                    interview_state[
                        "finish_requested"
                    ] = True

                    print(
                        "⏰ Interview time limit reached"
                    )


                    await ws_browser.send_json(
                        {
                            "type":
                                "time_limit_reached",
                        }
                    )


                    # Ask Bianca to use the EXISTING
                    # finish_interview tool.
                    #
                    # This means the normal TalentSift
                    # fixed closing still owns the ending.
                    await turns.control_response(
                        "deadline",
                        {"instructions": (
                            "The maximum TalentSift interview time has now been reached. "
                            "Do not ask another interview question. Call the finish_interview "
                            "tool immediately. Do not give a score or hiring decision."
                        ), "tools": [FINISH_TOOL],
                           "tool_choice": {"type": "function", "name": "finish_interview"}},
                        interrupt=True,
                    )


                except asyncio.CancelledError:

                    # Normal case when Bianca finishes the
                    # interview before the time limit.
                    print(
                        "⏱️ Interview timer stopped"
                    )            

            # =================================================
            # PUMP #1
            # BROWSER -> OPENAI
            # =================================================

            audio_budget = AudioBudget()

            async def browser_to_engine():
                """
                Continuously move candidate microphone audio
                from Chrome to OpenAI.
                """

                try:

                    while True:

                        # ---------------------------------
                        # WAIT FOR BROWSER MESSAGE
                        # ---------------------------------

                        raw_message = (
                            await ws_browser.receive_text()
                        )


                        # JSON text -> Python dictionary.
                        if len(raw_message) > 131072:
                            raise AccessError("Interview message is too large.")
                        msg = json.loads(raw_message)
                        if not isinstance(msg, dict):
                            raise AccessError("Invalid interview message.")


                        # Ask:
                        # "What kind of TalentSift message
                        # did the browser send?"
                        msg_type = msg.get(
                            "type",
                            "",
                        )


                        # ============================================
                        # CANDIDATE MICROPHONE AUDIO
                        # ============================================

                        if msg_type == "audio":

                            audio_b64 = msg.get("data")



                            if audio_b64:
                                relay_stats["browser_audio_chunks"] += 1

                                # During Bianca's opening disclosure,
                                # ignore microphone audio.
                                #
                                # This prevents VAD from creating another
                                # Bianca response at the same time as the
                                # manually-created opening response.
                                if (
                                    not interview_state[
                                    "opening_complete"
                                    ] or interview_state[
                                            "time_limit_reached"
                                    ]
                                     or interview_state[
                                        "finish_requested"
                                    ]
                                ):
                            
                                    continue


                                audio_budget.accept(audio_b64)
                                await ws_engine.send(
                                    json.dumps(
                                        {
                                            "type":
                                                "input_audio_buffer.append",

                                            "audio":
                                                audio_b64,
                                        }
                                    )
                                )
                                relay_stats["forwarded_audio_chunks"] += 1
                                if relay_stats["forwarded_audio_chunks"] == 1:
                                    print("[audio] First candidate microphone chunk forwarded to OpenAI")
                                live_captions.offer_audio(audio_b64)



                        elif (
                            msg_type
                            == "opening_playback_finished"
                        ):
                            if (not interview_state["opening_generated"]
                                    or interview_state["opening_complete"]
                                    or msg.get("response_id") != interview_state["opening_response_id"]):
                                continue

                            # The candidate may have clicked
                            # End Interview while Bianca's opening
                            # was still playing.
                            #
                            # In that case a delayed browser timer
                            # may still report that the opening
                            # finished. Do not restart the interview.
                            if interview_state[
                                "finish_requested"
                            ]:

                                print(
                                    "⏭️ Ignoring late opening finish "
                                    "because interview is already ending"
                                )

                                continue

                            interview_state[
                                "opening_complete"
                            ] = True
                            relay_stats["opening_complete"] = True
                            relay_stats["phase"] = "listening"


                            print(
                                "🎙️ Opening finished — candidate audio enabled"
                            )

                            # -----------------------------------------
                            # START THE INTERVIEW CLOCK
                            # -----------------------------------------
                            #
                            # Protect against the browser accidentally
                            # sending this message twice.

                            if (
                                interview_state[
                                    "timer_task"
                                ]
                                is None
                            ):

                                interview_state["clock_started_at"] = time.monotonic()
                                interview_state[
                                    "timer_task"
                                ] = asyncio.create_task(
                                    enforce_interview_time_limit()
                                )


                                print(
                                    "⏱️ 10-minute interview timer started"
                                )


                                # Tell the browser the official
                                # TalentSift interview clock has started.
                                await ws_browser.send_json(
                                    {
                                        "type":
                                            "interview_timer_started",

                                        "total_seconds":
                                            INTERVIEW_MAX_SECONDS,
                                    }
                                )



                            await ws_browser.send_json(
                                {
                                    "type": "status",
                                    "value": "listening",
                                }
                            )

                        # ============================================
                        # CANDIDATE ENDED INTERVIEW EARLY
                        # ============================================

                        elif (
                            msg_type
                            == "end_interview"
                        ):

                            print(
                                "🛑 Candidate ended interview early"
                            )
                            turns.close()


                            # Prevent any more normal Bianca
                            # questions from being created.
                            interview_state[
                                "finish_requested"
                            ] = True


                            # Stop the interview timer.
                            timer_task = interview_state[
                                "timer_task"
                            ]


                            if (
                                timer_task
                                and not timer_task.done()
                            ):

                                timer_task.cancel()


                            # Load the real Session.
                            ended_session = load_session(
                                session_id
                            )


                            # This is NOT a normal completion.
                            ended_session.status = (
                                "ended_early"
                            )


                            ended_session.ended_at = utc_now()


                            save_session(
                                ended_session
                            )


                            print(
                                "✅ Session marked ended_early:",
                                ended_session.id,
                            )


                            # Tell the candidate browser that
                            # persistence succeeded.
                            await ws_browser.send_json(
                                {
                                    "type":
                                        "interview_ended_early",
                                }
                            )

                        # ============================================
                        # BIANCA WAS INTERRUPTED
                        # ============================================

                        elif (
                            msg_type
                            == "playback_interrupted"
                        ):

                            # Which Bianca message was interrupted?
                            item_id =  msg.get(
                                                                "item_id"
                                                            )



                            # Which content block?
                            # For our current audio this should
                            # normally be 0.
                            content_index =  msg.get(
                                                                "content_index",
                                                                0,
                                                            )



                            # How many milliseconds did Chrome
                            # actually play to the candidate?
                            played_ms = msg.get(
                                                                "played_ms"
                                                            )



                            print(
                                "✂️ Browser reports interruption:",
                                item_id,
                                played_ms,
                                "ms",
                            )


                            # Only send truncation if we actually
                            # received the required information.
                            if (
                                item_id
                                and played_ms is not None
                            ):

                                truncate_event = {
                                    "type":
                                        "conversation.item.truncate",

                                    "item_id":
                                        item_id,

                                    "content_index":
                                        int(
                                            content_index
                                        ),

                                    "audio_end_ms":
                                        max(
                                            0,
                                            int(
                                                played_ms
                                            ),
                                        ),
                                }


                                await ws_engine.send(
                                    json.dumps(
                                        truncate_event
                                    )
                                )


                                print(
                                    "✂️ Sent truncation:",
                                    item_id,
                                    played_ms,
                                    "ms",
                                )

                        # ============================================
                        # FIXED CLOSING FINISHED PLAYING
                        # ============================================

                        elif (
                            msg_type
                            == "closing_playback_finished"
                        ):

                            if (not interview_state["closing_generated"]
                                    or msg.get("response_id") != interview_state["closing_response_id"]):
                                continue  # Only the server may initiate interview completion.
                            print(
                                "🏁 Candidate reached end of closing"
                            )


                            # Load the real Session from SQLite.
                            completed_session = load_session(
                                session_id
                            )


                            # Mark the intentional interview completion.
                            completed_session.status = "completed"


                            # Record when the interview finished.
                            completed_session.ended_at = utc_now()


                            # Save the updated Session.
                            save_session(
                                completed_session
                            )


                            print(
                                "✅ Session completed:",
                                completed_session.id,
                                completed_session.ended_at,
                            )

                            
                            # Tell the browser it can now move
                            # to the Done screen.
                            await ws_browser.send_json(
                                {
                                    "type":
                                        "interview_complete",
                                }
                            )


                            try:

                                # The voice can answer before ASR finishes. Scoring
                                # must still wait for every committed final transcript.
                                await final_transcripts.wait_for_scoring()
                                scorecard, scorecard_path = (
                                    await asyncio.to_thread(
                                        score_database_session,
                                        session_id,
                                            )
                                        )


                                print(
                                    "📊 Scorecard created:",
                                    scorecard.overall,
                                    "saved to:",
                                    scorecard_path,
                                )


                            except Exception as error:

                        
                                print(
                                    "❌ Scorecard creation failed:"
                                )

                                print(
                                    error
                                )


                                # Mark an active interview as failed
                                # when the interview relay crashes.
                                failed_session = load_session(
                                    session_id
                                )

                                if failed_session.status == "in_progress":

                                    failed_session.status = "failed"

                                    failed_session.failure_reason = (
                                        f"Interview relay error: {error}"
                                    )

                                    failed_session.ended_at = utc_now()

                                    save_session(
                                        failed_session
                                    )

                                    print(
                                        "⚠️ Session marked failed:",
                                        failed_session.id,
                                        failed_session.failure_reason,
                                    )

                            return

                        else:

                            print(
                                "⚠️ Unknown browser "
                                f"message: {msg_type}"
                            )


                except WebSocketDisconnect:
                    relay_stats["browser_disconnected"] = True
                    print(
                        "🔌 Browser disconnected"
                    )


                    return



            # =================================================
            # PUMP #2
            # OPENAI -> BROWSER
            # =================================================

            async def engine_to_browser():
                """
                Continuously listen for events from OpenAI.
                """

                # -------------------------------------------------
                # CLOSING RESPONSE STATE
                # -------------------------------------------------
                awaiting_closing_response = False          
                closing_response_id = None
                async for raw in ws_engine:

                    # OpenAI JSON text
                    # -> Python dictionary.
                    event = json.loads(raw)


                    relay_stats["last_engine_event"] = event.get("type")
                    # Read OpenAI's event type.
                    event_type = event.get(
                        "type",
                        "",
                    )


                    # ---------------------------------
                    # OPENAI ACCEPTED OUR SESSION
                    # ---------------------------------

                    if (
                        event_type
                        == "session.updated"
                    ):

                        # Initial session.updated is now consumed
                        # by connect_to_engine_with_retry().
                        #
                        # If another update acknowledgement ever
                        # arrives later, it must NOT start another
                        # Bianca opening.
                        print(
                            "ℹ️ Additional session.updated received"
                        )

                    elif event_type == "response.created":

                        if not await turns.response_created(event.get("response", {})):
                            continue
                        coverage.response_created(event.get("response", {}).get("id"))
                        print(
                            "🤖 New Bianca response started"
                        )


                        # Get the response OpenAI just created.
                        response_data = event.get(
                            "response",
                            {},
                        )


                        response_id = response_data.get(
                            "id"
                        )


                        # -----------------------------------------
                        # IS THIS THE OPENING RESPONSE?
                        # -----------------------------------------
                        #
                        # session.updated sets this flag immediately
                        # before TalentSift manually requests Bianca's
                        # opening.
                        #
                        # Therefore the next response.created event
                        # belongs to the opening.

                        if interview_state[
                            "awaiting_opening_response"
                        ]:

                            interview_state[
                                "opening_response_id"
                            ] = response_id


                            interview_state[
                                "awaiting_opening_response"
                            ] = False


                            print(
                                "🎬 Opening response started:",
                                response_id,
                            )


                        # -----------------------------------------
                        # IS THIS THE FIXED CLOSING RESPONSE?
                        # -----------------------------------------

                        if awaiting_closing_response:

                            closing_response_id = response_id
                            interview_state["closing_response_id"] = response_id

                            awaiting_closing_response = False


                            print(
                                "🎬 Closing response started:",
                                closing_response_id,
                            )


                        # Tell the browser that OpenAI has begun
                        # producing another Bianca response.
                        await ws_browser.send_json(
                            {
                                "type": "response_started",
                            }
                        )


                    # ---------------------------------
                    # OPENAI FINISHED A RESPONSE
                    # ---------------------------------

                    elif event_type == "response.done":

                        # Final response content also settles canceled/empty audio
                        # items, so transcript storage cannot wait on them forever.
                        for item in event.get("response", {}).get("output", []):
                            if item.get("type") == "message" and item.get("role") == "assistant":
                                text = " ".join(
                                    (part.get("transcript") or "")
                                    for part in item.get("content", [])
                                ).strip()
                                final_transcripts.complete(
                                    item.get("id"),
                                    TranscriptTurn(speaker="interviewer", text=text, item_id=item["id"])
                                    if text else None,
                                )
                        coverage.response_done(
                            event.get("response", {}),
                            allowed=turns.response_allowed({
                                "response_id": event.get("response", {}).get("id")}),
                        )
                        await turns.response_done(event.get("response", {}))
                        response_data = event.get(
                            "response",
                            {},
                        )


                        response_id = response_data.get(
                            "id"
                        )

                        await ws_browser.send_json({
                            "type": "response_finished", "response_id": response_id,
                            "status": response_data.get("status"),
                            "item_ids": sorted(interview_state["audio_items_by_response"].get(response_id, set())),
                        })


                        if (
                        interview_state[
                                "opening_response_id"
                            ]
                            and response_id
                                == interview_state[
                                    "opening_response_id"
                                ]
                        ):

                            if response_data.get("status") != "completed":
                                raise RuntimeError("Bianca's opening did not complete successfully")
                            if not interview_state["opening_audio_items"]:
                                raise RuntimeError("Bianca's opening contained no playable audio")
                            interview_state["opening_generated"] = True
                            print(
                                "✅ Opening response fully generated"
                            )


                            await ws_browser.send_json(
                                {
                                    "type":
                                        "opening_generated",
                                    "response_id": response_id,
                                    "item_ids": sorted(interview_state["opening_audio_items"]),
                                }
                            )

                            # Keep the ID to reject stale or duplicate playback acknowledgements.



                        # Is this the closing response we saved earlier?
                        if (
                            closing_response_id
                            and response_id == closing_response_id
                        ):

                            if response_data.get("status") != "completed":
                                raise RuntimeError("Bianca's closing did not complete successfully")
                            closing_items = interview_state["audio_items_by_response"].get(response_id, set())
                            if not closing_items:
                                raise RuntimeError("Bianca's closing contained no playable audio")

                            interview_state["closing_generated"] = True
                            print(
                                "✅ Closing response fully generated"
                            )


                            # Tell Chrome:
                            #
                            # "No more closing audio chunks are coming."
                            #
                            # Chrome still needs to finish PLAYING
                            # the audio already queued locally.
                            await ws_browser.send_json(
                                {
                                    "type":
                                        "closing_generated",
                                    "response_id": response_id,
                                    "item_ids": sorted(closing_items),
                                }
                            )


                    # ---------------------------------
                    # BIANCA CALLED A TALENTSIFT TOOL
                    # ---------------------------------

                    elif (
                        event_type
                        == "response.output_item.done"
                    ):
                        # A canceled answer must not end an interview while
                        # the candidate continues speaking.
                        if not turns.response_allowed(event):
                            continue
                        # So first inspect what kind of item it is.
                        item = event.get(
                            "item",
                            {},
                        )


                        if item.get("type") != "function_call":
                            continue
                        name = item.get("name")
                        if name not in ("next_interview_topic", "finish_interview"):
                            continue
                        call_id = item.get("call_id")
                        if not call_id or call_id in handled_tool_calls:
                            continue
                        handled_tool_calls.add(call_id)
                        if closing_response_id or awaiting_closing_response:
                            continue

                        at_deadline = interview_state["time_limit_reached"]
                        if name == "next_interview_topic" or (not at_deadline and not coverage.can_finish):
                            # The model cannot mark arbitrary topics complete. A
                            # prescribed spoken question and a subsequent candidate
                            # audio turn are required before advancing one topic.
                            if not at_deadline:
                                coverage.advance()
                            result = {
                                "finish_accepted": False,
                                "action": "finish_interview" if at_deadline else "continue_question_plan",
                                **coverage.progress(),
                            }
                            print("[coverage]", json.dumps(result))
                            await ws_engine.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {"type": "function_call_output", "call_id": call_id,
                                         "output": json.dumps(result)},
                            }))
                            if at_deadline:
                                await turns.control_response("deadline", {
                                    "instructions": "Time is up. Call finish_interview immediately.",
                                    "tools": [FINISH_TOOL],
                                    "tool_choice": {"type": "function", "name": "finish_interview"},
                                })
                            else:
                                # A normal tool continuation obeys the same VAD,
                                # interruption and single-response rules as a reply.
                                await turns.control_response("candidate")
                            continue

                        if name == "finish_interview":
                            if coverage.can_finish and coverage.current:
                                coverage.advance()
                            print("[coverage] finish", json.dumps({
                                "reason": "time_limit" if at_deadline else "all_topics",
                                **coverage.progress(),
                            }))
                            interview_state["finish_requested"] = True
                            timer_task = interview_state["timer_task"]
                            if timer_task and not timer_task.done():
                                timer_task.cancel()

                            # -----------------------------------------
                            # LOAD THE REAL TALENTSIFT SESSION
                            # -----------------------------------------
                            # load_session() reads that Session
                            # from SQLite.
                            current_session = load_session(
                                session_id
                            )


                            # -----------------------------------------
                            # CHOOSE THE FIXED CLOSING
                            # -----------------------------------------

                            closing_text = CLOSING_MESSAGES.get(
                                session_language,
                                CLOSING_MESSAGES["en"],
                            )


                            # -----------------------------------------
                            # RETURN TOOL RESULT TO OPENAI
                            # -----------------------------------------
                            #
                            # Bianca asked:
                            #
                            # finish_interview()
                            #
                            # Our backend says:
                            #
                            # "Accepted. The closing can now happen."
                            await ws_engine.send(
                                json.dumps(
                                    {
                                        "type":
                                            "conversation.item.create",

                                        "item": {
                                            "type":
                                                "function_call_output",

                                            "call_id":
                                                call_id,

                                            "output":
                                                "Interview completion accepted.",
                                        },
                                    }
                                )
                            )


                            # -----------------------------------------
                            # ASK BIANCA TO READ OUR FIXED CLOSING
                            # -----------------------------------------
                            #
                            # Important:
                            #
                            # closing_text came from OUR
                            # CLOSING_MESSAGES dictionary above.
                            #
                            # We are not asking Bianca to invent
                            # a closing.
                            awaiting_closing_response = True
                            await turns.control_response(
                                "closing",
                                {
                                    "instructions": (
                                        "Read the following closing message exactly as written. "
                                        "Do not add, remove, or change any words:\n\n" + closing_text
                                    ),
                                    "tools": [], "tool_choice": "none",
                                },
                            )


                            print(
                                "🎬 Fixed TalentSift closing requested"
                            )



                    elif (
                        event_type
                        == "conversation.item.truncated"
                    ):

                        print(
                            "✅ OpenAI truncated Bianca item "
                            f"{event.get('item_id')} "
                            f"at {event.get('audio_end_ms')} ms"
                        )

                    # ---------------------------------
                    # BIANCA AUDIO CHUNK
                    # ---------------------------------

                    elif (
                        event_type
                        == "response.output_audio.delta"
                    ):

                        # OpenAI sends Bianca's voice in many
                        # small Base64-encoded PCM16 chunks.
                        #
                        # "delta" simply means:
                        # one small piece of the full response.
                        if not turns.allow_audio(event):
                            continue
                        final_transcripts.expect(event.get("item_id"))
                        audio_b64 = event.get(
                            "delta",
                            "",
                        )


                        if audio_b64:
                            if not event.get("item_id") or not event.get("response_id"):
                                raise RuntimeError("Bianca audio did not include its message identity")
                            interview_state["audio_items_by_response"].setdefault(
                                event["response_id"], set()).add(event["item_id"])
                            if event.get("response_id") == interview_state["opening_response_id"]:
                                interview_state["opening_audio_items"].add(event.get("item_id"))

                            # Translate OpenAI's event:
                            #
                            # response.output_audio.delta
                            #
                            # into our simpler TalentSift
                            # browser message:
                            #
                            # {
                            #     "type": "audio",
                            #     "data": "..."
                            # }
                            await ws_browser.send_json(
                                {
                                    "type": "audio",
                                    "data": audio_b64,

                                     # Identifies WHICH Bianca message
                                    # this audio belongs to.
                                    "item_id": event.get("item_id"),
                                    "response_id": event.get("response_id"),

                                    # Normally 0 for the audio content
                                    # we are currently using.
                                    "content_index": event.get(
                                        "content_index",
                                        0,
                                        ),
                                }
                            )

                    # ---------------------------------
                    # LIVE BIANCA TRANSCRIPT PIECE
                    # ---------------------------------
                    #
                    elif event_type == "response.output_audio_transcript.delta":
                        if turns.response_allowed(event) and event.get("delta"):
                            await ws_browser.send_json({
                                "type": "transcript_delta", "speaker": "interviewer",
                                "text": event["delta"], "item_id": event.get("item_id"),
                                "response_id": event.get("response_id"),
                            })

                    # ---------------------------------
                    # BIANCA COMPLETED TRANSCRIPT
                    # ---------------------------------

                    elif (
                        event_type
                        == "response.output_audio_transcript.done"
                    ):

                        # This is the full text corresponding
                        # to Bianca's spoken response.
                        interviewer_text = event.get(
                            "transcript",
                            "",
                        ).strip()


                        if interviewer_text:

                            print(
                                "\n🤖 Bianca:",
                                interviewer_text,
                            )

                            # ---------------------------------------------
                            # SAVE FINAL BIANCA TRANSCRIPT TO SQLITE
                            # ---------------------------------------------

                            interviewer_turn = TranscriptTurn(
                                speaker="interviewer",
                                text=interviewer_text,
                                item_id=event.get("item_id"),
                            )


                            final_transcripts.complete(event.get("item_id"), interviewer_turn)


                            print(
                                "💾 Final Bianca transcript queued for ordered storage"
                            )


                            # Send the completed interviewer
                            # transcript to Chrome.
                            await ws_browser.send_json(
                                {
                                    "type": "transcript",
                                    "speaker": "interviewer",
                                    "text": interviewer_text,
                                    "item_id": event.get("item_id"),
                                    "response_id": event.get("response_id"),
                                }
                            )

                    # ---------------------------------
                    # LIVE CANDIDATE TRANSCRIPT PIECE
                    # ---------------------------------

                    elif (
                        event_type
                        == (
                            "conversation.item."
                            "input_audio_"
                            "transcription.delta"
                        )
                    ):

                        # "delta" is only the newest little piece
                        # of transcription.
                        #
                        # Example pieces might be:
                        #
                        # "I"
                        # " built"
                        # " a"
                        # " Flask"
                        delta_text = event.get(
                            "delta",
                            "",
                        )


                        if delta_text and not live_captions.ready:

                            # Send this small live fragment
                            # straight to the browser.
                            await ws_browser.send_json(
                                {
                                    "type": "transcript_delta",
                                    "speaker": "candidate",
                                    "text": delta_text,
                                    "item_id": event.get("item_id"),
                                }
                            )
                    # ---------------------------------
                    # CANDIDATE TRANSCRIPTION
                    # ---------------------------------

                    elif (
                        event_type
                        == (
                            "conversation.item."
                            "input_audio_"
                            "transcription.completed"
                        )
                    ):

                        candidate_text = (
                            event.get(
                                "transcript",
                                "",
                            ).strip()
                        )

                        live_captions.final_transcript_arrived(
                            event.get("item_id")
                        )
                        if not candidate_text:
                            final_transcripts.complete(event.get("item_id"))
                            # Remove a provisional caption if the main
                            # transcriber found no usable candidate speech.
                            await ws_browser.send_json({
                                "type": "transcript_retract",
                                "item_id": event.get("item_id"),
                            })

                        if candidate_text:

                            print(
                                "\n📝 Candidate:",
                                candidate_text,
                            )

                            # ---------------------------------------------
                            # SAVE FINAL CANDIDATE TRANSCRIPT TO SQLITE
                            # ---------------------------------------------

                            candidate_turn = TranscriptTurn(
                                speaker="candidate",
                                text=candidate_text,
                                item_id=event.get("item_id"),
                            )


                            final_transcripts.complete(event.get("item_id"), candidate_turn)


                            print(
                                "💾 Final candidate transcript queued for ordered storage"
                            )


                            # Translate the complicated
                            # OpenAI event into our simpler
                            # TalentSift browser protocol.
                            await ws_browser.send_json(
                                {
                                    "type":
                                        "transcript",

                                    "speaker":
                                        "candidate",

                                    "text":
                                        candidate_text,

                                    # Same ID used by the live deltas.
                                    # This lets the browser replace/finalize
                                    # the correct candidate answer.
                                    "item_id": event.get("item_id"),
                                }
                            )

                            # Final transcription is only for display and storage.
                            # Replies use committed native audio below; a late ASR
                            # result must never start speech over a newer answer.

                    elif event_type == "input_audio_buffer.committed":
                        final_transcripts.expect(event.get("item_id"))
                        coverage.audio_committed(event.get("item_id"))
                        await turns.audio_committed(event.get("item_id"))

                    elif event_type == "conversation.item.input_audio_transcription.failed":
                        final_transcripts.complete(event.get("item_id"), failed=True)
                        live_captions.final_transcript_arrived(event.get("item_id"))
                        print("[turn] Candidate transcription failed; voice response remains independent.")

                    elif (
                        event_type
                        == "input_audio_buffer.speech_started"
                    ):
                        coverage.speech_started(event.get("item_id"))
                        await turns.speech_started(event.get("item_id"))
                        # Stop queued playback when the engine cancels its response.
                        await ws_browser.send_json({"type": "interrupt", "response_id": turns.active_id})
                        await live_captions.speech_started(event.get("item_id"))
                        print("🎤 Candidate started speaking")
                        await ws_browser.send_json({"type": "status", "value": "listening"})
                    # ---------------------------------
                    # VAD: CANDIDATE STOPPED SPEAKING
                    # ---------------------------------

                    elif (
                        event_type
                        == (
                            "input_audio_buffer."
                            "speech_stopped"
                        )
                    ):
                        await turns.speech_stopped(event.get("item_id"))
                        live_captions.speech_stopped(event.get("item_id"))
                        print("🛑 VAD detected end of candidate turn")
                        await ws_browser.send_json({"type": "status", "value": "processing"})

                    # ---------------------------------
                    # OPENAI ERROR
                    # ---------------------------------

                    elif event_type == "error":

                        turns.response_error(event.get("error", {}))
                        print(
                            "\n❌ OpenAI error:"
                        )

                        print(
                            json.dumps(
                                event,
                                indent=2,
                            )
                        )



            # =================================================
            # READY -> START BIANCA'S OPENING
            # =================================================
            #
            # At this point:
            #
            # 1. OpenAI WebSocket is connected.
            # 2. session.update was sent.
            # 3. OpenAI acknowledged it with session.updated.
            #
            # Only now do we tell the browser that the engine
            # is ready and request Bianca's opening.

            await ws_browser.send_json(
                {
                    "type": "status",
                    "value": "engine_ready",
                }
            )


            interview_state[
                "awaiting_opening_response"
            ] = True


            await turns.control_response("opening")


            print(
                "🎬 Bianca opening requested"
            )

            live_captions.start()

            # =================================================
            # RUN BOTH PUMPS AT THE SAME TIME
            # =================================================

            try:
                await run_relay_pair(
                    browser_to_engine(), engine_to_browser(), name="interview"
                )
                current_status = load_session(session_id).status
                if current_status == "in_progress":
                    if relay_stats["browser_disconnected"]:
                        raise RuntimeError("Candidate browser disconnected during the interview")
                    raise RuntimeError("OpenAI Realtime connection closed during the interview")
            finally:
                turns.close()
                timer_task = interview_state["timer_task"]
                if timer_task and not timer_task.done():
                    timer_task.cancel()
                if timer_task:
                    await asyncio.gather(timer_task, return_exceptions=True)
                await live_captions.stop()
                # Completing one unresolved item may flush later ready entries.
                missing_items = [
                    item_id for item_id, entry in final_transcripts.entries.items()
                    if not entry[0]
                ]
                for item_id in missing_items:
                    final_transcripts.complete(item_id, failed=True)


    except Exception as error:
        details = connection_failure_details(error)
        details.update(relay_stats)
        details["session_id"] = session_id
        audio_rate_failure = isinstance(error, AudioRateError)
        if audio_rate_failure:
            details.update(error.details)
        details["elapsed_ms"] = round((time.monotonic() - relay_started_at) * 1000)
        print("[relay] failed " + json.dumps(details, ensure_ascii=False))
        billing = is_billing_failure(error)
        failure_code = ("audio_rate_exceeded" if audio_rate_failure else
                        "billing_unavailable" if billing else "connection_lost")
        try:
            failed_session = load_session(session_id)
            if failed_session.status == "in_progress":
                failed_session.status = "failed"
                failed_session.failure_reason = (
                    "Microphone audio exceeded the allowed streaming rate."
                    if audio_rate_failure else
                    "Interview service billing is unavailable."
                    if billing else "Interview connection ended unexpectedly."
                )
                failed_session.ended_at = utc_now()
                save_session(failed_session)
        except Exception as storage_error:
            print("[relay] Could not persist failed session:", type(storage_error).__name__)

        if not relay_stats["browser_disconnected"]:
            try:
                await asyncio.wait_for(ws_browser.send_json({
                    "type": "interview_error", "code": failure_code,
                    "message": (
                        "Microphone audio was arriving faster than the interview could accept. This interview has stopped. Refresh the page and contact the recruiter for a new invitation."
                        if audio_rate_failure else
                        "The interview service is unavailable. Please contact the recruiter or try later."
                        if billing else
                        "The connection to the interviewer was lost. This interview has stopped. Contact the recruiter for a new invitation."
                    ),
                }), timeout=3)
            except Exception:
                pass  # The browser may already have disconnected.
            try:
                await asyncio.wait_for(
                    ws_browser.close(code=1008 if audio_rate_failure else 1011,
                                     reason="Microphone streaming rate exceeded" if audio_rate_failure else "Interview service unavailable"), timeout=3
                )
            except Exception:
                pass
    else:
        try:
            await ws_browser.close(code=1000, reason="Interview finished")
        except Exception:
            pass


def job_title_or_none(job_id: str) -> str | None:
    """Legacy interview records may not have a persisted job title."""
    try:
        return load_job(job_id).title
    except KeyError:
        return None


@app.get("/recruiter/jobs")
def recruiter_jobs(request: Request):
    """Create a job and view the IDs and links already in the database."""
    return templates.TemplateResponse(
        request=request,
        name="recruiter_jobs.html",
        context={"jobs": list_jobs(), "usage": app.state.access.usage()},
    )


@app.post("/recruiter/jobs")
def recruiter_create_job(title: str = Form(...)):
    """Generate and persist a new job ID from a recruiter-entered title."""
    clean_title = " ".join(title.split())
    if not clean_title or len(clean_title) > 120:
        raise HTTPException(status_code=422, detail="Enter a job title (up to 120 characters).")
    job = JobListing(title=clean_title)
    save_job(job)
    return RedirectResponse(url=f"/recruiter/job/{job.id}", status_code=303)


@app.get("/recruiter/job/{job_id}")
def recruiter_job_dashboard(
    request: Request,
    job_id: str,
):
    """
    Render the recruiter dashboard for one job.
    """

    snapshot = build_job_snapshot(
        job_id
    )


    return templates.TemplateResponse(
        request=request,
        name="recruiter_leaderboard.html",
        context={
            "job_id":
                job_id,

            "job_title": job_title_or_none(job_id),
            "invitations": app.state.access.list_invitations(job_id),
            "usage": app.state.access.usage(),

            "snapshot":
                snapshot,
        },
    )


@app.get("/recruiter/job/{job_id}/fairness")
def recruiter_fairness_dashboard(
    request: Request,
    job_id: str,
):
    """
    Show descriptive fairness-monitoring statistics
    for one job.
    """

    summary = build_fairness_summary(
        job_id
    )


    return templates.TemplateResponse(
        request=request,
        name="recruiter_fairness.html",
        context={
            "job_id":
                job_id,

            "job_title": job_title_or_none(job_id),

            "summary":
                summary,
        },
    )



@app.get("/recruiter/session/{session_id}")
def recruiter_session_detail(
    request: Request,
    session_id: str,
):
    """
    Show the full recruiter audit view
    for one interview Session.
    """

    detail = build_session_detail(
        session_id
    )

    return templates.TemplateResponse(
        request=request,
        name="recruiter_session.html",
        context={
            "detail": detail,
            "recording": recording_store.details(session_id),
            "job_title": job_title_or_none(detail["job_id"]),
        },
    )

@app.post("/recruiter/session/{session_id}/override")
def recruiter_override(
    request: Request,
    session_id: str,
    score: float = Form(...),
    reason: str = Form(...),
):
    """
    Apply a recruiter override to one Scorecard.
    """

    apply_recruiter_override(
        session_id=session_id,
        score=score,
        reason=reason,
        overridden_by=request.state.access["username"],
    )

    return RedirectResponse(
        url=f"/recruiter/session/{session_id}",
        status_code=303,
    )


@app.post("/recruiter/session/{session_id}/restore-ai")
def recruiter_restore_ai_score(
    request: Request,
    session_id: str,
    reason: str = Form(...),
):
    """
    Restore the original AI overall as the
    effective ranking score.
    """

    restore_ai_score(
        session_id=session_id,
        reason=reason,
        restored_by=request.state.access["username"],
    )


    return RedirectResponse(
        url=f"/recruiter/session/{session_id}",
        status_code=303,
    )

@app.get("/recruiter/session/{session_id}/transcript.txt")
def download_transcript(
    session_id: str,
):
    """turn_detection

    Download the original interview transcript
    as a plain text file.
    """

    detail = build_session_detail(
        session_id
    )

    lines = [
        "TalentSift Interview Transcript",
        "",
        f"Candidate: {detail['candidate']['full_name'] or 'Not provided'}",
        f"Candidate ID: {detail['candidate']['id']}",
        f"Job: {job_title_or_none(detail['job_id']) or detail['job_id']}",
        f"Job ID: {detail['job_id']}",
        f"Session ID: {detail['session_id']}",
        f"Language: {detail['interview']['language']}",
        f"Started: {detail['interview']['started_at'] or '—'}",
        f"Ended: {detail['interview']['ended_at'] or '—'}",
        "",
        "-" * 60,
        "",
    ]

    for turn in detail["transcript"]:

        speaker = (
            "Bianca"
            if turn["speaker"] == "interviewer"
            else "Candidate"
        )

        timestamp = (
            turn["timestamp"]
            or "No timestamp"
        )

        lines.append(
            f"{speaker} [{timestamp}]"
        )

        lines.append(
            turn["text"]
        )

        lines.append("")

    transcript_text = "\n".join(
        lines
    )

    filename = (
        f"talentsift-transcript-{session_id}.txt"
    )

    return PlainTextResponse(
        content=transcript_text,
        headers={
            "Content-Disposition":
                f'attachment; filename="{filename}"'
        },
    )

# ---------------------------------------------------------
# SERVE FRONTEND FILES
# ---------------------------------------------------------

install_access_routes(app, templates)

app.mount(
    "/",
    StaticFiles(
        directory="client",
        html=True,
    ),
    name="client",
)
