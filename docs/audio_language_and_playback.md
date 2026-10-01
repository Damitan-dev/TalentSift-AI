# Interview language, captions and playback

## What changed and why

| File | Change | Reason |
| --- | --- | --- |
| `transcription_config.py` | Shared language normalization and transcription context | Both transcribers must receive the same selected language and hints. |
| `app.py` | Uses `languages`, sends Bianca text deltas with item/response IDs, checks response completion and audio presence | The former final-transcription field was outdated, and generation finishing did not prove playback had finished. |
| `live_captions.py` | Defaults to `low`, buffers startup audio within a limit, displays early provisional IDs and links them later | Captions should not wait solely for another connection to name the speech turn. |
| `client/index.html` | Uses per-item caption rows, queued-audio cues and source completion | The previous evenly spaced word animation drifted from the sound. Wall timers could advance while the audio clock was suspended. |
| `prompts/interviewer_prompt.txt` | Clarifies that accents, names and isolated terms must not switch language | Selected language and accent are separate concepts. |
| `.env.example` | Documents caption delay and existing main noise settings | Tuning should be visible and reproducible. |

The candidate still selects English or French before the session is created.
The server stores that choice in SQLite. Both `gpt-transcribe` and
`gpt-live-transcribe` now receive exactly `["en"]` or `["fr"]` through
`languages`. Older stored values `English` and `French` are normalized. Unknown
values fail instead of silently becoming English. Bianca's questions and fixed
closing also use the normalized language.

These are expected-input language hints. They do not guarantee that unclear
audio becomes English, and they do not request translation. The shared prompt
asks for faithful speech in its original language, with names, numbers,
technical terms and negations preserved. Only the candidate name and job title
are provided as optional literal keyword hints; expected answers are never
provided. Evaluate the hints on real recordings because they can also bias
recognition. This context is not added to the scoring prompt.

## Captions and reliable playback

The main interview still owns voice activity detection, replies and the final
transcript used for scoring. The separate live stream keeps
`turn_detection: null`. Faster provisional captions do not replace final text.

A live delta can arrive before the main stream assigns an item ID. It now
appears immediately under a provisional `live:` ID. A later `transcript_link`
reconciles the paragraph with the main ID without repeating the words. Final
text replaces only its matching paragraph. Empty final speech retracts its
preview, late previews cannot overwrite final text, and unlinked previews are
removed if the optional caption connection fails.

Bianca's deltas and final text also carry item and response IDs. The browser
holds text against the associated queued audio and advances it using the
AudioContext clock. Final text is applied after generation and playback finish.
The former words-per-second animation has been removed. Interrupted items reject
late caption tails independently of newer replies.

This is approximate chunk-level coordination, not exact word timestamps. These
models do not supply word alignment. Precise word highlighting in recording
replay requires a compatible timestamp/alignment workflow.

Playback has a 60 ms scheduling buffer when the queue needs filling. Opening and
closing acknowledgements require all listed audio sources to have ended and the
running audio clock to reach their end times. Their response IDs must match on
the server. Failed, cancelled or audio-less openings/closings do not count as
successful playback. Candidate audio is sent after the opening acknowledgement;
the backend continues to enforce its opening gate and usage limits.

## Run and retest locally

Restart Uvicorn after updating these files together. Refresh the candidate page
before using a new invitation; an already-open page has the old protocol code.

```bash
python -m pytest -q
node --test tests/*.cjs
uvicorn app:app --reload
```

Optional `.env` settings:

```text
TALENTSIFT_LIVE_CAPTIONS=1
TALENTSIFT_CAPTION_DELAY=low
TALENTSIFT_NOISE_PROFILE=near_field
```

An existing `TALENTSIFT_CAPTION_DELAY=medium` overrides the new default. Compare
`low` and `medium` using the same speech; lower delay can reduce preview quality.
Browser echo cancellation/noise suppression and the main server noise profile
remain in place. The separate caption socket does not inherit the main socket's
server-side noise reduction.

1. Run an English interview: listen to the full introduction, answer "Yes", then
   say "My name is David. I have not used Django in production."
2. Run a French interview: answer "Oui", then say "Je m'appelle David. Je n'ai pas
   utilisé Django en production."
3. Check numbers, names, technical terms and negations against the recording.
   Technical names can remain English in French speech. Do not judge language by
   accent or a one-word answer.
4. Speak with natural pauses, then interrupt a later Bianca question. Verify that
   old text does not overwrite the new reply and final candidate text updates the
   correct paragraph.
5. Try a long answer, a close microphone and realistic room noise. Compare live
   preview latency and saved-transcript errors separately. Measure word errors
   and meaning-changing errors, rather than assigning an unmeasured accuracy
   percentage.

Audio recording still follows the existing opt-in consent. The current recorder
stores the mixed conversation. No second paid transcription pass, model upgrade,
audio fine-tuning, translation layer or separate candidate recording was added.
Those are potential follow-up experiments if measured final accuracy requires
them. A completed transcription can still be wrong; recruiters should review
unclear evidence instead of treating a recording failure as poor performance.

## Validation limits

Automated tests use controlled OpenAI events and browser audio clocks. They
exercise English/French configuration, full topic coverage, delayed final
transcription before scoring, suspended clocks, actual source completion,
interrupted captions, provisional/final ordering and caption failure cleanup.
They do not measure real microphone transcription accuracy, provider latency or
physical output-device behaviour. A live interview on the user's device remains
necessary before declaring the reported audio issue resolved.

Official model contract references:

- https://developers.openai.com/api/docs/guides/realtime-transcription
- https://developers.openai.com/cookbook/examples/migrating_from_whisper_to_gpt_transcribe
- https://developers.openai.com/api/docs/guides/voice-prompting
