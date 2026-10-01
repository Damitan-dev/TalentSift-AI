"""One language and literal recognition context for both interview transcribers.

Language hints describe expected speech. They must never request translation or
invent an answer when audio is unclear. This context is not sent to the grader.
"""

LANGUAGE_NAMES = {"en": "English", "fr": "French"}
_ALIASES = {"english": "en", "french": "fr", "français": "fr"}


def interview_language(value: str) -> str:
    code = value.strip().lower()
    code = _ALIASES.get(code, code)
    if code not in LANGUAGE_NAMES:
        raise ValueError("Interview language must be English or French.")
    return code


def build_transcription_settings(language: str, model: str, *,
                                 candidate_name=None, job_title=None, delay=None):
    code = interview_language(language)
    prompts = {
        "en": (
            "A job interview conducted in English. Transcribe only the speech "
            "that is audible, faithfully in its original language. Preserve "
            "names, technical terms, numbers and negations. Do not translate, "
            "answer questions or invent words from silence or background noise."
        ),
        "fr": (
            "Un entretien d'embauche mené en français. Transcrivez fidèlement "
            "uniquement la parole audible, dans sa langue d'origine. Conservez "
            "les noms, termes techniques, nombres et négations. Ne traduisez "
            "pas, ne répondez pas aux questions et n'inventez pas de mots à "
            "partir du silence ou du bruit de fond."
        ),
    }
    # These are possible literal terms, never a script for the expected answer.
    keywords = []
    for value in (candidate_name, job_title):
        if not isinstance(value, str):
            continue
        term = " ".join(value.replace("<", "").replace(">", "").split())[:128]
        if term and term not in keywords:
            keywords.append(term)
    settings = {"model": model, "languages": [code], "prompt": prompts[code]}
    if keywords:
        settings["keywords"] = keywords
    if delay is not None:
        settings["delay"] = delay
    return settings
