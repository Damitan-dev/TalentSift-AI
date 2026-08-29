
import statistics

from dataclasses import dataclass

from scoring.engine import (
    score_transcript,
    EvaluationFailedError,
)

from scoring.session_scoring import build_transcripts

@dataclass
class TestTurn:
    speaker: str
    text: str


@dataclass
class TestSession:
    transcript: list[TestTurn]


def make_test_session(transcript: str) -> TestSession:
    """
    Convert the fixed test transcript into the same basic
    session structure expected by the production transcript builder.
    """

    turns = []

    blocks = transcript.strip().split("\n\n")

    for block in blocks:
        block = block.strip()

        if not block:
            continue

        if block.lower().startswith("interviewer:"):
            text = block[len("Interviewer:"):].strip()

            turns.append(
                TestTurn(
                    speaker="interviewer",
                    text=text,
                )
            )

        elif block.lower().startswith("candidate:"):
            text = block[len("Candidate:"):].strip()

            turns.append(
                TestTurn(
                    speaker="candidate",
                    text=text,
                )
            )

    return TestSession(transcript=turns)

RUN_COUNT = 5

# Keep the session ID identical across all runs.
# Consistency testing should change as little as possible.
SESSION_ID = "consistency-test-session"


# FIXED_TRANSCRIPT = """
# Interviewer: Tell me about a difficult backend problem you solved.

# Candidate: I discovered that storing household roles directly
# on the user model made it difficult to distinguish an owner
# from an occupant across households. I redesigned the architecture
# using household membership and tested owner-only routes to ensure
# occupants could not access them.

# Interviewer: How did you explain the architectural change to others?

# Candidate: I explained why the old structure caused problems and showed
# the new membership relationship before we continued with the implementation.
# """

FIXED_TRANSCRIPT = """
Interviewer: Tell me about a backend project you have worked on.

Candidate: I built a Python Flask backend for a household management application.
I was responsible for designing the API endpoints, connecting the application
to a database, implementing authentication, and testing the routes. One
challenge was handling different roles within a household. I initially stored
the role directly on the user model, but that made it difficult to distinguish
an owner from an occupant across different households. I redesigned it using
household membership and tested owner-only routes to make sure occupants could
not access them.

Interviewer: Tell me about a difficult technical problem you faced and how
you solved it.

Candidate: I first reproduced the problem and broke it into smaller parts.
I traced the request from the API endpoint through the database query and
identified where the incorrect authorization decision was being made. I
considered whether changing the route logic or changing the data model would
solve the problem. I chose the membership-based design because it represented
the relationship more accurately and also handled users belonging to multiple
households. I then tested both authorized and unauthorized cases, including
an occupant attempting to access an owner-only route.

Interviewer: How do you explain technical ideas to someone who is not deeply
familiar with backend development?

Candidate: I avoid starting with implementation details. I first explain the
problem in simple terms, then describe the relationship between the components,
and only introduce technical terms when they are useful. For example, when
explaining the household membership redesign, I compared it to having a
separate membership record for each household instead of putting every role
directly on the person's profile. I then showed a small example before
discussing the actual implementation.

Interviewer: Why are you interested in a Junior Python Backend Developer role?

Candidate: I enjoy building backend systems with Python because I like working
with APIs, databases, authentication, and the logic that makes applications
work reliably. I want to become stronger in backend engineering and learn how
professional teams design, test, and maintain production systems. This role
fits my goal because it would allow me to apply what I have already built while
developing stronger engineering practices.

Interviewer: Tell me about a time you worked with others, received feedback,
or took ownership of a mistake.

Candidate: During the household project, I initially designed the roles in a
way that worked for a single household but did not scale well when users could
belong to multiple households. After discussing the problem with another
developer, I accepted the feedback and took ownership of redesigning that
part of the system. I explained the change to the team, updated the affected
routes, and added tests so the same problem would not return.
"""






#RUBRIC B


