# TalentSift AI

**AI-Assisted Job Pre-Screening Interviewer — HR Tech / B2B**

TalentSift AI is a voice-first, AI-assisted interview platform designed to help
HR teams conduct more structured and evidence-based early-stage candidate
screening.

The system conducts a real-time voice interview, creates an interview
transcript, evaluates job-relevant evidence against a structured scoring
rubric, and provides recruiters with an auditable review dashboard.

TalentSift supports human hiring decisions. It is not designed to make
autonomous hiring decisions.

This repository contains work developed as part of the **TalentSift AI Product
Management & Engineering Internship**.

## Current MVP Capabilities

The current MVP includes:

- Real-time browser-based voice interviews
- AI interviewer named Bianca
- English and French interview support
- Candidate consent before an interview session begins
- Dynamic microphone selection
- Candidate-visible interview timer
- Maximum interview duration
- Candidate-controlled early interview ending
- Real-time transcript capture
- Persistent interview/session storage
- Evidence-grounded competency scoring
- `scored` and `not_explored` competency states
- Weighted overall scoring
- Recruiter leaderboard
- Candidate interview drill-down
- Recruiter score override with audit history
- Transcript download
- Interview failure-state tracking
- Fairness monitoring dashboard
- Controlled fairness-test storage
- Startup retry for OpenAI Realtime connections

## Architecture

The current TalentSift flow is:

```text
Candidate browser
        ↓
FastAPI backend
        ↓
OpenAI Realtime voice interview
        ↓
Interview transcript
        ↓
Evidence-based scoring
        ↓
Recruiter dashboard
        ↓
Human hiring decision
```

The candidate browser communicates with the FastAPI backend using WebSockets.

The backend maintains a separate server-side WebSocket connection to the
OpenAI Realtime API.

The OpenAI API key remains on the backend and is never exposed to candidate
browser JavaScript.

## Technologies

Current technologies include:

- Python
- FastAPI
- WebSockets
- asyncio
- OpenAI Realtime API
- SQLite
- Pydantic
- Jinja2
- HTML
- CSS
- JavaScript
- pytest
- Git & GitHub

## Scoring Model

The current Junior Python Backend Developer evaluation uses the following
competency weights:

| Competency | Weight |
|---|---:|
| Relevant Experience | 30% |
| Problem Solving | 25% |
| Communication | 20% |
| Role Motivation | 15% |
| Culture & Values Fit | 10% |

Competencies that were not meaningfully explored are represented as
`not_explored` rather than being assigned a score of zero.

The overall score is calculated using the explored competencies only, with
their weights renormalized.

## Testing

Run the complete automated Python test suite with:

```bash
python -m pytest -v
```

For a shorter test summary:

```bash
python -m pytest -q
```

Run the scoring tests specifically with:

```bash
python -m pytest tests/test_scoring.py -q
```

Run evaluator guardrail checks with:

```bash
python guardrail_check.py
```

Run evaluator consistency checks with:

```bash
python consistency_check.py
```

Current evaluator testing covers areas including:

- Evidence-grounded competency scoring
- `scored` and `not_explored` behavior
- Weighted score calculation
- Prompt-injection resistance
- Filler invariance
- Native-style vs. non-native grammar comparison
- English/French comparison
- Strong-vs-weak evidence sensitivity
- Competency isolation
- Repeated-run scoring consistency
- Level-5 score reachability

Controlled development tests do not demonstrate that TalentSift is universally
fair, unbiased, or production-ready.

## Fairness Monitoring

TalentSift separates **production monitoring** from **controlled development
fairness tests**.

### Production monitoring

The recruiter fairness dashboard calculates descriptive statistics from the
current completed interview data.

Current metrics include:

- Completed interviews by interview language
- Scored interviews by interview language
- Average original AI score by interview language
- Completed interviews without an available score

Missing scores are treated as unavailable data.

They are not converted to zero.

Recruiter overrides are excluded from AI-score averages so that later human
decisions do not alter monitoring of the original AI evaluation output.

### Controlled fairness testing

Controlled experiments are stored separately in:

