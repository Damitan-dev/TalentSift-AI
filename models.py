# models.py

from datetime import datetime, timezone
# datetime stores dates/times.
# timezone lets us save timestamps in UTC.

from typing import Literal
# Literal restricts a field to specific allowed values.

from uuid import uuid4
# uuid4 gives each object a unique ID.

from pydantic import BaseModel, Field
# BaseModel gives us validation.
# Field lets us create automatic default values.


def utc_now() -> datetime:
    # Return the current time in UTC.
    return datetime.now(timezone.utc)


class Competency(BaseModel):
    # Name of the competency, e.g. "Problem Solving".
    name: str

    # Explain what the competency measures.
    description: str

    # Importance of this competency for the job.
    weight: int

    # Describe the evidence expected from a strong answer.
    strong_answer_looks_like: str


class Job(BaseModel):
    # Automatically generate a unique job ID.
    id: str = Field(default_factory=lambda: str(uuid4()))

    # Job title.
    title: str

    # Job description.
    description: str

    # Competencies used to evaluate this job.
    competencies: list[Competency]

    # Languages supported for this interview.
    languages: list[str]

    # When this job was created.
    created_at: datetime = Field(default_factory=utc_now)

    # When this job was last changed.
    updated_at: datetime | None = None


class Candidate(BaseModel):

    id: str = Field(
        default_factory=lambda: str(uuid4())
    )

    # Used operationally by recruiters and invitations.
    #
    # It should NOT become part of AI scoring.
    full_name: str | None = None

    # Used to send the candidate their interview link.
    #
    # It should also remain outside the scoring input.
    email: str | None = None

    preferred_language: str

    created_at: datetime = Field(
        default_factory=utc_now
    )


class TranscriptTurn(BaseModel):
    # Only these two speaker names are allowed.
    speaker: Literal["candidate", "interviewer"]

    # The words spoken during this turn.
    text: str

    # When this transcript turn was captured.
    timestamp: datetime = Field(default_factory=utc_now)

    # OpenAI's conversation item ID.
    item_id: str | None = None





class Session(BaseModel):

    # Unique ID for this interview session.
    id: str = Field(
        default_factory=lambda: str(uuid4())
    )

    # Which job this interview belongs to.
    job_id: str

    # Which candidate is taking the interview.
    candidate_id: str

    # Current interview lifecycle state.
    status: Literal[
        "pending",
        "in_progress",
        "completed",
        "failed"
    ] = "pending"

    # Language selected for this interview.
    #
    # Tuesday currently supports:
    # en = Eng# Language selected for this interview.
    #
    # For Tuesday's browser flow we will normally
    # store "en" or "fr".
    #
    # We keep this as str for now so older sessions
    # that use values such as "English" still load.lish
        # fr = French

    language: str

    # The interview session must not exist unless
    # consent was explicitly provided.
    consent_given: bool

    # When the Session record itself was created.
    created_at: datetime = Field(
        default_factory=utc_now
    )

    # When the candidate gave consent.
    consented_at: datetime = Field(
        default_factory=utc_now
    )

    # None while candidate is still doing onboarding.
    #
    # We fill this when the actual interview begins.
    started_at: datetime | None = None

    # None until the interview has finished.
    ended_at: datetime | None = None

    # Final transcript turns.
    #
    # Do NOT save every live transcription fragment here.
    transcript: list[TranscriptTurn] = Field(
        default_factory=list
    )