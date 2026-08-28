from datetime import datetime, timezone
# Needed when we mark the interview as completed.

from models import Session, TranscriptTurn, utc_now
# Import utc_now so we can record the exact completion time.
# Session represents the whole interview.
# TranscriptTurn represents one candidate/Alex turn.

from storage import SessionRepo
# SessionRepo handles saving/loading interview sessions.

import asyncio, base64, json, os, queue
import numpy as np
import sounddevice as sd
import websockets
from dotenv import load_dotenv
import time #To measure the latency
import statistics # To help us calculate the median formus
from pathlib import Path  # Helps us build a reliable path to the prompt file.

#Part 1: Connect and Configure
load_dotenv()

API_KEY = os.environ["OPENAI_API_KEY"]

URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1" #Tells the websocket where to connect to
HEADERS = {"Authorization" : f"Bearer {API_KEY}"} #For authorization via the API key if valid
print(URL)

#This tells it to capture the audio 16000 samples per second
MIC_RATE = 24000
SPK_RATE = 24000 #To know how fast the speaker should play back the audio from the OPENAI, commonly OpenAI realtime voices are commonly generated around 24kHz PCM 
CHUNK_MS = 40 # That means 40 milliseconds of the audio would be sent as chunks
play_q = asyncio.Queue() #This is where audio waits until the speaker is ready to play it
CHUNK_BYTES = 960 # 20ms at 24kHz, PCM16, mono
INTERRUPT_GRACE_MS = 40
fade_requested = asyncio.Event()

# Get the folder where interviewer.py itself is located.
BASE_DIR = Path(__file__).resolve().parent


# Build the path to Alex's instruction file.
PROMPT_FILE = BASE_DIR / "prompts" / "interviewer_prompt.txt"

# The language selected for this interview session.
LANGUAGE = "English"


# Read Alex's instruction template from the prompt file.
prompt_template = PROMPT_FILE.read_text(
    encoding="utf-8"
)


# Replace {LANGUAGE} in the prompt with the language selected for this session.
INSTRUCTIONS = prompt_template.replace(
    "{LANGUAGE}",
    LANGUAGE
)

async def main():
    async with websockets.connect(
        URL,#OpenAI Realtime Websocket URL
        additional_headers=HEADERS , # Send your authorization and other required headers.
        ping_interval=20,  # Send a keepalive ping every 20 seconds.,
         ping_timeout=60,  # Give the connection up to 60 seconds to answer a ping before declaring it dead.
        ) as ws:
        # TODO #1 — build the session-configuration event.
        # From the current docs, find the event that updates the session, and set:
        #   a) input and output audio format  (16-bit PCM)
        #   b) turn detection = server-side VAD (the engine detects when you stop)
        #   c) a voice you like from the available list
        #   d) instructions = INSTRUCTIONS
        session_config = {
            "type": "session.update",          # the session-update event type
            "session": {
                "type" : "realtime",
                "audio": {
                    "input": {
                        # MY exisiting microphone audio format
                        "format": {
                            "type": "audio/pcm",
                            "rate": MIC_RATE
                        },
                        "noise_reduction": {

                            "type": "near_field"
                        },
                        "transcription": {
                            "model": "gpt-live-transcribe"
                        },  # Convert the candidate's spoken audio into text during the interview.

                        "turn_detection": {
                                            "type": "server_vad",
                                            "threshold" : 0.7, #How loud counts as speech (0.0 - 1.0)
                                            "prefix_padding_ms": 300,  # audio kept from just BEFORE speech began
                                            "silence_duration_ms" : 1000, # how long a pause ends your turn

                                            "create_response": True,
                                            "interrupt_response": True

                                        }
                    },

                    "output": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": SPK_RATE
                        },
                        "voice": "marin"
                    }
                },
                
                "instructions": INSTRUCTIONS,
                "tools": [
    {
        "type": "function",  # Tell Realtime that this is a function handled by our Python application.

        "name": "finish_interview",  # This is the name Alex will call when the whole interview is finished.

        "description": (
            "Call this only after all required interview competencies "
            "have been explored and the final closing has been given."
        ),  # Helps Alex understand exactly when this function should be used.

        "parameters": {
            "type": "object",  # The function accepts an object of arguments.

            "properties": {},  # We do not need any arguments to finish the interview.

            "required": []
        }
    }
],

