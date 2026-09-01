# TalentSift AI

**Automated Job Pre-Screening Interviewer — HR Tech / B2B**

TalentSift AI is an AI-assisted interview platform designed to help HR teams
automate and structure parts of the job pre-screening process.

This repository contains my work for the **TalentSift AI Product Management &
Engineering Internship**, where I am building and improving features related to:

- Real-time voice communication
- AI-assisted interviews
- Structured candidate evaluation
- Scoring guardrails and bias testing
- Professional software engineering practices

## Technologies

- Python
- Git & GitHub
- WebSockets
- asyncio
- Pydantic
- pytest

## Testing

Run the automated Python tests with:

```bash
pytest -v
```

Run the scoring tests specifically with:

```bash
python -m pytest tests/test_scoring.py -q
```

Run the evaluator guardrail tests with:

```bash
python guardrail_check.py
```

Run the evaluator consistency tests with:

```bash
python consistency_check.py
```

The current evaluator tests cover areas including:

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

These tests are controlled development tests and do not prove that the system
is universally unbiased or production-ready.

## Recruitment Data Retention

TalentSift follows a data-minimization approach.

For unsuccessful applications, interview transcripts and evaluation results
should be deleted no later than three months after the relevant recruitment
process has ended, unless a documented lawful purpose requires a different
retention period.

Where candidate data is retained for future recruitment opportunities, the
candidate must be appropriately informed, and the retention period should
normally not exceed two years from the candidate's last contact, subject to
applicable law and organizational policy.

Retention periods must be configurable by the organization deploying TalentSift
rather than assumed to be universally applicable.