RUBRIC = {
    "Relevant Experience": {
        "weight": 30,
        "strong_answer_looks_like": (
            "Describes previous projects, internships, or practical work "
            "related to backend development. Explains responsibilities, "
            "technologies used, challenges faced, and measurable outcomes."
        )
    },

    "Problem Solving": {
        "weight": 25,
        "strong_answer_looks_like": (
            "Breaks problems into logical steps, explains reasoning before "
            "coding, considers edge cases, and chooses appropriate solutions."
        )
    },

    "Communication": {
        "weight": 20,
        "strong_answer_looks_like": (
            "Gives clear, structured answers, explains technical concepts "
            "understandably, and communicates ideas effectively."
        )
    },

    "Role Motivation": {
        "weight": 15,
        "strong_answer_looks_like": (
            "Explains why they want the backend developer role, shows "
            "interest in Python/backend engineering, and connects personal "
            "goals with the position."
        )
    },

    "Culture & Values Fit": {
        "weight": 10,
        "strong_answer_looks_like": (
            "Provides examples of teamwork, handling feedback, learning "
            "from mistakes, ownership, and collaboration."
        )
    }
}

runs = []




# Run the EXACT SAME evaluation pipeline five times.
for run_number in range(1, RUN_COUNT + 1):

    print(
        f"\n--- RUN "
        f"{run_number}/{RUN_COUNT} ---"
    )

    try:

        # --------------------------------------------------
        # STEP 1
        # Create a test session from the fixed transcript.
        # --------------------------------------------------

        session = make_test_session(
            FIXED_TRANSCRIPT
        )

        # --------------------------------------------------
        # STEP 2
        # Use the SAME transcript builder used by production.
        #
        # This gives us:
        #
        # full_transcript
        # scoring_transcript
        # evidence_transcript
        # --------------------------------------------------

        (
            full_transcript,
            scoring_transcript,
            evidence_transcript,
        ) = build_transcripts(session)

        # --------------------------------------------------
        # STEP 3
        # Send the production-generated transcripts
        # to the evaluator.
        # --------------------------------------------------

        scorecard = score_transcript(
            session_id=SESSION_ID,
            scoring_transcript=scoring_transcript,
            rubric=RUBRIC,
            evidence_transcript=evidence_transcript,
        )

        # --------------------------------------------------
        # STEP 4
        # Save successful run.
        # --------------------------------------------------

        runs.append(scorecard)

        # --------------------------------------------------
        # STEP 5
        # Display individual scores.
        # --------------------------------------------------

        for competency in scorecard.scores:

            print(
                f"{competency.name}: "
                f"{competency.score}/5"
            )

        print(
            f"Overall: "
            f"{scorecard.overall}"
        )

    except EvaluationFailedError as error:

        print(
            f"❌ Run {run_number} failed."
        )

        print(error)

# If every evaluator call failed, min/max would have
# nothing to work with.
if not runs:
    raise RuntimeError(
        "No valid evaluation runs were produced."
    )


print("\nCONSISTENCY RESULTS")
print("-" * 80)

print(
    f"{'Competency':<30}"
    f"{'Scores':<20}"
    f"{'Mean':<10}"
    f"{'Min':<10}"
    f"{'Max':<10}"
    f"{'Range':<10}"
    f"{'SD':<10}"

)


for competency_name in RUBRIC.keys():

    scores = []

    # Get this particular competency's score
    # from every successful evaluation run.
    for scorecard in runs:

        competency = next(
            score
            for score in scorecard.scores
            if score.name == competency_name
        )

        scores.append(competency.score)


    minimum = min(scores)

    maximum = max(scores)


    score_range = maximum - minimum
    mean = statistics.mean(scores)

     # Population standard deviation because these 5 runs
    # are the complete set of trials in this experiment.
    standard_deviation = statistics.pstdev(scores)

    print(
        f"{competency_name:<30}"
        f"{str(scores):<25}"
        f"{mean:<10.2f}"
        f"{minimum:<10}"
        f"{maximum:<10}"
        f"{score_range:<10}"
        f"{standard_deviation:<10.2f}"
    )