"tool_choice": "auto",
# Allow Alex to decide when the finish_interview tool is appropriate.
            },
        }

        await ws.send(json.dumps(session_config)) #convert to json and wait till it sends
        print("✓ Session configured. Listening for events…") #Tells me on my terminal


        mic = sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16",
                          blocksize=CHUNK_SAMPLES, callback=on_mic) #setting the mic stream


        mic.start() #start recording
        await asyncio.gather(send_mic(ws), receive(ws))  


        

    # part 2: mic -> engine

CHUNK_SAMPLES = MIC_RATE * CHUNK_MS // 1000
mic_q = queue.Queue() #It acts like a buffer

def on_mic(indata, frames, t, status): #To receive every chunk audio from the microphone and place it into the queue
    mic_q.put(bytes(indata)) 


async def send_mic(ws): #To continuously send microphone chunks to OpenAI
    try:
        while True:
            chunk = await asyncio.to_thread(mic_q.get) #To wait for a chunk in another thread 
            audio_b64 = base64.b64encode(chunk).decode("utf-8") #converts the chunk to base64 then strings
            event = {
                        "type": "input_audio_buffer.append",
                        "audio": audio_b64
                        }
                #client event for OpenAI to know this is another microphone audio chunk

            await ws.send(json.dumps(event)) #convert to Json and send

    except websockets.exceptions.ConnectionClosed as e:
        # This runs if the WebSocket disappears while send_mic()
        # is trying to send another microphone chunk.

        print(
            f"\n🎤 Microphone sender stopped because connection closed: {e}"
        )

        return
        # End send_mic() cleanly instead of producing a large traceback.









async def graceful_stop(ws, speaker, playback_state):  # We need ws to send truncation and playback_state to know what was heard.

    await asyncio.sleep(
        INTERRUPT_GRACE_MS / 1000
    )  # Give Alex the tiny 40 ms graceful tail before stopping him.

    flush_playback()  # Delete all Alex audio that is still waiting inside our Python queue.

    await asyncio.to_thread(
        speaker.abort
    )  # Immediately stop audio that has already reached the sound device.

    await asyncio.to_thread(
        speaker.start
    )  # Restart the audio stream so Alex's next response can play normally.

    playback_state["playing"] = False  # Alex's interrupted local playback has now been stopped.

    item_id = playback_state["item_id"]  # Find out which Alex message the candidate was hearing.

    played_ms = int(
        playback_state["played_ms"]
    )  # Find approximately how many milliseconds of that message the candidate heard.

    if item_id is not None and played_ms > 0:  # Only truncate if we actually have an Alex message and some audio was heard.

        truncate_event = {  # Build the Realtime event that tells OpenAI where the candidate stopped hearing Alex.
            "type": "conversation.item.truncate",  # Tell OpenAI that we're shortening an earlier assistant audio message.
            "item_id": item_id,  # Identify the exact Alex message that was interrupted.
            "content_index": 0,  # OpenAI requires the audio content index to be 0 for this truncation event.
            "audio_end_ms": played_ms  # Keep only the amount of Alex audio the candidate actually heard.
        }

        await ws.send(
            json.dumps(truncate_event)
        )  # Send the truncation instruction to the OpenAI Realtime server.   


