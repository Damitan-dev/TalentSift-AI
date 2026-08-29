import re


NAME_INTRODUCTION = re.compile(
    r"\b(?P<prefix>(?i:my name is|i am|i'm|i’m|this is))\s+"
    r"(?P<name>[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
    r"(?:\s+[A-Z][a-z]+){0,2})"
)


def redact_candidate_identity(text: str) -> str:
    """
    Remove a candidate's name from evaluator-facing text only.

    The original transcript is NOT modified.
    """

    return NAME_INTRODUCTION.sub(
        lambda match: f"{match.group('prefix')} [CANDIDATE_NAME]",
        text,
    )


