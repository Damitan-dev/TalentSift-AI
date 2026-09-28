# TalentSift candidate captions and microphone quality

These changes were prepared against the TalentSift-AI snapshot supplied on
23 September 2026. If your local Windows copy has newer edits, compare and
merge the changes before replacing either `app.py` or `client/index.html`.

## Install

From the bundle, place the files at these paths in your project:

| Bundle file | Project path |
| --- | --- |
| `app.py` | `app.py` |
| `live_captions.py` | `live_captions.py` (new) |
| `client/index.html` | `client/index.html` |
| `tests/test_live_captions.py` | `tests/test_live_captions.py` (new, optional for deployment) |
| `docs/live_captions_and_audio_quality.md` | `docs/live_captions_and_audio_quality.md` |

Use the patch in the deliverables to inspect the exact changes to existing
files. The project already uses `websockets`; install its normal requirements
as described in `README.md`.

Keep `OPENAI_API_KEY` on the server in `.env`. Start with
`uvicorn app:app --reload`, then open an interview in Chrome. Begin after
Bianca finishes her opening. Speak a longer answer and watch the candidate
paragraph grow *during* speech. The final paragraph may correct the preview.

## How the two streams work

The browser sends the same 24 kHz PCM chunks to the existing interview socket
and to an optional `gpt-live-transcribe` transcription-only socket. The live
socket uses `turn_detection: null` and receives the main interview's speech
start/stop item IDs. It emits provisional `transcript_delta` messages as audio
arrives. At speech stop, the relay commits the live buffer. The original
`gpt-transcribe` completion saves the official candidate transcript and asks
Bianca for the next response. If live transcription fails, the interview keeps
running and the completed transcript still appears. The second audio stream
adds transcription usage and cost.

Both streams receive the browser's processed microphone signal. The main
interview also has its own `near_field`/`far_field` server setting; the live
preview does not inherit that setting, so its temporary words may differ in
noise. Judge accuracy using the saved final transcript.

The main session keeps `server_vad`, `silence_duration_ms: 1500`, and
`create_response: false`. Do not put `gpt-live-transcribe` into that main
session's `transcription.model`: it has no server VAD. In this implementation,
Bianca replies only when the main session emits a nonempty completed candidate
transcript.

## Settings

Add these optional lines to the server's `.env`, then restart the server:

```text
TALENTSIFT_LIVE_CAPTIONS=1
TALENTSIFT_CAPTION_DELAY=medium
TALENTSIFT_NOISE_PROFILE=near_field
```

| Setting | Choices | Use |
| --- | --- | --- |
| `TALENTSIFT_LIVE_CAPTIONS` | `1` (default), `0` | Turn the optional preview stream on or off. |
| `TALENTSIFT_CAPTION_DELAY` | `minimal`, `low`, `medium` (default), `high`, `xhigh` | Lower shows words sooner; higher lets the live model use more context. Test `low` versus `medium` with real voices and noise. |
| `TALENTSIFT_NOISE_PROFILE` | `near_field` (default), `far_field` | Use `near_field` for a close headset mic; `far_field` for a laptop or room mic. This configures the **main** interview socket in `app.py`. |

The browser already requests `echoCancellation`, `noiseSuppression`, and
`autoGainControl` in `client/index.html` inside `prepareMicrophone()`.
Browsers may not honor every request; open Chrome DevTools and inspect the
`🎛️ Microphone settings` log, which comes from the selected track's
`getSettings()` result. `captureCtx.sampleRate` should read `24000`: that is
the rate the server declares for the PCM audio. Use headphones and position
the microphone near the mouth when possible; it improves the original signal
before any processing.

Noise suppression reduces stationary and some changing noise, echo
cancellation targets Bianca's speaker playback leaking into the mic, and
automatic gain control adjusts level. Voice activity detection decides where
the candidate turn begins and ends. Newer optional neural suppressors such
as RNNoise can reduce harder background sounds, but compare recordings before
adding one: aggressive processing can erase soft consonants or make the VAD
miss short replies. Avoid stacking another processor without an A/B test.

For specialized vocabulary, the live model supports `prompt` and `keywords`
in `session.audio.input.transcription` in `live_captions.py`. Use a short,
neutral recording description and literal names or acronyms relevant to the
role; do not provide expected interview answers. The selected interview
language is already passed as `languages: ["en"]` or `["fr"]`. The saved
transcript comes from the main session, so a hint added only to the live
preview will not improve the saved text. Change and validate the main session
separately if you later decide to add hints there.

## Diagnose short replies and missing Bianca responses

After saying “yes,” allow the 1.5 second VAD silence period. Inspect the
server output in order:

1. `🎤 Candidate started speaking`: microphone audio reached main VAD. If
   absent, check mic selection, `getSettings()`, sample rate, and whether
   noise suppression has gated the voice.
2. `🛑 OpenAI detected end of speech`: VAD closed the turn. If absent, a
   background voice or noise may be keeping it open.
3. `📝 Candidate: ...` and `💾 Saved candidate transcript turn`: the main
   transcriber produced a usable final. If absent, compare `near_field` and
   `far_field`, lower room noise, and try a close headset microphone.
4. `➡️ Candidate transcript accepted — Bianca response requested`: the main
   flow requested Bianca's response. If present but she stays silent, inspect
   subsequent Realtime error and response events.

`📝 Live candidate captions ready` means only the preview socket connected.
It does not prove the main session heard the candidate or triggered Bianca.

## Check accuracy before adding processing

Record consented test samples with different microphones, accents, short
“yes/no” answers, numbers, technical names, and realistic room noise. Write
the spoken reference text. Compare the *saved final* transcript against that
reference and count substitutions, deletions, and insertions; word error rate
is `(substitutions + deletions + insertions) / reference words`. Also track
empty answers, missed turns, and time to first provisional word. Run the same
samples with each microphone/profile and `low`/`medium` caption delay. Keep
the settings that improve your actual samples. A clean headset recording is
the most useful baseline.

## Verification and limits

The isolated relay tests cover live words before a speech stop, live-to-main
turn IDs, two rapid turns, and live commit. Python and browser-script syntax
checks passed in the prepared snapshot. An authenticated microphone session
against the OpenAI endpoint still needs to be run in your environment.

References:

- OpenAI Realtime transcription: https://developers.openai.com/api/docs/guides/realtime-transcription
- OpenAI Realtime VAD: https://developers.openai.com/api/docs/guides/realtime-vad
- OpenAI noise reduction types: https://developers.openai.com/api/reference/ruby/resources/realtime
- MDN audio track settings: https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackSettings
- WebRTC Audio Processing Module: https://webrtc.googlesource.com/src/+/main/modules/audio_processing/g3doc/audio_processing_module.md
- RNNoise project: https://github.com/xiph/rnnoise
