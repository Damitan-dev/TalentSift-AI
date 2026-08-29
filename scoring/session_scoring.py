from pathlib import Path

from scoring.engine import EvaluationFailedError, score_transcript
from scoring.rubric import ROLE_RUBRIC
from storage import SessionRepo
from scoring.text_normalization import normalize_for_scoring
from scoring.privacy import redact_candidate_identity


class ScorecardRepo:
    def __init__(self, root: str = "data/scorecards"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, scorecard) -> Path:
        path = self.root / f"{scorecard.session_id}.json"

        path.write_text(
            scorecard.model_dump_json(indent=2),
            encoding="utf-8",
        )

        return path


def build_transcripts(session) -> tuple[str, str, str]:
    """
    Produces three transcript versions:

    1. full_transcript:
       Original interviewer + candidate speech.
       Preserved for HR/audit purposes.

    2. scoring_transcript:
       Interviewer questions + cleaned candidate speech.
       Used by the AI evaluator.

    3. evidence_transcript:
   Original candidate speech only.
   Used ONLY as the authoritative source for verbatim evidence.
   Interviewer speech is intentionally excluded because it is never
   valid candidate evidence.
    """

    full_lines = []
    scoring_lines = []
    evidence_lines = []

    for turn in session.transcript:
        speaker = turn.speaker.strip().lower()
        text = turn.text.strip()

        if not text:
            continue

        if speaker == "candidate":

            # --------------------------------------------------
            # ORIGINAL CANDIDATE TEXT
            # --------------------------------------------------
            # NEVER modify this copy.
            # This is the authoritative evidence source.

            full_lines.append(
                f"Candidate: {text}"
            )

            evidence_lines.append(text)

            # --------------------------------------------------
            # SCORING COPY
            # --------------------------------------------------
            # Candidate identity is removed and language is
            # normalized ONLY in this scoring representation.

            scoring_text = normalize_for_scoring(
                redact_candidate_identity(text)
            )

            scoring_lines.append(
                f"Candidate: {scoring_text}"
            )

        elif speaker == "interviewer":
            full_lines.append(
                f"Interviewer: {text}"
            )

            scoring_lines.append(
                f"Interviewer: {text}"
            )

        else:
            raise ValueError(
                f"Unknown transcript speaker: {turn.speaker!r}"
            )

    return (
        "\n\n".join(full_lines),
        "\n\n".join(scoring_lines),
        "\n\n".join(evidence_lines),
    )


def score_saved_session(
    session_id: str,
    session_repo: SessionRepo,
):
    """
    Load a completed persisted session,
    construct the required transcript representations,
    score it, and persist the scorecard.
    """

    session = session_repo.load(session_id)

    (
        full_transcript,
        scoring_transcript,
        evidence_transcript,
    ) = build_transcripts(session)

    if not evidence_transcript:
        raise ValueError(
            "Cannot score a session with no candidate transcript."
        )

    scorecard = score_transcript(
        session_id=session.id,
        scoring_transcript=scoring_transcript,
        rubric=ROLE_RUBRIC,
        evidence_transcript=evidence_transcript,
    )

    scorecard_path = ScorecardRepo().save(scorecard)

    return scorecard, scorecard_path