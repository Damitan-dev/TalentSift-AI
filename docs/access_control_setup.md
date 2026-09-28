# TalentSift: recruiter login and candidate invitations

This update implements the invite-only beta discussed with Damitan. It includes
recruiter login/logout, names assigned through individual invitations, and
server-side usage controls. It adds tables to the existing SQLite database;
it does not delete jobs, candidates, transcripts, scorecards or recordings.

## Install locally on Windows

1. Stop Uvicorn with Ctrl+C. Back up the project and its `data` directory while
   the server is stopped. Keep the backup private: it contains interview data.
2. Extract **TalentSift-access-control.zip** into your existing **TalentSift-AI**
   project. Replace the matching files and preserve the `client`, `templates`,
   `tests` and `docs` folders. The archive root contains `app.py` beside the new
   `access_control.py`, `access_routes.py` and `manage_access.py` modules.
   Do not move only `app.py` or only the HTML: these files work together.
3. Activate your existing virtual environment. No new Python packages are
   required beyond the project's existing requirements (Python 3.11+).
4. From the directory containing `app.py`, run:

   ```powershell
   python manage_access.py create-recruiter
   ```

   Choose your own username and a 15–128 character password/passphrase.
   Password entry is hidden, including on Windows: typing shows no characters.
   Do not put the password in source code, a URL, or a command-line argument.
   There is no default account/password in this release.
5. Keep your existing server-side `OPENAI_API_KEY` configuration. Start:

   ```powershell
   uvicorn app:app --reload
   ```

6. Open **http://127.0.0.1:8000/login**, sign in, then open or create a job.
   Hard-refresh the candidate/recruiter pages after replacing the files.

If you forget the local password, stop the server and run
`python manage_access.py reset-recruiter`. This replaces the one account and
revokes existing recruiter login sessions. If environment bootstrap variables
are configured, update those too; they are authoritative at startup.

## Recruiter workflow

From a job dashboard, enter the candidate's name and select an expiry: 24 hours,
3 days (default), or 7 days. Click **Create invitation**, copy the personal link,
and send it yourself. No email or messaging service is configured or contacted.

The secret link is displayed only on that confirmation page. The database keeps
its hash, so the dashboard cannot redisplay the secret later. If a link is lost,
cancel the available invitation from the job dashboard and issue a new one.
A mistaken name is corrected in the same way. Each invitation is for one person
and one interview; an unused link could still be forwarded to someone else.

The dashboard shows invitation status and the daily/concurrent interview counts.
Automatic refresh pauses while you are filling in the invitation form. Existing
interview results and downloads now require your login. Score overrides and
restoring AI scores record your authenticated username in their audit history.

## Candidate workflow and retries

The candidate opens the personal link and sees **Welcome, [their name]** and
the job title. Their name is read-only and supplied by the server. They still
choose a language, consent, opt into recording if wanted, and select a microphone.

Old `/?job_id=...` shared links no longer grant interview access. Generate a
new personal invitation for every candidate, including your own test interviews.

- Viewing the link, accepting consent, or retrying microphone setup does not
  consume it. Repeating consent reuses the same pending session.
- When the interview WebSocket is admitted, one start is counted and the
  invitation is consumed **before** the server contacts OpenAI.
- If the service fails during startup or the connection breaks afterward,
  the attempt stays counted. This is deliberate: some provider usage may have
  occurred, and automatic resets would permit repeated paid attempts.
- A retry after a consumed attempt requires the recruiter to issue another
  invitation. There is no hidden automatic mid-interview reconnect.
- A busy-slot rejection leaves the invitation available; the candidate can
  reload and try later. The invitation must still be unexpired when they start.
- If a page is refreshed before recording starts, an unused recording upload
  credential is replaced. Existing audio is never overwritten. If recording
  has already begun, a later attempt to initialize recording reports that it
  is unavailable; the interview can continue without another recording.

## Adjustable server limits

Add these to local `.env` or the hosting service's environment settings:

```dotenv
TALENTSIFT_DAILY_INTERVIEWS=10
TALENTSIFT_CONCURRENT_INTERVIEWS=2
TALENTSIFT_INTERVIEW_SECONDS=600
```

Restart the server after changes. These are global limits for this one-recruiter
beta, not separate allowances per job or candidate. The daily count resets at
**00:00 UTC (01:00 Nigeria time)** and counts admitted starts, including failures.
Finishing an interview releases its concurrent slot but does not refund its
start. Restarting the app does not reset the counters.

