from database import (
    list_completed_sessions_for_job,
    list_sessions_for_job,
    load_candidate,
     load_session,
)

from scoring.session_scoring import (
    ScorecardRepo,
)


def build_leaderboard_rows(
    job_id: str,
) -> list[dict]:
    """
    Build recruiter leaderboard data for one job.

    Each row combines:

    Session data from SQLite
    +
    Candidate data from SQLite
    +
    Scorecard data from JSON
    """

    # --------------------------------------------------
    # 1. LOAD COMPLETED INTERVIEWS FOR THIS JOB
    # --------------------------------------------------
    #
    # This comes from:
    #
    # data/talentsift.db
    #     ↓
    # sessions table
    #
    # Only completed Sessions are returned.
    sessions = list_completed_sessions_for_job(
        job_id
    )


    scorecard_repo = ScorecardRepo()


    rows = []


    for session in sessions:

        # --------------------------------------------------
        # 2. LOAD THE CANDIDATE
        # --------------------------------------------------
        #
        # session.candidate_id came from the Session row.
        #
        # load_candidate() uses that ID to read the
        # matching Candidate from SQLite.
        candidate = load_candidate(
            session.candidate_id
        )


        # --------------------------------------------------
        # 3. LOAD THE SCORECARD
        # --------------------------------------------------
        #
        # Scorecards currently live in:
        #
        # data/scorecards/<session-id>.json
        #
        # A completed interview without a Scorecard
        # cannot yet appear in the ranked leaderboard.
        try:

            scorecard = scorecard_repo.load(
                session.id
            )

        except FileNotFoundError:

            print(
                "⚠️ No Scorecard for completed Session:",
                session.id,
            )

            continue


        # --------------------------------------------------
        # 4. CHOOSE THE SCORE USED FOR RANKING
        # --------------------------------------------------
        #
        # Wednesday rule:
        #
        # recruiter override exists
        #       ↓
        # use override
        #
        # otherwise
        #       ↓
        # use AI overall

        if scorecard.recruiter_override is not None:

            ranking_score = (
                scorecard.recruiter_override
            )

            overridden = True

        else:

            ranking_score = scorecard.overall

            overridden = False


        # --------------------------------------------------
        # 5. BUILD ONE LEADERBOARD ROW
        # --------------------------------------------------

        row = {
            "session_id":
                session.id,

            "candidate_id":
                candidate.id,

            "candidate_name":
                candidate.full_name
                or candidate.id,

            "language":
                session.language,

            "completed_at":
                session.ended_at,

            # Original AI result.
            "overall":
                scorecard.overall,

            # Human override, if one exists.
            "recruiter_override":
                scorecard.recruiter_override,

            # Actual number used for ranking.
            "ranking_score":
                ranking_score,

            "overridden":
                overridden,
        }


        rows.append(
            row
        )

    # --------------------------------------------------
    # 6. SEPARATE RANKED AND UNRANKED CANDIDATES
    # --------------------------------------------------
    #
    # ranking_score can be None when:
    #
    # - AI overall is unavailable
    # - there is no recruiter override
    #
    # We do NOT invent a score for those candidates.

    ranked_rows = [
        row
        for row in rows
        if row["ranking_score"] is not None
    ]


    unranked_rows = [
        row
        for row in rows
        if row["ranking_score"] is None
    ]


    # --------------------------------------------------
    # 7. SORT RANKED CANDIDATES
    # --------------------------------------------------
    #
    # Primary ordering:
    # highest effective score first.
    #
    # candidate_id is ONLY used to make the display
    # order deterministic when scores are tied.
    #
    # IMPORTANT:
    # candidate_id does NOT change the candidate's rank.

    ranked_rows.sort(
        key=lambda row: (
            -row["ranking_score"],
            row["candidate_id"],
        )
    )


    # --------------------------------------------------
    # 8. ASSIGN COMPETITION RANKS
    # --------------------------------------------------
    #
    # Example:
    #
    # scores:
    # 4.8
    # 4.5
    # 4.5
    # 4.1
    #
    # ranks:
    # 1
    # 2
    # 2
    # 4
    #
    # Equal scores receive the SAME rank.


    # --------------------------------------------------
    # COUNT HOW MANY CANDIDATES SHARE EACH SCORE
    # --------------------------------------------------
    #
    # Example:
    #
    # 4.8 -> 1 candidate
    # 4.5 -> 3 candidates
    # 4.1 -> 1 candidate
    #
    # This lets the recruiter dashboard explicitly
    # show when a rank is tied.

    score_counts = {}


    for row in ranked_rows:

        score = row[
            "ranking_score"
        ]


        score_counts[score] = (
            score_counts.get(
                score,
                0,
            )
            + 1
        )


    previous_score = None
    previous_rank = None


    for position, row in enumerate(
        ranked_rows,
        start=1,
    ):

        current_score = row[
            "ranking_score"
        ]

        # How many candidates have exactly
        # this effective ranking score?
        tie_size = score_counts[
            current_score
        ]


        row["tie_size"] = tie_size


        row["is_tied"] = (
            tie_size > 1
        )

        if (
            previous_score is not None
            and current_score == previous_score
        ):

            # Same score as the previous candidate.
            #
            # Keep the same rank.
            row["rank"] = previous_rank

        else:

            # New lower score.
            #
            # Its rank is its actual position in the
            # sorted leaderboard.
            row["rank"] = position


            previous_rank = position


        previous_score = current_score


    # --------------------------------------------------
    # 9. UNRANKED CANDIDATES
    # --------------------------------------------------
    #
    # No effective score means:
    #
    # Rank: —
    #
    # NOT:
    #
    # Rank: last place

    for row in unranked_rows:

        row["rank"] = None
        row["is_tied"] = False
        row["tie_size"] = None

    # --------------------------------------------------
    # 10. RETURN RANKED FIRST, THEN UNRANKED
    # --------------------------------------------------

    return (
        ranked_rows
        + unranked_rows
    )


