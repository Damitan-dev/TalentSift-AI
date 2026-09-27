import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import websockets

from models import (
    Session,
    TranscriptTurn,
    utc_now,
)
from pydantic import BaseModel
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

app = FastAPI()

# Make sure the SQLite tables exist whenever
# TalentSift starts.
initialize_database()


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

# Bianca decides WHEN the interview is finished.
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

When the required interview areas have been covered,
or there are no more substantive questions that would
reasonably improve the interview evidence, call the
finish_interview tool.

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

INTERVIEW_MAX_SECONDS = 10 * 60

INTERVIEW_WARNING_SECONDS = 60

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
    language_name = LANGUAGE_NAMES.get(
        language_code,
        "English",
    )


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
    """
    Data required to create a new interview session.
    """

    # The candidate must already exist in our
    # candidates table.
    candidate_id: str

    # The job this interview belongs to.
    job_id: str

    # Candidate's chosen interview language.
    language: str

    # Candidate must explicitly consent.
    consent: bool

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

def build_session_config(language_code: str):
    """
    Build the session.update event that tells OpenAI
    how this realtime interview session should behave.
    """

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
                        "type": "near_field",
                    },

                    # Ask OpenAI to turn candidate
                    # speech into text.
                    "transcription": {
                    # TalentSift prioritizes accurate final interview
                    # evidence over instant live captions.
                    "model": "gpt-transcribe",

                    "language": 
                        language_code
                    ,
                },

                    # Server-side Voice Activity Detection.
                    #
                    # OpenAI decides when the candidate
                    # starts and stops talking.
                    "turn_detection": {
                        "type": "server_vad",

                        "interrupt_response": True,

                        "threshold": 0.5,

                        "prefix_padding_ms": 300,

                        "silence_duration_ms": 1500,

                        "create_response": False,
                    },
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
            "tools": [
            {
                "type": "function",
                "name": "finish_interview",
                "description": (
                    "Call this only when all required "
                    "interview questions and necessary "
                    "follow-up questions have been completed "
                    "and the interview should now end."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ],

        "tool_choice": "auto",

            },
        }


