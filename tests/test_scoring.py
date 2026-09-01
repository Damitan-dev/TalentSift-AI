import pytest

from scoring.models import CompetencyScore, Scorecard
from scoring.engine import score_transcript, verify_evidence_quotes


def test_score_bounds():
    # Real competency scores must stay on TalentSift's 1-5 scale.

    with pytest.raises(ValueError):
        CompetencyScore(
            name="Problem Solving",
            status="scored",
            score=6,
            evidence=["I tried to solve the issue."],
            justification="Invalid score.",
        )


def test_scored_competency_requires_evidence():
    # Every real score, including score 1, must be supported
    # by candidate evidence.

    with pytest.raises(ValueError):
        CompetencyScore(
            name="Problem Solving",
            status="scored",
            score=4,
            evidence=[],
            justification="Strong problem solving.",
        )


def test_score_one_still_requires_evidence():
    # Score 1 now means the competency WAS assessed,
    # but the demonstrated evidence was weak.
    #
    # It is no longer used to mean "not explored".

    with pytest.raises(ValueError):
        CompetencyScore(
            name="Problem Solving",
            status="scored",
            score=1,
            evidence=[],
            justification="Weak problem solving.",
        )


def test_not_explored_has_no_score_or_evidence():
    # A competency that was never assessed must not
    # be represented as a fake low score.

    competency = CompetencyScore(
        name="Problem Solving",
        status="not_explored",
        score=None,
        evidence=[],
        justification="not explored",
    )

    assert competency.status == "not_explored"
    assert competency.score is None
    assert competency.evidence == []


def test_not_explored_rejects_score():
    # These states contradict each other:
    #
    # status says "not assessed"
    # but score says "assessed"

    with pytest.raises(ValueError):
        CompetencyScore(
            name="Problem Solving",
            status="not_explored",
            score=3,
            evidence=[],
            justification="not explored",
        )


def test_weighted_overall():
    scorecard = Scorecard(
        session_id="test-session",
        scores=[
            CompetencyScore(
                name="Problem Solving",
                status="scored",
                score=5,
                evidence=[
                    "I redesigned the membership model."
                ],
                justification=(
                    "Strong problem-solving evidence."
                ),
            ),
            CompetencyScore(
                name="Communication",
                status="scored",
                score=3,
                evidence=[
                    "I explained the change to my teammate."
                ],
                justification="Adequate communication.",
            ),
            CompetencyScore(
                name="Relevant Experience",
                status="scored",
                score=4,
                evidence=[
                    "I built authentication and authorization."
                ],
                justification="Relevant backend experience.",
            ),
        ],
    )

    weights = {
        "Problem Solving": 40,
        "Communication": 20,
        "Relevant Experience": 40,
    }

    # (5×40 + 3×20 + 4×40) / 100
    # = 420 / 100
    # = 4.2

    result = scorecard.compute_overall(weights)

    assert result == 4.2


def test_not_explored_is_excluded_from_overall():
    # A competency that was not assessed must not be treated
    # as though the candidate demonstrated poor performance.
    #
    # Only scored competencies contribute to the current
    # weighted overall result.
    scorecard = Scorecard(
        session_id="partial-session",
        scores=[
            CompetencyScore(
                name="Problem Solving",
                status="scored",
                score=5,
                evidence=[
                    "I reproduced the problem and tested the fix."
                ],
                justification="Strong demonstrated process.",
            ),
            CompetencyScore(
                name="Communication",
                status="scored",
                score=3,
                evidence=[
                    "I explained the solution to my teammate."
                ],
                justification="Adequate communication.",
            ),
            CompetencyScore(
                name="Relevant Experience",
                status="not_explored",
                score=None,
                evidence=[],
                justification="not explored",
            ),
        ],
    )

    weights = {
        "Problem Solving": 40,
        "Communication": 20,
        "Relevant Experience": 40,
    }

    # Only explored competencies count:
    #
    # (5×40 + 3×20) / 60
    # = 260 / 60
    # = 4.333...
    #
    # Rounded to 2 decimal places = 4.33

    result = scorecard.compute_overall(weights)

    assert result == 4.33


