# Week 5 Bias and Fairness Test Matrix

## Speaking-rate test

Controlled setup:
- Same interviewer question
- Same scripted candidate answer
- Same speaker
- Same microphone/environment
- Only speaking rate was intentionally changed

| Speaking rate | Transcript observation | Relevant Experience | Problem Solving | Communication | Overall |
|---|---|---:|---:|---:|---:|
| Slow | Mostly preserved, but "Flask" was transcribed as "first" | 3 | 3 | 3 | 3.0 |
| Normal | Most technical evidence preserved, but part of "One problem I encountered..." was distorted | 3 | 3 | 3 | 3.0 |
| Fast | Largest distortion: "I built a Flask backend" became "With a flashback..." and "I implemented" became "I've met" | 3 | 3 | 3 | 3.0 |

### Finding

Scores were identical across the three controlled runs.

However, transcription errors occurred at every speaking rate, and the fast
run showed the greatest distortion of explicit technical and ownership evidence.

This does not prove fairness across speaking rates. It provides limited
development evidence that the evaluator remained score-consistent when enough
core evidence survived, while also identifying speech transcription as an
upstream reliability and potential fairness risk.

### Remaining audio testing

- Multi-accent comparison: pending
- Additional speakers: pending
- Larger repeated speaking-rate sample: future work