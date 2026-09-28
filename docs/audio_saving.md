# Save interview audio in TalentSift

This update adds optional audio recording to the shared TalentSift project.
It saves the candidate's microphone and Bianca's actual playback together,
then provides a player and download link on the recruiter session page.
Deployment remains paused.

## Try it locally

1. Extract `TalentSift-audio-saving.zip` into a separate folder. This bundle is
   based on the shared project plus the earlier live-caption update; merge any
   newer changes from your laptop before replacing your working project.
2. Activate your project's virtual environment. For a fresh copy, install with
   `python -m pip install -r requirements.txt`. The included requirements were
   frozen from an isolated application and test environment. Recording itself
   adds no new Python dependency beyond the existing FastAPI stack.
3. Put your existing `OPENAI_API_KEY` in your local `.env`, then run
   `python -m uvicorn app:app --reload` from the project folder.
4. Open `http://localhost:8000/recruiter/jobs`, create a test job, and follow its
   candidate interview link.
5. On the consent screen, select **I also agree to save this interview's audio**.
   It is optional and starts unchecked. Without this choice, the interview
   works as before and no audio recording is created.
6. Start the interview. The recording status appears below the current screen.
   Speak, let Bianca reply, then finish or use **End Interview**. Keep the page
   open until **Audio recording saved** appears.
7. Open that session from the recruiter dashboard. Use the **Audio** tab to
   play or download the recording.

The patch file contains only changes relative to the earlier live-caption
bundle. If using Git, run `git apply --check TalentSift-audio-saving.patch`
before applying it. A failed check means your local files differ: merge the
changes instead of overwriting newer work. Keep the patch outside the project
or adjust the command's path as appropriate.

## Where to edit

| File | What it does |
| --- | --- |
| `client/audio-recorder.js` | Mixes the microphone with Bianca's playback, selects a supported encoding, sends audio chunks, and waits for the final save acknowledgement. |
| `client/index.html` | Adds recording consent, sends `record_audio` when creating a session, connects the recorder, taps Bianca's playback gain, and finishes recording on completion or disconnect. |
| `recording_storage.py` | Saves chunks to disk, records consent and recording status, bounds recording size, and prevents overwrite. |
| `recording_routes.py` | Handles the separate recording WebSocket and serves recruiter playback/downloads. |
| `app.py` | Registers the recording routes, prepares an upload token only after opt-in, and passes recording details to the recruiter view. |
| `config.py` | Loads `.env` before resolving the shared data directory. |
| `templates/recruiter_session.html` | Shows the player, download link, and partial/missing recording states. |
| `client/styles.css` | Styles recording status and the player. |

## What is captured

Recording starts when the interview engine is ready. It includes the opening,
the candidate's microphone during the conversation, and Bianca's playback.
Bianca is tapped after her playback gain, so existing pauses, fades, and
interruption stops affect the recording. The recorder does not save an unheard
tail directly from OpenAI's generated audio.

The audio mix is not connected to the speakers; your microphone is not played
back to you. The microphone and playback use the same browser audio clock.
Both sources are reduced by 6 dB in the recording mix to leave room for overlap.
The live interview's playback volume is unchanged.

This is compressed replay audio after the browser's microphone processing,
not a bit-for-bit archive of the PCM sent to OpenAI. Headphones help avoid
Bianca's speaker output leaking back into the microphone. Recording makes it
possible to compare what was audible with transcription errors such as
"I am David" becoming "English"; it does not itself improve the recognizer.

The browser tries Opus in WebM, then another supported WebM/Opus-in-Ogg format,
then MP4. It uses `MediaRecorder.isTypeSupported()` rather than assuming that
every browser supports WebM. A 96 kbps mono recording is roughly 7 MB for ten
minutes, plus container overhead; the browser controls the actual bitrate.

## Where the files go

The default location, when launched from the project folder, is:

```text
data/recordings/<session-id>/audio.webm
data/recordings/<session-id>/metadata.json
```

Other selected formats use `audio.ogg` or `audio.m4a`. Metadata records the
recording consent time/version, MIME type, byte count, and status. It stores
only a hash of the upload token. The token is sent once to the candidate's
page and then in the recording WebSocket handshake message; it is not put in
a URL, browser local storage, or the recruiter page.

