# Why Bianca could leave out a topic, and what changed

The old prompt told Bianca to explore all five competencies, but the application
did not check that before accepting `finish_interview`. The finishing instructions
also allowed an early finish when Bianca thought no more useful questions remained.
Collaboration, feedback, ownership and learning were grouped into one competency,
so a question about one behavior could crowd out the others.

Those are gaps in the implementation. Without the transcript and duration of a
particular interview, they do not prove why that specific interview missed a topic.
The hard time limit, a candidate ending early, or a connection failure can also
leave areas unexplored.

## The required plan

There are now six core questions across the existing five scoring areas. The
server supplies them in this order, in the candidate's selected language:

| Core topic | Scoring area | Purpose |
| --- | --- | --- |
| Project experience | Relevant Experience | The candidate's own contribution |
| Technical problem | Problem Solving | Investigation and resolution |
| Collaboration | Culture & Values Fit | Working with others and contributing to a team |
| Explaining an API | Communication | Explaining a technical idea to a new teammate |
| Feedback or a mistake | Culture & Values Fit | Ownership, learning and following through |
| Interest in the role | Role Motivation | Motivation and goals |

Collaboration has its own question. A feedback answer cannot replace it.
The experience question serves as the warm-up, so there is no extra introductory
question taking time away from the required topics. Bianca still introduces
herself, gives the existing disclosure and asks whether the candidate is ready.

The core questions are fixed for consistency. Bianca can ask a short, relevant
follow-up; the existing limit of one useful follow-up per competency remains.
When time is short relative to the remaining topics, per-turn instructions tell
her to prioritize the next core question over an optional follow-up.

## How the check works

1. After readiness is confirmed, Bianca calls `next_interview_topic`.
2. The server chooses the next question. Bianca is instructed to say it verbatim.
3. The server looks for that full question in a completed, allowed interviewer
   audio response. It tolerates punctuation and spacing differences. A related
   keyword or a tool call alone cannot mark a question as asked.
4. A candidate audio turn must start after the question has been generated and
   then be committed. A stale or duplicate audio event cannot answer a new topic.
5. After listening to the answer, Bianca requests the next topic. The server
   advances at most one topic, and only when those checks have passed.
6. A normal `finish_interview` request is rejected while required opportunities
   remain. The response supplies the next required question. Only an accepted
   finish starts the fixed closing and stops the timer.

The check uses native audio events, so it does not wait for the final candidate
transcription before replying. Tool continuations use the existing response
scheduler, wait for resumed speech to finish and share its single response slot.
The topic transition requires an additional model/tool round trip; real-world
latency still needs to be measured in a live interview.

## What this does and does not establish

This is a **question-opportunity check**, not a second grading system. Receiving
a candidate audio turn does not prove that the answer was useful, complete or
even on topic. Bianca still interprets whether it was an answer, a refusal, a
request to wait or a request for clarification. The evaluator still decides what
evidence the final transcript supports.

A generated question is also not proof that the candidate heard every word of
its playback. Interrupted, failed or suppressed generations do not qualify;
if the core question is paraphrased instead of read as supplied, the conservative
check may ask it again. Test interruptions and wording in a real voice session.

The 10-minute maximum and the candidate's End Interview button can end a session
with missing topics. A connection failure can also end it. The check does not
extend the budget or force the candidate to continue. Missing areas must remain
`not_explored` where the evaluator finds no meaningful opportunity, rather than
being assigned a low score merely because the interviewer omitted them.

Coverage lives in memory for the active relay and is printed in server logs as
`[coverage]`. It is not a new saved recruiter-dashboard field. The final log
distinguishes `all_topics` from `time_limit` and lists remaining topics. The saved
transcript remains the record recruiters can inspect.

The current questions still target the MVP's Python backend role. Changing a job
title in the dashboard does not create a new role-specific question plan or rubric.

## Files and installation

Copy these **four runtime files together**, preserving their relative paths:

| File | Change |
| --- | --- |
| `interview_coverage.py` (new) | Six bilingual core questions, coverage state, per-turn guidance and tool definitions |
| `app.py` | Connects coverage to audio events, handles the next-topic tool and checks completion |
| `interview_turns.py` | Schedules ordinary tool continuations without overlapping candidate speech |
| `prompts/interviewer_prompt.txt` | Uses the server plan and permits continuing after a rejected early finish |

The update also includes coverage/relay/scheduler tests, this guide, and updated
README/product documentation. No new dependency or database migration is required.
Keep your existing `.env`, database, recordings and recruiter account.

Stop the development server before replacing files, then run from your project:

```bash
python -m pytest -q
node --test tests/*.cjs
uvicorn app:app --reload
```

Start a **new** interview using a fresh unused invitation. An already-running
interview keeps its original prompt/state and is interrupted if its server reloads.

In a short live test, answer every core question briefly. Confirm that all six
appear in the transcript, including collaboration and feedback/ownership. Try
asking for a repeat once; Bianca should remain on the same topic. Verify a normal
finish log has `"reason": "all_topics"` and `"remaining": []`.

## Verification performed

The shared project passes 98 Python tests and 19 JavaScript tests. Coverage tests
exercise both English and French, missing questions, premature completion after
each topic, duplicate/late audio events, cancelled generations, interrupted tool
continuations, the hard deadline and candidate-controlled stopping. Existing
transcript-ordering, authentication and recording tests also pass.

These tests use controlled OpenAI/browser events. They are not a real microphone
interview, a live OpenAI call or a deployment test. Those remain manual checks.

Suggested commit message:

```text
fix: enforce required interview coverage before normal completion
```
