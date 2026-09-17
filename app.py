import asyncio
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
)
from fastapi.staticfiles import StaticFiles
from database import (
    initialize_database,
    load_candidate,
    load_session,
    save_session,
    save_transcript_turn,
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


# Build the path to our existing interviewer prompt.
PROMPT_FILE = (
    BASE_DIR
    / "prompts"
    / "interviewer_prompt.txt"
)


LANGUAGE = "English"


# Load the same Bianca prompt that interviewer.py uses.
prompt_template = PROMPT_FILE.read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------
# INTERVIEW FINISHING RULE
# ---------------------------------------------------------

# This is added to Bianca's normal interviewer prompt.
#
# Bianca decides WHEN the interview requirements
# have been completed.
#
# But Bianca does NOT invent the closing.
# Instead, she calls our finish_interview tool.
FINISHING_INSTRUCTIONS = """
When you have completed the interview and there are
no more substantive interview questions to ask,
call the finish_interview tool.

Do not create your own closing statement.
Do not tell the candidate their score or whether
they passed or failed.
"""

INSTRUCTIONS = (
    prompt_template.replace(
        "{LANGUAGE}",
        LANGUAGE,
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

def build_session_config():
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
                        "model": "gpt-live-transcribe",
                    },

                    # Server-side Voice Activity Detection.
                    #
                    # OpenAI decides when the candidate
                    # starts and stops talking.
                    "turn_detection": {
                        "type": "server_vad",

                        "interrupt_response": True,

                        "threshold": 0.7,

                        "prefix_padding_ms": 300,

                        "silence_duration_ms": 1000,


                        # Now that browser playback is being added,
                        # OpenAI may automatically generate Bianca's
                        # response after VAD detects that the candidate
                        # has finished speaking.
                        "create_response": True,
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
            "instructions": INSTRUCTIONS,

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

        # -------------------------------------------------
        # CONNECTION #2: FASTAPI -> OPENAI
        # -------------------------------------------------
        #
        # We now open ANOTHER WebSocket.
        #
        # ws_browser:
        #     Chrome <-> FastAPI
        #
        # ws_engine:
        #     FastAPI <-> OpenAI
        #
        # Both are open at the same time.
        async with websockets.connect(
            ENGINE_URL,
            additional_headers=ENGINE_HEADERS,
            ping_interval=20,
            ping_timeout=60,
        ) as ws_engine:

            print(
                "🤖 Connected to OpenAI Realtime"
            )


            # ---------------------------------------------
            # CONFIGURE THE OPENAI SESSION
            # ---------------------------------------------

            # build_session_config() gives us a
            # normal Python dictionary.
            session_config = build_session_config()


            # WebSockets send text.
            #
            # json.dumps() turns our dictionary
            # into JSON text before sending it.
            await ws_engine.send(
                json.dumps(session_config)
            )


            print(
                "⚙️ Realtime session configuration sent"
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

                        print(
                            "✅ OpenAI session ready"
                        )


                        # Tell the browser:
                        #
                        # "The entire route all the way
                        # to OpenAI is now ready."
                        await ws_browser.send_json(
                            {
                                "type": "status",
                                "value": "engine_ready",
                            }
                        )

                        # Ask OpenAI to generate Bianca's opening
                        # interview turn.
                        #
                        # Without this, create_response=True only helps
                        # AFTER candidate speech is detected.
                        await ws_engine.send(
                            json.dumps(
                                {
                                    "type": "response.create"
                                }
                            )
                        )

                    elif event_type == "response.created":

                        print(
                            "🤖 New Bianca response started"
                        )


                        response_data = event.get(
                            "response",
                            {},
                        )


                        response_id = response_data.get(
                            "id"
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
                                }
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


                        # -------------------------------------------------
                        # TELL THE BROWSER TO STOP BIANCA GRACEFULLY
                        # -------------------------------------------------
                        #
                        # FastAPI cannot directly control the candidate's
                        # laptop speakers.
                        #
                        # The browser owns playback, so we send a small
                        # TalentSift message telling JavaScript:
                        #
                        # "The candidate has interrupted Bianca."
                        await ws_browser.send_json(
                            {
                                "type": "interrupt",
                            }
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