The normal interview window starts after the opening playback. A separate
server deadline covers the whole paid connection: configured interview time
plus 120 seconds for startup/opening/closing/scoring. At the default settings,
that is a 10-minute interview window and a 12-minute total connection deadline.
An already submitted scoring HTTP request runs in a worker thread and may finish
after relay cancellation; the deadline cannot undo provider work already accepted.
An additional 30-second lease margin allows cleanup after a process crash.
Expired leases free capacity without reusing the invitation or erasing usage.

Incoming microphone data must be PCM16/base64 and is limited to real-time
24 kHz audio with a ten-second buffering allowance. Sending hours of audio
quickly cannot bypass the duration limit. Recording upload size limits remain
in place too.

These are usage limits, **not an exact currency budget**. OpenAI charges vary
with actual input/output and optional live captions/scoring. The implementation
does not modify the OpenAI account's billing settings.

## Before deploying this version

Use one application instance with a persistent local volume for this SQLite MVP.
You can use multiple workers sharing the same local database; the admission
transaction prevents quota races. Do not launch independent replicas with
separate databases: each would have its own allowance. For a multi-instance
service, move the database and admission logic to shared transactional storage.

Set these on the host:

```dotenv
TALENTSIFT_PUBLIC_URL=https://your-actual-hostname.example
TALENTSIFT_DATA_DIR=/your/persistent/mount/data
TALENTSIFT_DAILY_INTERVIEWS=10
TALENTSIFT_CONCURRENT_INTERVIEWS=2
TALENTSIFT_INTERVIEW_SECONDS=600
```

Replace both example values with the real HTTPS origin and mounted directory.
The public URL must contain only the origin, with no path or query. Without it,
the app permits only loopback development addresses. With it, login/candidate
cookies use the Secure and HttpOnly flags and the `__Host-` prefix. Configure
your host to preserve the public Host header and serve HTTPS. Only trust your
actual reverse proxy for forwarded headers; do not accept forwarded client IP
headers from arbitrary internet clients. Same-origin WebSockets derive `wss://`
from the page URL.

Keep **all** of `TALENTSIFT_DATA_DIR` persistent: SQLite, recordings, scorecards
and fairness data. A restart survives while the underlying files survive;
ephemeral storage wiped by redeployment cannot preserve accounts or quotas.
Back up the data privately. Authentication does not provide encryption at rest.

Create the recruiter account in the host shell with `manage_access.py`, or use
these bootstrap settings if a shell is unavailable:

1. Run `python manage_access.py password-hash` locally. Enter/confirm your
   password at the hidden prompt; copy the resulting hash.
2. Set `TALENTSIFT_RECRUITER_USERNAME` and
   `TALENTSIFT_RECRUITER_PASSWORD_HASH` in the host's environment dashboard.
   Paste the exact full hash, including its `$` separators. Store the hash as
   sensitive configuration; do not commit it.
3. On startup, those values configure the one account. Changing them rotates
   the account and revokes existing recruiter logins. Keeping the same values
   does not log everyone out on each restart.

Use a dedicated OpenAI project/key and configure its spending alerts and an
**enforced monthly hard limit** in the OpenAI dashboard. Alerts alone do not
stop usage; hard-limit enforcement may lag slightly. See
[OpenAI spend limits](https://developers.openai.com/api/docs/guides/spend-limits).
These account settings must be configured by the account owner; they were not
changed by this code update.

## Verification and limits of testing

The release passed **75 Python tests and 19 JavaScript tests**. It was tested with isolated SQLite databases and a simulated OpenAI
connection. The automated suite covers existing recording, live caption,
transcript ordering and connection behavior alongside the new access controls.
An actual local Uvicorn HTTP/WebSocket smoke test also covered login, job and
invitation creation, candidate cookies, recording transport, protected downloads,
used-link denial and logout. No OpenAI credits were spent on these tests.

A rendered browser walkthrough could not run because the browser executable
was unavailable and its download failed. JavaScript tests execute the real
candidate script with mocked DOM/audio APIs. No live microphone or real OpenAI
interview was run in this environment.

Run the automated checks from the project directory:

```powershell
python -m pytest -q
node --test tests/*.cjs
```

Then run one real local interview with a new invitation, including recording,
and repeat on the deployed HTTPS URL before inviting actual applicants. Verify
that signing out prevents direct transcript/audio downloads. This release
contains no deployed URL and is not a multi-organization production service.