Set `TALENTSIFT_DATA_DIR` in `.env` or the process environment to put all
TalentSift data somewhere else. For example, on Windows:

```dotenv
TALENTSIFT_DATA_DIR=D:/TalentSiftData
```

Audio files are kept outside the public `client/` folder. Playback is served
through `/recruiter/session/<session-id>/audio`; adding `?download=true`
downloads the same bytes. No automatic audio deletion or retention schedule
is included. While the app is stopped, deleting a session's recording folder
removes its audio and recording metadata without changing its transcript.

## Endings and failures

- The browser requests an audio chunk about every second and sends it over
  `/ws/recording/<session-id>`. This connection is separate from the existing
  interview and live-transcription connections.
- A normal finish or early end stops the encoder, sends its final chunk, then
  sends the finish message. The UI reports success only after the server
  flushes and acknowledges the recording.
- If the interview connection drops, the recording connection can still
  finalize the received audio, marking it interrupted.
- If the tab, recording connection, or process closes abruptly, already
  written chunks are kept. The last unsent chunk may be lost. A partial
  container, particularly MP4, may not play correctly. The recruiter page
  labels incomplete recordings; it does not call them complete.
- A process crash can leave metadata saying `recording`; the recruiter page
  treats that state as in progress or interrupted. A power/disk failure can
  still lose buffered bytes. This is not a crash-proof storage system.
- Unsupported recording, storage failure, or an upload backlog stops the
  recorder and displays a warning while the interview continues.
- Limits are 32 MiB per recording, 1 MiB per server message, 4 MiB of browser
  upload backlog, and a 15-minute recording connection. The browser stops
  capture at 14 minutes; TalentSift's existing interview limit is ten minutes.
  Delayed browser chunks are split into 256 KiB WebSocket messages.
- Recordings cannot be appended after finishing or overwritten by a second
  connection using the same session/token. Start a new interview for a retake.

## Validation

Run the automated checks from the project folder:

```bash
python -m pytest -q
node --test tests/test_audio_recorder.cjs
```

Verified on 26 September 2026:

- All 30 Python tests pass, including existing caption/scoring tests and new
  consent-token, append, interruption, size-limit, playback, and range checks.
- All 4 JavaScript lifecycle tests pass, including final-chunk ordering,
  acknowledgement before success, unsupported recording, and upload failure.
- The real application creates sessions with and without recording, rejects
  missing interview consent, and renders the corresponding recruiter view.
- A real Uvicorn HTTP/WebSocket round trip saved and downloaded a synthetic
  Opus/WebM file byte-for-byte. The downloaded audio decoded successfully.
- Python and browser script syntax checks pass.

A live microphone/OpenAI interview has not been run here. The browser binary
was unavailable, so automated browser audio rendering was not verified.
Before deployment, test these on your own browser:

1. Say **"My name is David. I am testing audio recording."** and replay it.
2. Confirm Bianca and the candidate are both audible, with no doubled voice.
3. Interrupt Bianca and verify her stopped speech is also stopped in playback.
4. End one interview early and complete another normally. Check both tails.
5. Repeat with recording unchecked: no recording/player should be created.
6. Reload mid-interview and check that any received recording is labelled
   partial. Test the exact browsers you plan to support, including mobile if
   applicable.

## Before resuming deployment

The current shared MVP has no recruiter authentication. Its transcript routes
are already accessible without a login, and the new audio route follows that
same access model. Add or reuse recruiter authentication on both the review
page and its audio/download route before putting real candidate recordings
on a public URL. A session UUID is not access control.

Audio also needs persistent storage. A free Render web service's local files
can disappear on spin-down, restart, or redeploy. A paid persistent disk or
object storage is needed if these recordings must be retained. Keep runtime
audio, databases, metadata, and `.env` out of Git and release ZIPs. The included
ignore rules cover the default `data/` folder; keep a custom data directory
outside the source checkout.

Technical references:

- [MDN: recording a Web Audio mix](https://developer.mozilla.org/en-US/docs/Web/API/AudioContext/createMediaStreamDestination)
- [MDN: stop delivers final data before the stop event](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/stop)
- [MDN: supported recording formats](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/isTypeSupported_static)
- [Render: free service storage limits](https://render.com/docs/free)
