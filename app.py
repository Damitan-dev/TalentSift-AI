import asyncio
import json
import os
from pathlib import Path

import websockets

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.staticfiles import StaticFiles


app = FastAPI()


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


INSTRUCTIONS = prompt_template.replace(
    "{LANGUAGE}",
    LANGUAGE,
)


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
        },
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

                For THIS checkpoint we forward:
                - session status
                - candidate transcript

                Next checkpoint we add Bianca audio.
                """

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


                        await ws_browser.send_json(
                            {
                                "type": "response_started",
                            }
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