# TalentSift MVP: jobs and automatically generated IDs

## Try the new flow locally

1. From the project root, start the existing FastAPI server: `uvicorn app:app --reload`.
2. Visit `http://127.0.0.1:8000/recruiter/jobs` and enter a job title such as **Software Engineer**.
3. The server generates a UUID, saves the job to `data/talentsift.db`, and opens its recruiter dashboard.
4. Copy the **Candidate interview link** from that dashboard. It looks like `http://127.0.0.1:8000/?job_id=<generated-job-id>`.
5. Open the link. The candidate sees the stored job title, enters their name, reads the notice, selects a language, and gives consent.
6. `POST /api/session` now generates a candidate UUID and an interview session UUID. The existing microphone, WebSocket, timer, interview, and recruiter scoring flows use that session as before.

No manual database seeding or fixed candidate ID is needed. The SQLite startup setup adds a `jobs` table to an existing database without clearing its candidate or interview rows. Keep `data/talentsift.db` if you want jobs and past interviews to survive restarts.

## Where the values come from

| Value | Created by | Stored / passed to |
| --- | --- | --- |
| Job title | Recruiter form at `/recruiter/jobs` | SQLite `jobs` table; candidate and recruiter headings |
| Job ID | `JobListing` default UUID | SQLite `jobs`; candidate link query string; session `job_id` |
| Candidate ID | `Candidate` default UUID after consent | SQLite `candidates`; session `candidate_id` |
| Session ID | Existing `Session` default UUID | SQLite `sessions`; existing WebSocket route |

The browser looks up the job with `GET /api/jobs/{job_id}` and will not enable **Start** for a missing or invalid job link. The server also checks that a new candidate belongs to a saved job before storing candidate data. A session is not created without consent or a nonempty candidate name. Recruiter pages retrieve titles from the database; older interviews without a saved job title still show their job ID.

## Current MVP boundary

Changing a title changes display and interview grouping. The current interviewer prompt and scoring rubric remain tuned for backend/Python engineering roles; this update does not make the questions or evaluation criteria job-specific. Recruiter routes still need authentication and invitation links need access controls before any public candidate deployment. Each new consent creates a separate candidate record; matching repeat visits to one person is future work.
