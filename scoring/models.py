from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CompetencyScore(BaseModel):
    """
    Represents the evaluation result for ONE competency.
    """

    name: str
    # Example:
    # "Problem Solving"
    # "Communication"


    status: Literal["scored", "not_explored"]
    # Tells us whether this competency was actually assessed.
    #
    # "scored"
    #     The interview produced enough evidence to assign a 1-5 score.
    #
    # "not_explored"
    #     The interview never meaningfully assessed this competency.


    score: int | None = Field(
        default=None,
        ge=1,
        le=5,
    )
    # A real competency score is still between 1 and 5.
    #
    # But score may now also be None.
    #
    # None does NOT mean "bad".
    # None means "there is no score because this competency
    # was not explored".


    evidence: list[str]
    # Original candidate quotes supporting the assessment.
    #
    # A scored competency must contain evidence.
    #
    # A not_explored competency must contain no evidence.


    justification: str
    # Human-readable explanation of the result.


    @model_validator(mode="after")
    def validate_competency_result(self):
        """
        Prevent logically impossible competency results.
        """

        # --------------------------------------------------
        # CASE 1:
        # The competency was NOT explored.
        # --------------------------------------------------

        if self.status == "not_explored":

            if self.score is not None:
                raise ValueError(
                    "A not_explored competency must not have a score."
                )

            if self.evidence:
                raise ValueError(
                    "A not_explored competency must not contain evidence."
                )

            return self


        # --------------------------------------------------
        # CASE 2:
        # The competency WAS scored.
        # --------------------------------------------------

        if self.score is None:
            raise ValueError(
                "A scored competency must have a score."
            )

        if not self.evidence:
            raise ValueError(
                "A scored competency must include transcript evidence."
            )

        return self


class Scorecard(BaseModel):
    """
    Represents the complete evaluation for ONE interview session.
    """

    session_id: str

    scores: list[CompetencyScore]

    overall: float | None = None
    # None means there was not enough assessed competency
    # information to calculate an overall score.


    def compute_overall(
        self,
        weights: dict[str, int],
    ) -> float | None:
        """
        Calculate the weighted average using ONLY competencies
        that were actually explored.

        Not-explored competencies must not penalize the candidate.
        """

        weighted_total = 0.0
        total_weight = 0.0

        for competency_score in self.scores:

            # --------------------------------------------------
            # Make sure this competency exists in the rubric.
            # --------------------------------------------------

            if competency_score.name not in weights:
                raise ValueError(
                    f"No weight found for competency: "
                    f"{competency_score.name}"
                )


            # --------------------------------------------------
            # FAIRNESS RULE:
            #
            # If the interview never explored this competency,
            # do not treat it as a low score.
            # --------------------------------------------------

            if competency_score.status == "not_explored":
                continue


            # --------------------------------------------------
            # This should already be guaranteed by Pydantic,
            # but we check again before doing arithmetic.
            # --------------------------------------------------

            if competency_score.score is None:
                raise ValueError(
                    f"Scored competency has no score: "
                    f"{competency_score.name}"
                )


            weight = weights[competency_score.name]

            if weight < 0:
                raise ValueError(
                    f"Weight cannot be negative: "
                    f"{competency_score.name}"
                )


            weighted_total += (
                competency_score.score * weight
            )

            total_weight += weight


        # --------------------------------------------------
        # Nothing was actually assessed.
        #
        # Example: completely empty interview.
        #
        # We return None instead of inventing an overall score.
        # --------------------------------------------------

        if total_weight == 0:
            return None


        overall = weighted_total / total_weight


        if not 1.0 <= overall <= 5.0:
            raise ValueError(
                f"Computed overall score is outside "
                f"the valid range: {overall}"
            )


        return round(overall, 2)