```text
data/fairness_tests.json
```

The current Week 5 speaking-rate experiment compared slow, normal, and fast
delivery while keeping the interviewer question, scripted candidate response,
speaker, microphone, and environment constant.

All three controlled runs received the same competency and overall scores.

However, transcription errors occurred at every speaking rate, with the fast
condition producing the greatest distortion of explicit technical and
ownership evidence.

This does not prove fairness across speaking rates.

It provides limited development evidence of score consistency in those runs
while identifying speech transcription as an upstream reliability and
potential fairness risk.

Further testing remains necessary across:

- More speakers
- Multiple accents
- Larger repeated speaking-rate samples
- Additional speech characteristics

Fairness monitoring results are signals for investigation and should not be
interpreted independently as proof of discrimination or proof that no
discrimination exists.

## Recruitment Data Retention

TalentSift follows a data-minimization approach.

For unsuccessful applications, interview transcripts and evaluation results
should be deleted no later than three months after the relevant recruitment
process has ended unless a documented lawful purpose requires a different
retention period.

Where candidate data is retained for future recruitment opportunities, the
candidate should be appropriately informed and the retention period should
normally not exceed two years from the candidate's last contact, subject to
applicable law and organizational policy.

Retention periods must be configurable by the organization deploying
TalentSift rather than assumed to be universally applicable.

## Running TalentSift Locally

Create and activate a Python virtual environment.

Install the TalentSift web application dependencies:

```bash
python -m pip install -r requirements.txt
```

Developers who also need the older local Python audio tools can install:

```bash
python -m pip install -r requirements-local-audio.txt
```

Create a local `.env` file:

```text
OPENAI_API_KEY=your-api-key
```

Do not commit the real `.env` file.

Start TalentSift locally with:

```bash
uvicorn app:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

## Environment Variables

### `OPENAI_API_KEY`

Required.

Used by the TalentSift backend when connecting to OpenAI.

It must remain server-side.

### `TALENTSIFT_DATA_DIR`

Optional.

Locally, TalentSift defaults to:

```text
data/
```

A deployment can point TalentSift to a persistent mounted disk:

```text
TALENTSIFT_DATA_DIR=/persistent/talentsift-data
```

The configured directory contains runtime application data such as:

```text
talentsift.db
scorecards/
fairness_tests.json
```

## Deployment Readiness

The current MVP runs as a FastAPI web application.

A basic production process can be started with:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

A hosting provider may provide its own port configuration, in which case the
launch command should be adapted to that environment.

### HTTPS and WebSockets

TalentSift automatically chooses the browser WebSocket protocol from the page
protocol:

```text
HTTP  -> ws://
HTTPS -> wss://
```

A deployed candidate-facing application should therefore use HTTPS.

### Persistent storage

The current MVP uses:

- SQLite for candidates, sessions, and transcripts
- JSON files for scorecards
- JSON storage for controlled fairness-test records

Production deployment must use persistent storage for the configured
`TALENTSIFT_DATA_DIR`.

Deploying these files only to an ephemeral application filesystem could result
in interview records being lost when the service restarts or redeploys.

The local `data/` directory is excluded from Git so runtime recruitment data is
not committed to the source repository.

## Current MVP Limitations

The internship MVP currently has several known limitations.

Speech transcription can sometimes distort candidate responses, including
technical terms and statements describing personal contribution.

A mid-interview upstream WebSocket failure currently terminates the interview.
TalentSift retries OpenAI Realtime startup connections, but automatic
mid-interview conversation reconstruction is not yet implemented.

Recruiter authentication and role-based access control are not yet implemented.
The current recruiter identity used for overrides is a development placeholder.

SQLite and JSON storage are suitable for the current MVP and local development,
but a larger multi-user deployment would require a stronger persistence and
concurrency architecture.

Controlled fairness testing currently uses a small development sample and
should be expanded before production conclusions are made.

PDF interview-report export is not currently implemented.

TalentSift does not claim to eliminate hiring bias. Recruiters remain
responsible for hiring decisions and should review interview evidence alongside
AI-assisted evaluation outputs.