def select_top_n_with_ties(
    rows: list[dict],
    limit: int,
) -> list[dict]:
    """
    Select the top N ranked candidates while
    including everyone tied at the cutoff score.

    Unranked candidates are not included.
    """

    # --------------------------------------------------
    # 1. VALIDATE THE REQUEST
    # --------------------------------------------------

    if limit < 1:

        raise ValueError(
            "Leaderboard limit must be at least 1."
        )


    # --------------------------------------------------
    # 2. KEEP ONLY CANDIDATES WITH A REAL SCORE
    # --------------------------------------------------

    ranked_rows = [
        row
        for row in rows
        if row["ranking_score"] is not None
    ]


    # Fewer candidates than the requested limit?
    #
    # Example:
    #
    # recruiter asks for top 10
    # but only 6 candidates exist.
    if len(ranked_rows) <= limit:

        return ranked_rows


    # --------------------------------------------------
    # 3. FIND THE CUTOFF SCORE
    # --------------------------------------------------
    #
    # Python lists start counting from 0.
    #
    # So for "top 10":
    #
    # position 10
    #     ↓
    # index 9
    cutoff_score = ranked_rows[
        limit - 1
    ]["ranking_score"]


    # --------------------------------------------------
    # 4. INCLUDE EVERYONE AT OR ABOVE THE CUTOFF
    # --------------------------------------------------
    #
    # This prevents arbitrary exclusion of candidates
    # who have exactly the same effective score.

    selected_rows = [
        row
        for row in ranked_rows
        if row["ranking_score"] >= cutoff_score
    ]


    return selected_rows


