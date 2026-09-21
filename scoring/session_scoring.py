from pathlib import Path
from config import DATA_DIR
from models import utc_now
from scoring.engine import EvaluationFailedError, score_transcript
from scoring.models import Scorecard
from scoring.rubric import ROLE_RUBRIC
from storage import SessionRepo
from scoring.text_normalization import normalize_for_scoring
from scoring.privacy import redact_candidate_identity
from database import load_session

class ScorecardRepo:
    def __init__(
        self,
        root: str | Path | None = None,
    ):
        if root is None:

            self.root = (
                DATA_DIR
                / "scorecards"
            )

        else:

            self.root = Path(
                root
            )
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, scorecard) -> Path:
        path = self.root / f"{scorecard.session_id}.json"

        path.write_text(
            scorecard.model_dump_json(indent=2),
            encoding="utf-8",
        )

        return path

    def load(
        self,
        session_id: str,
    ) -> Scorecard:
        """
        Load one saved Scorecard using its Session ID.
        """

        # Scorecards are stored using:
        #
        # data/scorecards/<session-id>.json
        path = (
            self.root
            / f"{session_id}.json"
        )


        # Read the saved JSON file as text.
        json_text = path.read_text(
            encoding="utf-8"
        )


        # Convert the JSON back into our validated
        # Pydantic Scorecard model.
        return Scorecard.model_validate_json(
            json_text
        )


def apply_recruiter_override(
    session_id: str,
    score: float,
    reason: str,
    overridden_by: str,
) -> Scorecard:
    """
    Apply an auditable human override to one Scorecard.

    The original AI overall score is preserved.
    """

    # --------------------------------------------------
    # 1. BASIC INPUT CHECKS
    # --------------------------------------------------

    # A reason is required because an unexplained
    # override would not be auditable.
    if not reason.strip():
        raise ValueError(
            "Recruiter override requires a reason."
        )


    # Until TalentSift has recruiter authentication,
    # the caller must explicitly tell us who made
    # the override.
    if not overridden_by.strip():
        raise ValueError(
            "Recruiter override requires overridden_by."
        )


    # --------------------------------------------------
    # 2. LOAD EXISTING SCORECARD
    # --------------------------------------------------

    repo = ScorecardRepo()


    scorecard = repo.load(
        session_id
    )


    # --------------------------------------------------
    # 3. BUILD A NEW VALIDATED SCORECARD
    # --------------------------------------------------
    #
    # IMPORTANT:
    #
    # We do NOT change:
    #
    # scorecard.overall
    #
    # That remains the original AI result.
    #
    # Instead we add the recruiter's separate decision.

    updated_data = scorecard.model_dump()

    override_time = utc_now()


    history = list(
        scorecard.override_history
    )


    history.append(
        {
            "action": "override",
            "score": score,
            "reason": reason.strip(),
            "actor": overridden_by.strip(),
            "at": override_time,
        }
    )

    updated_data.update(
        {
            "recruiter_override": 
                score,

            "override_reason":
                reason.strip(),

            "overridden_by":
                overridden_by.strip(),

            "overridden_at":
                utc_now(),

            "override_history":
                history,
        }
    )


    # Run everything through Scorecard validation again.
    #
    # This checks:
    #
    # - override score is between 1 and 5
    # - reason exists
    # - who exists
    # - timestamp exists
    updated_scorecard = Scorecard.model_validate(
        updated_data
    )


    # --------------------------------------------------
    # 4. SAVE UPDATED SCORECARD
    # --------------------------------------------------

    repo.save(
        updated_scorecard
    )


    return updated_scorecard

def restore_ai_score(
    session_id: str,
    reason: str,
    restored_by: str,
) -> Scorecard:
    """
    Remove the current recruiter override and
    return the effective ranking score to the
    original AI overall.

    The previous override remains in audit history.
    """

    if not reason.strip():
        raise ValueError(
            "Restoring the AI score requires a reason."
        )


    if not restored_by.strip():
        raise ValueError(
            "Restoring the AI score requires restored_by."
        )


    repo = ScorecardRepo()


    scorecard = repo.load(
        session_id
    )


    if scorecard.recruiter_override is None:
        raise ValueError(
            "This Scorecard does not currently have an override."
        )


    restore_time = utc_now()


    history = list(
        scorecard.override_history
    )


    history.append(
        {
            "action": "restore_ai",
            "score": scorecard.overall,
            "reason": reason.strip(),
            "actor": restored_by.strip(),
            "at": restore_time,
        }
    )


    updated_data = scorecard.model_dump()


    updated_data.update(
        {
            # Clear ONLY the current override state.
            #
            # History remains preserved.
            "recruiter_override":
                None,

            "override_reason":
                None,

            "overridden_by":
                None,

            "overridden_at":
                None,

            "override_history":
                history,
        }
    )


    updated_scorecard = Scorecard.model_validate(
        updated_data
    )


    repo.save(
        updated_scorecard
    )


    return updated_scorecard

def format_scorecard_summary(scorecard) -> str:
    """
    Create a compact terminal-friendly summary of a scorecard.

    A not_explored competency is displayed with "-" rather than
    being presented as a numeric score.
    """

    lines = [
        "",
        "SCORECARD SUMMARY",
        "-" * 72,
        f"{'Competency':<28} {'Status':<16} {'Score':<8}",
        "-" * 72,
    ]

    for result in scorecard.scores:
        if result.score is None:
            score_text = "-"
        else:
            score_text = str(result.score)

        lines.append(
            f"{result.name:<28} "
            f"{result.status:<16} "
            f"{score_text:<8}"
        )

    lines.append("-" * 72)

    if scorecard.overall is None:
        lines.append("Overall: unavailable")
    else:
        lines.append(f"Overall: {scorecard.overall}/5")

    return "\n".join(lines)


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


def score_session(session):
    """
    Score one already-loaded TalentSift Session.

    This is storage-independent.

    The Session may have come from:
    - the old JSON SessionRepo
    - the new SQLite database
    """

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


    scorecard_path = ScorecardRepo().save(
        scorecard
    )


    return (
        scorecard,
        scorecard_path,
    )

def score_saved_session(
    session_id: str,
    session_repo: SessionRepo,
):
    """
    Load a Session from the old JSON repository
    and score it.
    """

    session = session_repo.load(
        session_id
    )


    return score_session(
        session
    )


def score_database_session(
    session_id: str,
):
    """
    Load a Session from the new SQLite database
    and score it.
    """

    session = load_session(
        session_id
    )


    return score_session(
        session
    )