# For the from queue  to speaker
async def player(speaker, playback_state):  # Plays Alex's audio and tracks what the candidate actually hears.
    while True:  # Keep the playback worker alive throughout the interview.

        item_id, chunk = await play_q.get()  # Wait for the next Alex message ID + audio chunk.

        if playback_state["item_id"] != item_id:  # Check whether this chunk belongs to a new Alex message.
            playback_state["item_id"] = item_id  # Store the ID of the new Alex message.
            playback_state["played_ms"] = 0.0  # Reset heard duration because this is a new message.

        playback_state["playing"] = True  # Alex now has audio being played locally.

        await asyncio.to_thread(
            speaker.write,
            chunk
        )  # Send this PCM16 chunk to the speaker without blocking our async event loop.

        chunk_ms = (
            len(chunk) / (SPK_RATE * 2)
        ) * 1000  # Convert the number of PCM16 bytes in this chunk into milliseconds.

        playback_state["played_ms"] += chunk_ms  # Record that the candidate has now heard this additional audio.

        if (
            play_q.empty()
            and not playback_state["response_active"]
        ):  # Only call Alex finished when the queue is empty AND OpenAI has finished producing the response.

            playback_state["playing"] = False  # Alex is now genuinely finished with local playback.

#For flushing the queue when the candidate speak when the A.I is speaking
def flush_playback():
    while True:

        try:
            play_q.get_nowait()

        except asyncio.QueueEmpty:
            break

# To print the median,max and length of the latencies which is also the number of conversation between the A.I and the candidate
def print_latency_summary(latencies):
    if not latencies:
        return

    median_latency = statistics.median(latencies)
    worst_latency = max(latencies)

    print("\n--- LATENCY SUMMARY ---")
    print(f"Turns measured: {len(latencies)}")
    print(f"Median response gap: {median_latency:.0f} ms")
    print(f"Worst response gap: {worst_latency:.0f} ms")


ai_speaking = False
    #part 3: Engine -> Speakers