def build_shortlist_summary(
    rows: list[dict],
    limit: int,
) -> dict:
    """
    Explain the result of a Top-N shortlist.

    Example:

    Top 10 requested,
    but 15 candidates were selected because
    several candidates tied at the cutoff.
    """

    selected_rows = select_top_n_with_ties(
        rows,
        limit,
    )


    ranked_rows = [
        row
        for row in rows
        if row["ranking_score"] is not None
    ]


    # Not enough ranked candidates to reach
    # the requested cutoff.
    if len(ranked_rows) < limit:

        return {
            "requested_limit": limit,
            "selected_count":
                len(selected_rows),
            "cutoff_score": None,
            "expanded_due_to_tie": False,
            "cutoff_tie_size": None,
        }


    cutoff_score = ranked_rows[
        limit - 1
    ]["ranking_score"]


    cutoff_rows = [
        row
        for row in ranked_rows
        if row["ranking_score"]
        == cutoff_score
    ]


    expanded_due_to_tie = (
        len(selected_rows) > limit
    )


    return {
        "requested_limit":
            limit,

        "selected_count":
            len(selected_rows),

        "cutoff_score":
            cutoff_score,

        "expanded_due_to_tie":
            expanded_due_to_tie,

        "cutoff_tie_size":
            len(cutoff_rows),
    }

def build_job_snapshot(
    job_id: str,
    shortlist_limit: int = 10,
) -> dict:
    """
    Build one current recruiter view for a job.

    This combines:

    - interview status counts
    - interview activity
    - current leaderboard
    - Top-N shortlist with cutoff ties
    """

    # --------------------------------------------------
    # 1. LOAD EVERY INTERVIEW FOR THIS JOB
    # --------------------------------------------------

    sessions = list_sessions_for_job(
        job_id
    )


    # --------------------------------------------------
    # 2. COUNT INTERVIEW STATUSES
    # --------------------------------------------------

    status_counts = {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
    }


    for session in sessions:

        if session.status in status_counts:

            status_counts[
                session.status
            ] += 1


    # --------------------------------------------------
    # 3. BUILD THE INTERVIEW ACTIVITY ROWS
    # --------------------------------------------------

    interview_rows = []

    scorecard_repo = ScorecardRepo()


    for session in sessions:

        candidate = load_candidate(
            session.candidate_id
        )


        # ----------------------------------------------
        # Does this completed interview already have
        # an evaluation Scorecard?
        # ----------------------------------------------

        has_scorecard = False


        if session.status == "completed":

            try:

                scorecard_repo.load(
                    session.id
                )

                has_scorecard = True

            except FileNotFoundError:

                has_scorecard = False


        # ----------------------------------------------
        # Human-readable evaluation state
        # ----------------------------------------------
        #
        # We deliberately say "awaiting evaluation"
        # instead of pretending we know whether a
        # missing Scorecard is currently processing
        # or failed.
        #
        # Later we can persist a dedicated scoring
        # status if needed.

        if session.status != "completed":

            evaluation_status = None

        elif has_scorecard:

            evaluation_status = "ready"

        else:

            evaluation_status = (
                "awaiting_evaluation"
            )


        interview_rows.append(
            {
                "session_id":
                    session.id,

                "candidate_id":
                    candidate.id,

                "candidate_name":
                    candidate.full_name
                    or candidate.id,

                "status":
                    session.status,

                "failure_reason":
                    session.failure_reason,

                "language":
                    session.language,

                "created_at":
                    session.created_at,

                "started_at":
                    session.started_at,

                "ended_at":
                    session.ended_at,

                "evaluation_status":
                    evaluation_status,
            }
        )


    # --------------------------------------------------
    # 4. BUILD THE CURRENT LEADERBOARD
    # --------------------------------------------------

    leaderboard_rows = (
        build_leaderboard_rows(
            job_id
        )
    )


    # --------------------------------------------------
    # 5. BUILD TOP-N WITH CUTOFF TIES
    # --------------------------------------------------

    shortlist_rows = (
        select_top_n_with_ties(
            leaderboard_rows,
            shortlist_limit,
        )
    )


    shortlist_summary = (
        build_shortlist_summary(
            leaderboard_rows,
            shortlist_limit,
        )
    )


    # --------------------------------------------------
    # 6. RETURN ONE SNAPSHOT
    # --------------------------------------------------

    return {
        "job_id":
            job_id,

        "status_counts":
            status_counts,

        "interviews":
            interview_rows,

        "leaderboard":
            leaderboard_rows,

        "shortlist":
            shortlist_rows,

        "shortlist_summary":
            shortlist_summary,
    }


