from models import Session, TranscriptTurn
from scoring.models import CompetencyScore, Scorecard
from scoring.rubric import ROLE_RUBRIC
import scoring.session_scoring as session_scoring_module
from storage import SessionRepo
from scoring.session_scoring import format_scorecard_summary

def test_score_saved_session_persists_scorecard(tmp_path, monkeypatch):
    # ---------------------------------------------------------
    # 1. CREATE ISOLATED TEST STORAGE
    # ---------------------------------------------------------
    # tmp_path is a temporary directory created by pytest.
    #
    # This means the test does NOT write into the real:
    # data/
    # data/scorecards/
    #
    # folders used by TalentSift during a real interview.

    session_repo = SessionRepo(
        root=str(tmp_path / "sessions")
    )

    scorecard_root = tmp_path / "scorecards"

    # ---------------------------------------------------------
    # 2. CREATE A REAL SESSION
    # ---------------------------------------------------------
    # We intentionally use a partial interview.
    #
    # Relevant Experience was explored.
    # The other competencies were not.
    #
    # This exercises the new not_explored semantics.

    session = Session(
        job_id="junior-python-backend",
        candidate_id="candidate-test-001",
        status="completed",
        language="English",
        consent_given=True,
        transcript=[
            TranscriptTurn(
                speaker="interviewer",
                text="Tell me about a backend project you've worked on.",
                item_id="turn-1",
            ),
            TranscriptTurn(
                speaker="candidate",
                text=(
                    "My name is Alice Smith. "
                    "I built a Flask backend for a booking application "
                    "and implemented the REST endpoints and authentication."
                ),
                item_id="turn-2",
            ),
        ],
    )

    session_repo.save(session)

    # ---------------------------------------------------------
    # 3. PREPARE THE SCORECARD THE FAKE EVALUATOR WILL RETURN
    # ---------------------------------------------------------
    # We are NOT testing the LLM's judgement here.
    #
    # We are testing what the application does once a valid
    # Scorecard comes back from the evaluator.

    fake_scorecard = Scorecard(
        session_id=session.id,
        scores=[
            CompetencyScore(
                name="Relevant Experience",
                status="scored",
                score=4,
                evidence=[
                    (
                        "My name is Alice Smith. "
                        "I built a Flask backend for a booking application "
                        "and implemented the REST endpoints and authentication."
                    )
                ],
                justification=(
                    "The candidate described concrete hands-on backend work."
                ),
            ),
            CompetencyScore(
                name="Problem Solving",
                status="not_explored",
                score=None,
                evidence=[],
                justification="The competency was not explored.",
            ),
            CompetencyScore(
                name="Communication",
                status="not_explored",
                score=None,
                evidence=[],
                justification="The competency was not explored.",
            ),
            CompetencyScore(
                name="Role Motivation",
                status="not_explored",
                score=None,
                evidence=[],
                justification="The competency was not explored.",
            ),
            CompetencyScore(
                name="Culture & Values Fit",
                status="not_explored",
                score=None,
                evidence=[],
                justification="The competency was not explored.",
            ),
        ],
        overall=4.0,
    )

    # ---------------------------------------------------------
    # 4. REPLACE ONLY THE EXTERNAL SCORING CALL
    # ---------------------------------------------------------

    def fake_score_transcript(
        session_id,
        scoring_transcript,
        rubric,
        evidence_transcript,
    ):
        # The real session ID reached the scorer.
        assert session_id == session.id

        # The production role rubric reached the scorer.
        assert rubric is ROLE_RUBRIC

        # Interviewer and candidate turns were assembled.
        assert "Interviewer:" in scoring_transcript
        assert "Candidate:" in scoring_transcript

        # Candidate identity was removed from evaluator-facing text.
        assert "[CANDIDATE_NAME]" in scoring_transcript
        assert "Alice Smith" not in scoring_transcript

        # But original candidate speech was preserved for evidence.
        assert "Alice Smith" in evidence_transcript
        assert (
            "I built a Flask backend for a booking application"
            in evidence_transcript
        )

        return fake_scorecard

    monkeypatch.setattr(
        session_scoring_module,
        "score_transcript",
        fake_score_transcript,
    )

    # ---------------------------------------------------------
    # 5. REDIRECT SCORECARD STORAGE
    # ---------------------------------------------------------
    # score_saved_session() normally creates:
    #
    # ScorecardRepo()
    #
    # which writes into data/scorecards.
    #
    # We replace only the constructor so this test writes into
    # pytest's temporary directory instead.

    real_scorecard_repo = session_scoring_module.ScorecardRepo

    monkeypatch.setattr(
        session_scoring_module,
        "ScorecardRepo",
        lambda: real_scorecard_repo(
            root=str(scorecard_root)
        ),
    )

    # ---------------------------------------------------------
    # 6. RUN THE REAL APPLICATION INTEGRATION FUNCTION
    # ---------------------------------------------------------

    scorecard, scorecard_path = (
        session_scoring_module.score_saved_session(
            session_id=session.id,
            session_repo=session_repo,
        )
    )

    # ---------------------------------------------------------
    # 7. VERIFY THE RESULT
    # ---------------------------------------------------------

    assert scorecard.session_id == session.id
    assert scorecard.overall == 4.0

    # score_saved_session really persisted the scorecard.
    assert scorecard_path.exists()

    saved_json = scorecard_path.read_text(
        encoding="utf-8"
    )

    assert '"Relevant Experience"' in saved_json
    assert '"not_explored"' in saved_json
    assert '"score": null' in saved_json
    assert '"overall": 4.0' in saved_json



def test_format_scorecard_summary_marks_not_explored_without_numeric_score():
    scorecard = Scorecard(
        session_id="session-summary-test",
        scores=[
            CompetencyScore(
                name="Relevant Experience",
                status="scored",
                score=4,
                evidence=["I built a Flask backend."],
                justification="Concrete backend work was described.",
            ),
            CompetencyScore(
                name="Problem Solving",
                status="not_explored",
                score=None,
                evidence=[],
                justification="The competency was not explored.",
            ),
        ],
        overall=4.0,
    )

    summary = format_scorecard_summary(scorecard)

    assert "Relevant Experience" in summary
    assert "Problem Solving" in summary
    assert "scored" in summary
    assert "not_explored" in summary
    assert "Overall: 4.0/5" in summary

    problem_solving_line = next(
        line
        for line in summary.splitlines()
        if "Problem Solving" in line
    )

    assert "-" in problem_solving_line
    