async def receive(ws): #For receiving everything the AI sends back
    speaker = sd.RawOutputStream(samplerate=SPK_RATE, channels=1, dtype="int16")#creates speaker output stream and OPENAI returns audio in PCM16 that is why it is int16
    speaker.start()  # Start the physical audio output stream.

    playback_state = {  # Shared information about the Alex audio currently being played.
        "item_id": None,  # No Alex message has been played yet when the interview starts.
        "played_ms": 0.0,  # The candidate has heard 0 ms of Alex so far.
        "playing": False,  # Alex is not currently playing through the speaker yet.
        "response_active": False  # True while OpenAI is still producing Alex's current response.
    }

    asyncio.create_task(
        player(speaker, playback_state)
    )  # Start the player in the background and give it access to our playback tracking information.


    interview_started = False
    interrupted = False

    turn_ended_at = None # Intially nobody as finished speaking
    waiting_first_delta =False #To tell if we are cuurrently waiting for the A.I's first audio
    latencies = [] #Where we'd save all the mesauremnts
    transcript_turns = []  # Store every completed candidate and interviewer turn for this interview.
    repo = SessionRepo()
    # Create our storage repository.
    # This also makes the data/ folder if it does not exist.


    session = Session(
        job_id="junior-python-backend",
        # TEMPORARY terminal-demo job ID.
        # Later this will come from the real Job selected by the recruiter.

        candidate_id="candidate-demo-001",
        # TEMPORARY candidate ID.
        # Later this will come from the real logged-in/registered candidate.

        status="in_progress",
        # The interview has started, so its state is now in progress.

        language=LANGUAGE,
        # Save the language selected for this exact session.

        consent_given=True
        # TEMPORARY for terminal testing.
        # Later this MUST come from the candidate's actual consent action.
    )

    finishing_interview = False
    # False = normal interview is still happening.
    # True = Alex has finished asking questions and we are waiting for the final closing.

    try:
        async for raw in ws: #waiting for messages from the websocket
            event = json.loads(raw) #convert the json text into python dictionary
            etype = event.get("type", "") # saving the event type into a variable called etype

            if etype == "session.updated" and not interview_started:
                print("✅ Session ready. Alex is starting...")
                interview_started = True
                start_event = {
                    "type": "response.create"
                }
                await ws.send(json.dumps(start_event))
        
            elif etype == "response.output_audio.delta":
                if waiting_first_delta and turn_ended_at is not None:  # Only measure the first Alex audio after the candidate's turn ended.
                    response_gap_ms = (time.monotonic() - turn_ended_at) * 1000  # Calculate the post-VAD response delay in milliseconds.

                    print(f"\n⏱️ Response gap: {response_gap_ms:.0f} ms")  # Show the latency during testing.

                    latencies.append(response_gap_ms)  # Save this latency so we can calculate the session summary later.

                    if len(latencies) == 10:  # Once we've collected ten exchanges...
                        print_latency_summary(latencies)  # ...show the median and worst latency.

                    waiting_first_delta = False  # We've measured the first delta, so don't measure the remaining chunks.


                if not interrupted:  # Only accept Alex audio if the candidate has NOT barged in.

                    audio_bytes = base64.b64decode(event["delta"])  # Convert OpenAI's Base64 audio back into raw PCM16 bytes.

                    item_id = event["item_id"]  # Get the ID of the exact Alex message this audio belongs to.

                    for i in range(0, len(audio_bytes), CHUNK_BYTES):  # Break the received audio into small chunks.

                        chunk = audio_bytes[i:i + CHUNK_BYTES]  # Take one small audio chunk.

                        play_q.put_nowait(
                            (item_id, chunk)
                        )  # Store BOTH the Alex message ID and audio together in the queue.




            elif etype == "input_audio_buffer.speech_started":  # VAD detected that the candidate has started speaking.

                print(
                    "\r🎤 you're talking…",
                    end=""
                )  # Show that candidate speech has been detected.


                if playback_state["playing"]:  # Check whether Alex is STILL locally audible when the candidate starts speaking.

                    interrupted = True  # This is genuine barge-in, so stop accepting new audio from Alex's interrupted response.

                    asyncio.create_task(
                        graceful_stop(
                            ws,  # Needed so graceful_stop can send conversation.item.truncate.
                            speaker,  # Needed so graceful_stop can stop the local speaker.
                            playback_state  # Needed to know which Alex message was playing and how much was heard.
                        )
                    )  # Run interruption handling in the background so receive() can keep processing WebSocket events.
                    print(
                        "\n✋ Alex yielding"
                    )  # Confirm that the candidate actually interrupted Alex.

                else:  # Alex had already finished speaking before the candidate began.

                    print(
                        "\r🎤 Normal candidate turn",
                        end=""
                    )  # This is ordinary conversation, so DO NOT abort, flush, or truncate Alex's previous question.



            elif etype == "conversation.item.input_audio_transcription.completed":
                # Get the completed candidate transcript.
                candidate_text = event.get("transcript", "").strip()

                # Ignore empty transcript events.
                if candidate_text:

                    session.transcript.append(
                        TranscriptTurn(
                            speaker="candidate",
                            # The candidate said this turn.

                            text=candidate_text,
                            # Save what the transcription model heard.

                            item_id=event.get("item_id")
                            # Keep OpenAI's conversation item ID for traceability.
                        )
                    )

                    repo.save(session)
                    # Save the updated session immediately so transcript progress isn't lost.

                    # Temporary evidence while we're testing.
                    print(f"\n📝 Candidate: {candidate_text}")


            elif etype == "response.output_audio_transcript.done":
                # Get Alex's completed spoken transcript.
                interviewer_text = event.get("transcript", "").strip()

                # Ignore empty transcript events.
                if interviewer_text:

                    session.transcript.append(
                        TranscriptTurn(
                            speaker="interviewer",
                            # Alex produced this turn.

                            text=interviewer_text,
                            # Save exactly what Alex said.

                            item_id=event.get("item_id")
                            # Keep the corresponding conversation item ID.
                        )
                    )

                    # Show Alex's completed transcript while testing.
                    print(f"\n📝 Alex: {interviewer_text}")


                    completed_now = False
                    # Normally this is just another interviewer turn.
                    # We only change it to True when this particular turn
                    # is the final closing.


                    if finishing_interview:
                        # finish_interview was already called,
                        # so this transcript is Alex's final closing.

                        session.status = "completed"
                        # NOW the whole interview is actually finished.

                        session.ended_at = utc_now()
                        # Record the exact time Alex finished the closing.

                        finishing_interview = False
                        # Reset this so another Alex response cannot
                        # accidentally complete the interview again.

                        completed_now = True
                        # Remember that THIS turn completed the interview.


                    repo.save(session)
                    # Save after every Alex turn.
                    #
                    # During the interview:
                    #   status = "in_progress"
                    #
                    # On the final closing:
                    #   status = "completed"
                    #   ended_at = timestamp
                    #
                    # This also means the final closing itself is inside
                    # the saved transcript before completion is persisted.


                    if completed_now:
                        print(
                            f"\n✅ Interview completed and saved: {session.id}"
                        )


            elif etype == "response.function_call_arguments.done":
                # The model has finished requesting one of our Python functions.

                function_name = event.get("name")
                # Find out which function Alex requested.


                if function_name == "finish_interview":
                    # Alex has decided that all required interview questions are finished.

                    finishing_interview = True
                    # IMPORTANT:
                    # We are NOT marking the Session completed yet.
                    # This only means we are entering the closing stage.


                    call_id = event.get("call_id")
                    # Every function call has an ID.
                    # OpenAI uses this to connect our function result
                    # back to the function Alex called.


                    tool_result = {
                        "type": "conversation.item.create",

                        "item": {
                            "type": "function_call_output",

                            "call_id": call_id,
                            # Tell OpenAI which finish_interview call
                            # this result belongs to.

                            "output": json.dumps({
                                "status": "ready_to_close"
                            })
                            # Tell Alex that Python accepted the request
                            # and he should now perform the final closing.
                        }
                    }


                    await ws.send(json.dumps(tool_result))
                    # Send our function result back to the Realtime conversation.


                    closing_response = {
                        "type": "response.create",

                        "response": {
                            "instructions": (
                                "Deliver the final interview closing now. "
                                "Thank the candidate briefly, tell them their responses "
                                "will be reviewed, clearly state that the interview has ended, "
                                "and say goodbye. "
                                "Do not ask another question."
                            )
                        }
                    }


                    await ws.send(json.dumps(closing_response))
                    # Explicitly ask the Realtime model to generate the closing response.

                    print("\n🏁 Interview questions finished. Generating closing...")

            elif etype == "input_audio_buffer.speech_stopped":
                turn_ended_at = time.monotonic()# start tehs top watch now
                waiting_first_delta = True

            elif etype == "response.created":  # OpenAI has started creating a new Alex response.
                interrupted = False  # Allow audio from this new response into the playback queue.
                playback_state["response_active"] = True  # Remember that OpenAI is currently producing Alex's response.

            elif etype == "response.done":  # OpenAI has finished generating/sending Alex's current response.
                playback_state["response_active"] = False  # The server is no longer producing this Alex response.
                if play_q.empty():  # If there is also no remaining Alex audio waiting locally...
                    playback_state["playing"] = False  # ...then Alex is genuinely no longer audible.
                print("\r🤖 interviewer response generated.")

            elif etype == "conversation.item.truncated":  # OpenAI sends this when our truncate request succeeds.

                print(
                    f"\n✂️ Alex memory truncated at {event['audio_end_ms']} ms"
                )  # Show exactly where OpenAI says the assistant audio was truncated.

            elif etype == "error":
                print("\n⚠️ ", json.dumps(event, indent=2))


    except websockets.exceptions.ConnectionClosedError as e:
          # This runs only if the WebSocket unexpectedly disconnects,
        # such as from a keepalive ping timeout.

        print(
            f"\n❌ Realtime connection lost: {e}"
        )




asyncio.run(main())







