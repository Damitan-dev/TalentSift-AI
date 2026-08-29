import re


def normalize_for_scoring(text: str) -> str:
    text = re.sub(
        r"\b(?:um+|uh+|erm+|ah+)\b[,\s]*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;!?])", r"\1", text)
    return text.strip()