@app.post("/api/session")
async def create_session(
    request: CreateSessionRequest
):

    # -------------------------------------------------
    # 1. CONSENT IS REQUIRED
    # -------------------------------------------------

    # Tuesday's rule:
    #
    # no consent
    #     ↓
    # no Session
    if not request.consent:

        raise HTTPException(
            status_code=400,
            detail=(
                "Consent is required before "
                "an interview session can be created."
            ),
        )


    # -------------------------------------------------
    # 2. CHECK THAT CANDIDATE EXISTS
    # -------------------------------------------------

    try:

        candidate = load_candidate(
            request.candidate_id
        )

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Candidate not found.",
        )


    # -------------------------------------------------
    # 3. VALIDATE LANGUAGE
    # -------------------------------------------------

    # Tuesday currently supports English and French.
    if request.language not in (
        "en",
        "fr",
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Language must be 'en' or 'fr'."
            ),
        )


    # -------------------------------------------------
    # 4. CREATE THE REAL SESSION
    # -------------------------------------------------

    session = Session(
        job_id=request.job_id,
        candidate_id=candidate.id,
        status="pending",
        language=request.language,
        consent_given=True,

        # started_at remains None.
        #
        # Candidate has consented, but the actual
        # voice interview has not started yet.
    )


    # -------------------------------------------------
    # 5. SAVE TO SQLITE
    # -------------------------------------------------

    save_session(
        session
    )


    print(
        "✅ Session created:",
        session.id,
        "candidate:",
        candidate.id,
        "language:",
        session.language,
    )


    # -------------------------------------------------
    # 6. RETURN ONLY WHAT BROWSER NEEDS
    # -------------------------------------------------

    return {
        "session_id": session.id,
        "status": session.status,
        "language": session.language,
    }


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
async def interview(
    ws_browser: WebSocket,
    session_id: str,
):
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

    try:

        # ---------------------------------------------
        # BUILD THE OPENAI SESSION CONFIG ONCE
        # ---------------------------------------------

        session_config = build_session_config(
            session.language
        )


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
                "awaiting_opening_response": False,
                "opening_response_id": None,

                # True once the hard 10-minute interview
                # window has been reached.
                "time_limit_reached": False,

                # Becomes True once Bianca has started
                # the intentional finishing process.
                "finish_requested": False,

                # Holds the background timer task.
                "timer_task": None,
            }


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
                    await ws_engine.send(
                        json.dumps(
                            {
                                "type":
                                    "response.create",

                                "response": {
                                    "instructions": (
                                        "The maximum TalentSift "
                                        "interview time has now "
                                        "been reached. Do not ask "
                                        "another interview question. "
                                        "Call the finish_interview "
                                        "tool immediately. Do not "
                                        "give a score or hiring "
                                        "decision."
                                    )
                                },
                            }
                        )
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

            async def browser_to_engine():
                """
                Continuously move candidate microphone audio
                from Chrome to OpenAI.
                """

                audio_chunk_count = 0

                try:

                    while True:

                        # ---------------------------------
                        # WAIT FOR BROWSER MESSAGE
                        # ---------------------------------

                        raw_message = (
                            await ws_browser.receive_text()
                        )


                        # JSON text -> Python dictionary.
                        msg = json.loads(raw_message)


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



                        elif (
                            msg_type
                            == "opening_playback_finished"
                        ):

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

                        else:

                            print(
                                "⚠️ Unknown browser "
                                f"message: {msg_type}"
                            )


                except WebSocketDisconnect:

                    print(
                        "🔌 Browser disconnected"
                    )


                    # If the human leaves the page,
                    # close the OpenAI connection too.
                    #
                    # Otherwise we could leave an
                    # unnecessary Realtime session alive.
                    await ws_engine.close()


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
                # -------------------------------------------------
                # CANDIDATE INTERRUPTION STATE
                # -------------------------------------------------
                #
                # VAD can sometimes briefly detect breathing,
                # clicks, background speech, or other noise as
                # candidate speech.
                #
                # We therefore wait a short moment before telling
                # the browser to interrupt Bianca.

                candidate_is_speaking = False

                interruption_task = None


                async def confirm_candidate_interruption():

                    try:

                        # Give VAD a short confirmation window.
                        await asyncio.sleep(
                            0.25
                        )


                        # If speech is still active after 250 ms,
                        # treat it as a real candidate interruption.
                        if candidate_is_speaking:

                            await ws_browser.send_json(
                                {
                                    "type": "interrupt",
                                }
                            )


                            print(
                                "🤫 Candidate interruption confirmed"
                            )


                    except asyncio.CancelledError:

                        # Speech stopped before the confirmation
                        # window finished.
                        #
                        # Treat that as a tiny/noisy trigger rather
                        # than interrupting Bianca.
                        print(
                            "🔇 Short speech trigger ignored"
                        )         
                async for raw in ws_engine:

                    # OpenAI JSON text
                    # -> Python dictionary.
                    event = json.loads(raw)


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

                        response_data = event.get(
                            "response",
                            {},
                        )


                        response_id = response_data.get(
                            "id"
                        )


                        if (
                        interview_state[
                                "opening_response_id"
                            ]
                            and response_id
                                == interview_state[
                                    "opening_response_id"
                                ]
                        ):

                            print(
                                "✅ Opening response fully generated"
                            )


                            await ws_browser.send_json(
                                {
                                    "type":
                                        "opening_generated",
                                }
                            )

                            # We no longer need to keep this ID
                            # after the opening has completed.
                            interview_state[
                                "opening_response_id"
                            ] = None



                        # Is this the closing response we saved earlier?
                        if (
                            closing_response_id
                            and response_id == closing_response_id
                        ):

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
                                }
                            )


                    # ---------------------------------
                    # BIANCA CALLED A TALENTSIFT TOOL
                    # ---------------------------------

                    elif (
                        event_type
                        == "response.output_item.done"
                    ):
                        # So first inspect what kind of item it is.
                        item = event.get(
                            "item",
                            {},
                        )


                        # We only care here about function calls.
                        if (
                            item.get("type") == "function_call"
                            and item.get("name") == "finish_interview"
                        ):

                            print(
                                "🏁 Bianca requested interview finish"
                            )

                            # -----------------------------------------
                            # INTERVIEW IS NOW FINISHING
                            # -----------------------------------------

                            interview_state[
                                "finish_requested"
                            ] = True


                            timer_task = interview_state[
                                "timer_task"
                            ]


                            if (
                                timer_task
                                and not timer_task.done()
                            ):

                                timer_task.cancel()   


                            # -----------------------------------------
                            # GET THE TOOL CALL ID
                            # -----------------------------------------
                            call_id = item.get(
                                "call_id"
                            )


                            if not call_id:

                                print(
                                    "❌ finish_interview had no call_id"
                                )

                                continue


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
                                current_session.language,
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
                            await ws_engine.send(
                                json.dumps(
                                    {
                                        "type":
                                            "response.create",

                                        "response": {

                                            "instructions": (
                                                "Read the following closing "
                                                "message exactly as written. "
                                                "Do not add, remove, or change "
                                                "any words:\n\n"
                                                + closing_text
                                            ),

                                            # Do not allow another tool call
                                            # during the closing.
                                            "tools": [],

                                            "tool_choice": "none",
                                        },
                                    }
                                )
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
                        audio_b64 = event.get(
                            "delta",
                            "",
                        )


                        if audio_b64:

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
                    # This arrives while Bianca is still
                    # speaking.
                    # elif (
                    #     event_type
                    #     == "response.output_audio_transcript.delta"
                    # ):

                    #     # "delta" is only the newest little
                    #     # piece of Bianca's transcript.
                    #     delta_text = event.get(
                    #         "delta",
                    #         "",
                    #     )


                    #     if delta_text:

                    #         # Translate OpenAI's detailed event
                    #         # into our simpler TalentSift message.
                    #         await ws_browser.send_json(
                    #             {
                    #                 "type": "transcript_delta",
                    #                 "speaker": "interviewer",
                    #                 "text": delta_text,
                    #             }
                    #         )

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


                            save_transcript_turn(
                                session_id,
                                interviewer_turn,
                            )


                            print(
                                "💾 Saved Bianca transcript turn"
                            )


                            # Send the completed interviewer
                            # transcript to Chrome.
                            await ws_browser.send_json(
                                {
                                    "type": "transcript",
                                    "speaker": "interviewer",
                                    "text": interviewer_text,
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


                        if delta_text:

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


                            save_transcript_turn(
                                session_id,
                                candidate_turn,
                            )


                            print(
                                "💾 Saved candidate transcript turn"
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

                            # ---------------------------------------------
                            # ONLY CONTINUE IF THE INTERVIEW IS ACTIVE
                            # ---------------------------------------------
                            #
                            # A candidate transcript may finish processing
                            # at almost the same moment that:
                            #
                            # 1. Bianca naturally finishes, or
                            # 2. the hard interview time limit is reached.
                            #
                            # We still KEEP the candidate transcript above,
                            # but we must not create another Bianca question.

                            if (
                                not interview_state[
                                    "finish_requested"
                                ]
                                and not interview_state[
                                    "time_limit_reached"
                                ]
                            ):

                                await ws_engine.send(
                                    json.dumps(
                                        {
                                            "type":
                                                "response.create"
                                        }
                                    )
                                )


                                print(
                                    "➡️ Candidate transcript accepted "
                                    "— Bianca response requested"
                                )


                            else:

                                print(
                                    "⏭️ Candidate transcript saved, "
                                    "but no new Bianca response was requested "
                                    "because the interview is finishing"
                                )


                    # ---------------------------------
                    # VAD: CANDIDATE STARTED SPEAKING
                    # ---------------------------------

                    elif (
                        event_type
                        == "input_audio_buffer.speech_started"
                    ):

                        print(
                            "🎤 Candidate started speaking"
                        )


                        # Mark speech as currently active.
                        candidate_is_speaking = True


                        # If an old confirmation timer somehow
                        # still exists, cancel it first.
                        if (
                            interruption_task
                            and not interruption_task.done()
                        ):

                            interruption_task.cancel()


                        # Do NOT interrupt Bianca immediately.
                        #
                        # First confirm that candidate speech lasts
                        # longer than a tiny noise trigger.
                        interruption_task = (
                            asyncio.create_task(
                                confirm_candidate_interruption()
                            )
                        )


                        await ws_browser.send_json(
                            {
                                "type": "status",
                                "value": "listening",
                            }
                        )
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

                        print(
                            "🛑 OpenAI detected "
                            "end of speech"
                        )


                        # Candidate is no longer speaking.
                        candidate_is_speaking = False


                        # If speech ended before our 250 ms
                        # confirmation window completed, cancel
                        # the interruption.
                        if (
                            interruption_task
                            and not interruption_task.done()
                        ):

                            interruption_task.cancel()


                        await ws_browser.send_json(
                            {
                                "type": "status",
                                "value": "processing",
                            }
                        )

                    # ---------------------------------
                    # OPENAI ERROR
                    # ---------------------------------

                    elif event_type == "error":

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


            await ws_engine.send(
                json.dumps(
                    {
                        "type":
                            "response.create"
                    }
                )
            )


            print(
                "🎬 Bianca opening requested"
            )

            # =================================================
            # RUN BOTH PUMPS AT THE SAME TIME
            # =================================================

            await asyncio.gather(
                browser_to_engine(),
                engine_to_browser(),
            )


    except Exception as error:

        print(
            "\n❌ Interview relay error:"
        )

        print(error)


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
        },
    )

@app.post("/recruiter/session/{session_id}/override")
def recruiter_override(
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
        overridden_by="demo-recruiter",
    )

    return RedirectResponse(
        url=f"/recruiter/session/{session_id}",
        status_code=303,
    )


@app.post("/recruiter/session/{session_id}/restore-ai")
def recruiter_restore_ai_score(
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
        restored_by="demo-recruiter",
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

app.mount(
    "/",
    StaticFiles(
        directory="client",
        html=True,
    ),
    name="client",
)