def test_no_explored_competencies_has_no_overall():
    # If nothing was assessed, TalentSift must not
    # invent an overall score.

    scorecard = Scorecard(
        session_id="empty-session",
        scores=[
            CompetencyScore(
                name="Problem Solving",
                status="not_explored",
                score=None,
                evidence=[],
                justification="not explored",
            ),
            CompetencyScore(
                name="Communication",
                status="not_explored",
                score=None,
                evidence=[],
                justification="not explored",
            ),
        ],
    )

    weights = {
        "Problem Solving": 50,
        "Communication": 50,
    }

    assert scorecard.compute_overall(weights) is None



def test_empty_transcript_returns_not_explored():
    """
    An empty interview contains no candidate evidence.

    Therefore:
    - every requested competency must be not_explored
    - every competency score must be None
    - no evidence should exist
    - overall must be None
    """

    rubric = {
        "Problem Solving": {
            "weight": 50,
            "indicators": [
                "Identifies a concrete problem",
                "Explains investigation or diagnosis",
                "Explains reasoning behind the chosen approach",
                "Describes the implemented solution",
                "Explains how the result was verified",
            ],
            "score_anchors": {
                1: "Provides no meaningful problem-solving evidence.",
                2: "Provides limited problem-solving evidence.",
                3: "Provides adequate problem-solving evidence.",
                4: "Provides strong systematic problem-solving evidence.",
                5: "Provides exceptional problem-solving evidence.",
            },
        },
        "Communication": {
            "weight": 50,
            "indicators": [
                "Explains technical ideas understandably",
                "Organizes explanations logically",
                "Connects technical details to their purpose",
            ],
            "score_anchors": {
                1: "Provides no meaningful communication evidence.",
                2: "Provides limited communication evidence.",
                3: "Provides adequate communication evidence.",
                4: "Provides strong communication evidence.",
                5: "Provides exceptional communication evidence.",
            },
        },
    }

    scorecard = score_transcript(
        session_id="empty-session",
        scoring_transcript="",
        rubric=rubric,
        evidence_transcript="",
    )

    assert len(scorecard.scores) == 2

    for competency in scorecard.scores:
        assert competency.status == "not_explored"
        assert competency.score is None
        assert competency.evidence == []
        assert competency.justification == "not explored"

    assert scorecard.overall is None
def test_empty_evidence_quote_is_rejected():
    scorecard = Scorecard(
        session_id="empty-evidence-session",
        scores=[
            CompetencyScore(
                name="Problem Solving",
                status="scored",
                score=3,
                evidence=[""],
                justification="Some justification.",
            )
        ],
    )

    with pytest.raises(ValueError):
        verify_evidence_quotes(
            scorecard=scorecard,
            transcript="I reproduced the problem and tested the fix.",
        )


def test_compute_overall_uses_competency_weights():
    scorecard = Scorecard(
        session_id="weighted-overall-test",
        scores=[
            CompetencyScore(
                name="Problem Solving",
                status="scored",
                score=3,
                evidence=["Problem-solving evidence"],
                justification="Test justification.",
            ),
            CompetencyScore(
                name="Culture & Values Fit",
                status="scored",
                score=4,
                evidence=["Culture evidence"],
                justification="Test justification.",
            ),
        ],
    )

    weights = {
        "Problem Solving": 25,
        "Culture & Values Fit": 10,
    }

    assert scorecard.compute_overall(weights) == 3.29



def test_python_overwrites_untrusted_overall():
    scorecard = Scorecard(
        session_id="overall-overwrite-test",
        scores=[
            CompetencyScore(
                name="Problem Solving",
                status="scored",
                score=3,
                evidence=["Problem-solving evidence"],
                justification="Test justification.",
            ),
            CompetencyScore(
                name="Culture & Values Fit",
                status="scored",
                score=4,
                evidence=["Culture evidence"],
                justification="Test justification.",
            ),
        ],

        # Pretend this incorrect value came from the LLM.
        overall=3.5,
    )

    weights = {
        "Problem Solving": 25,
        "Culture & Values Fit": 10,
    }

    # Before deterministic post-processing, the untrusted value exists.
    assert scorecard.overall == 3.5

    # This is the same assignment performed in scoring/engine.py.
    scorecard.overall = scorecard.compute_overall(weights)

    # Python replaces 3.5 with the correct weighted result.
    assert scorecard.overall == 3.29


    