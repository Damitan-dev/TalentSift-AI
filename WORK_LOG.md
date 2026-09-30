# TalentSift work log

## 26 September 2026 — audio saving before deployment

Added optional recording consent, a browser mix of candidate microphone plus
Bianca's actual playback, incremental saving over a separate WebSocket, and
recruiter playback/downloads. Received partial recordings are retained and
labelled incomplete. Audio is stored under `TALENTSIFT_DATA_DIR/recordings/`.

Validated: 30 Python tests, 4 JavaScript lifecycle tests, application smoke
checks, and a real HTTP/WebSocket transfer with byte-identical, decodable
Opus/WebM audio. Syntax checks pass. A real browser microphone/OpenAI interview
remains to be tested; browser rendering was not available in this environment.

Deployment is paused at the user's request. No deployed URL or hosted full
interview has been verified. Recruiter authentication and persistent audio
storage remain prerequisites for retaining real candidate recordings online.

See [the audio saving guide](docs/audio_saving.md) for setup and manual checks.

## 26 September 2026 — missing recorder script fix

The candidate reported `InterviewAudioRecorder is not defined` after the
`Microphone ready` log. Added an explicit script-availability check and isolated
optional recorder initialization errors, so a missing recording module no longer
blocks the interview or appears to be a microphone permission denial.

Added four integration regression tests using the actual inline microphone
setup code with mocked browser APIs. All eight JavaScript tests pass. The
setup guide now explains file placement, script order, and browser verification.

## 27 September 2026 — candidate interruptions and response timing

Removed the final-transcription gate from reply generation. The primary session
uses semantic VAD at medium eagerness and a manual response scheduler that
serializes questions and closing, handles resumed speech, and rejects canceled
reply audio/tool calls. Added separate timing logs for requesting a reply and
receiving its first audio. Fixed browser queue reset order and delayed fade
callbacks affecting a new reply. The 10-minute maximum remains in force.

Final transcription stays asynchronous but is persisted in turn order. Scoring
waits for pending text and skips on timeout or transcription failure. The live
caption session still has no VAD.

Validation: 43 Python tests and 12 JavaScript tests, including a simulated full
interview relay with delayed ASR and closing/scoring behavior. No real microphone
or live OpenAI latency measurement was available. See [setup and tuning](docs/turn_timing.md).

## 27 September 2026 — abrupt upstream disconnect and orphan caption tasks

The user's new log reached opening playback completion and then reported
`no close frame received or sent`. Reproduced the independent caption shutdown
bug: canceling its parent left both sender and receiver pending. Added shared
paired-task supervision to the primary relay and caption relay, persistent
failed-session status, browser interruption feedback, and microphone/close
metadata diagnostics. Fixed partial-transcript cleanup when settling one entry
removes several following ready entries. Billing failures now stop startup
retries immediately.

Validated with 53 Python tests and 15 JavaScript tests, including a real local
WebSocket TCP abort and actual relay-handler failure scenarios. The original
connection drop's cause remains unconfirmed; testing the user's network/live
OpenAI connection is still required. See [connection failure notes](docs/connection_failures.md).

## 27 September 2026 — recruiter access and invite-only beta

Implemented the approved one-recruiter login, unique expiring candidate invitations
with server-assigned names, cancellation, protected transcripts/audio/score
changes and durable usage admission. New access tables are created additively in
the existing SQLite database. Passwords use salted scrypt; session and invitation
secrets are stored as hashes. Cookies, CSRF and Origin checks protect browser
requests. Score audit entries now identify the authenticated recruiter.

The default beta allowance is 10 admitted interview starts per UTC day and 2
concurrent interviews. Invitation consumption, session start and slot reservation
commit in one transaction. A whole-connection deadline and real-time PCM budget
bound use even for modified clients. Failed paid starts remain counted; a new
invitation is required for another attempt. Opening the invitation or repeating
microphone preparation does not consume it. No provider billing settings were
changed, and no deployment was performed.

Passed 75 Python tests and 19 JavaScript tests, including new
security/identity/concurrency/recording and candidate JavaScript tests,
plus a real local Uvicorn HTTP/WebSocket smoke test with a simulated interviewer.
No OpenAI calls or real microphone interview were used. Browser rendering could
not be verified because a compatible browser executable could not be installed.
See [setup](docs/access_control_setup.md) and [design explanation](docs/access_control_explained.md).

## 28 September 2026 — recruiter login form origin rejection

The recruiter account was created successfully, but the browser login reported
"This request must come from the TalentSift site." Found a conflicting response
header in the access-control update: `Referrer-Policy: no-referrer` can make
native form POSTs send `Origin: null` with no Referer, which the strict origin
check rejects. Changed the policy to `same-origin`; null and foreign origins
remain rejected and CSRF validation remains required. Only `access_routes.py`
needs replacing at runtime. Existing recruiter credentials remain valid.

The policy regression failed before the fix on local IP, localhost and public
HTTPS configurations. After the fix all 85 Python tests passed, including ten
new cases covering form policy, login/invitation/logout, origin rejection and
Referer fallback with CSRF. Request headers were simulated; a real browser
walkthrough and the user's exact request headers were not available. The prior
19 JavaScript tests remain unchanged and were not rerun for this Python fix.
Updated the setup/design guides and release bundles with the correction.

## 28 September 2026 — required interview topic coverage

Found that the prompt grouped collaboration with other workplace behaviors and
allowed a discretionary early finish; the relay accepted finish calls without
checking coverage. Added a server-owned six-question bilingual plan across the
existing five scoring areas, with explicit collaboration and feedback/ownership
questions. The relay checks for the prescribed generated question and a later
candidate audio turn before advancing. Normal finish requests are guarded; the
hard time limit and candidate ending remain available with missing topics.

Per-turn remaining-time guidance prioritizes core questions over optional
follow-ups. Tool continuations share the response scheduler and wait for resumed
speech; replies still do not wait for final ASR. Coverage is logged, not stored
as a new score or dashboard field. This validates question opportunities, not
answer quality or complete audible playback.

Validation: 98 Python tests and 19 JavaScript tests passed, including early finish
at every topic, both languages, late/duplicate audio, cancellation, the deadline,
candidate stop and transcript-order/scoring behavior. Update and cumulative
patches apply cleanly to their baselines. No live microphone/OpenAI interview or
deployment was performed. See [the coverage guide](docs/interview_coverage.md).