def build_session_detail(
    session_id: str,
) -> dict:
    """
    Build the full recruiter audit view
    for one interview Session.
    """

    # --------------------------------------------------
    # 1. LOAD THE INTERVIEW SESSION
    # --------------------------------------------------

    session = load_session(
        session_id
    )


    # --------------------------------------------------
    # 2. LOAD THE CANDIDATE
    # --------------------------------------------------

    candidate = load_candidate(
        session.candidate_id
    )


    # --------------------------------------------------
    # 3. LOAD THE SCORECARD, IF ONE EXISTS
    # --------------------------------------------------
    #
    # A completed interview may temporarily have no
    # Scorecard if evaluation has not finished or
    # scoring failed.
    #
    # The recruiter should still be able to inspect
    # the interview itself.

    scorecard_repo = ScorecardRepo()


    try:

        scorecard = scorecard_repo.load(
            session.id
        )

    except FileNotFoundError:

        scorecard = None


    # --------------------------------------------------
    # 4. DETERMINE THE EFFECTIVE RANKING SCORE
    # --------------------------------------------------
    #
    # Preserve the original AI overall.
    #
    # If a recruiter override exists, that becomes
    # the effective score used by the leaderboard.

    if scorecard is None:

        ai_overall = None
        recruiter_override = None
        effective_score = None
        overridden = False

    else:

        ai_overall = scorecard.overall

        recruiter_override = (
            scorecard.recruiter_override
        )


        if recruiter_override is not None:

            effective_score = (
                recruiter_override
            )

            overridden = True

        else:

            effective_score = (
                ai_overall
            )

            overridden = False


    # --------------------------------------------------
    # 5. BUILD THE COMPETENCY BREAKDOWN
    # --------------------------------------------------

    competencies = []


    if scorecard is not None:

        for competency in scorecard.scores:

            competencies.append(
                {
                    "name":
                        competency.name,

                    "status":
                        competency.status,

                    "score":
                        competency.score,

                    "evidence":
                        competency.evidence,

                    "justification":
                        competency.justification,
                }
            )


    # --------------------------------------------------
    # 6. BUILD THE ORIGINAL TRANSCRIPT
    # --------------------------------------------------
    #
    # IMPORTANT:
    #
    # This is the original stored transcript used for
    # recruiter review and audit.
    #
    # We are NOT replacing it with the derived
    # scoring/redaction transcript.

    transcript = []


    for turn in session.transcript:

        transcript.append(
            {
                "speaker":
                    turn.speaker,

                "text":
                    turn.text,

                "timestamp":
                    turn.timestamp,

                "item_id":
                    turn.item_id,
            }
        )


    # --------------------------------------------------
    # 7. RETURN ONE DETAIL OBJECT
    # --------------------------------------------------

    return {
        "session_id":
            session.id,

        "job_id":
            session.job_id,

        "candidate": {
            "id":
                candidate.id,

            "full_name":
                candidate.full_name,

            "email":
                candidate.email,

            "preferred_language":
                candidate.preferred_language,
        },

        "interview": {
            "status":
                session.status,

            "failure_reason":
                session.failure_reason,


            "language":
                session.language,

            "created_at":
                session.created_at,

            "started_at":
                session.started_at,

            "ended_at":
                session.ended_at,
        },

        "assessment": {
            "available":
                scorecard is not None,

            "ai_overall":
                ai_overall,

            "recruiter_override":
                recruiter_override,

            "effective_score":
                effective_score,

            "overridden":
                overridden,

            "override_reason":
                (
                    scorecard.override_reason
                    if scorecard is not None
                    else None
                ),

            "overridden_by":
                (
                    scorecard.overridden_by
                    if scorecard is not None
                    else None
                ),

            "overridden_at":
                (
                    scorecard.overridden_at
                    if scorecard is not None
                    else None
                ),

            "override_history":
                (
                    scorecard.override_history
                    if scorecard is not None
                    else []
                ), 
        },

        "competencies":
            competencies,

        "transcript":
            transcript,
    }