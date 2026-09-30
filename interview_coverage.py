"""Server-owned question opportunities, separate from evidence quality/scoring."""

from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class Topic:
    key: str
    competency: str
    en: str
    fr: str

    def question(self, language):
        return self.fr if language == "fr" else self.en


# Six prompts cover the existing five rubric areas. Culture & Values Fit has
# two explicit opportunities: teamwork, then feedback/ownership/learning.
# Edit the spoken questions here; do not maintain a second copy in the prompt.
TOPICS = (
    Topic("experience", "Relevant Experience",
          "Tell me about a Python or backend project you worked on, focusing on your own contribution.",
          "Parlez-moi d'un projet Python ou backend sur lequel vous avez travaillé, en précisant votre contribution personnelle."),
    Topic("problem_solving", "Problem Solving",
          "Walk me through how you investigated and resolved a technical problem in a project.",
          "Expliquez-moi comment vous avez analysé et résolu un problème technique dans un projet."),
    Topic("collaboration", "Culture & Values Fit",
          "Tell me about a time you worked with others on a project and how you contributed to the team.",
          "Parlez-moi d'une expérience de projet en équipe et de votre contribution au travail collectif."),
    Topic("communication", "Communication",
          "How would you explain what an API does to a teammate who is new to backend development?",
          "Comment expliqueriez-vous le rôle d'une API à un collègue qui débute en développement backend ?"),
    Topic("feedback_ownership", "Culture & Values Fit",
          "Tell me about a time feedback or a mistake led you to change your work, including how you followed through.",
          "Parlez-moi d'une fois où un retour ou une erreur vous a amené à modifier votre travail, en précisant comment vous avez mené ce changement à son terme."),
    Topic("motivation", "Role Motivation",
          "What interests you about this Python backend role and how does it connect with your goals?",
          "Qu'est-ce qui vous intéresse dans ce poste en développement backend Python et quel lien faites-vous avec vos objectifs ?"),
)


def tool(name, description):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False}}


NEXT_TOPIC_TOOL = tool(
    "next_interview_topic",
    "After readiness is confirmed, request the first core question. Thereafter, "
    "call only after the current question has been asked and the candidate has "
    "answered or declined to answer, with at most one useful follow-up. A request "
    "to repeat, clarify, or wait is not an answer. The server chooses the next topic.")
FINISH_TOOL = tool(
    "finish_interview",
    "Request the fixed closing after every required core question has been asked "
    "and the candidate has had an opportunity to answer, or when the server "
    "explicitly requests completion at the time limit. The server checks coverage.")


def normalized(text):
    # Preserve words/accents; allow punctuation, apostrophe and whitespace
    # differences in the generated transcript of the prescribed question.
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


class InterviewCoverage:
    def __init__(self, language="en"):
        self.language = language
        self.index = -1
        self.completed = []
        self.asked = False
        self.answered = False
        self.responses = {}
        self.candidate_topics = {}
        self.committed = set()

    @property
    def current(self):
        return TOPICS[self.index] if 0 <= self.index < len(TOPICS) else None

    @property
    def remaining(self):
        return [topic.key for topic in TOPICS if topic.key not in self.completed]

    @property
    def can_finish(self):
        return not self.remaining or (
            self.index == len(TOPICS) - 1 and self.asked and self.answered)

    def advance(self):
        """Never jump past an unasked question or a missing candidate turn."""
        if self.current and not (self.asked and self.answered):
            return False
        if self.current:
            self.completed.append(self.current.key)
        if self.index < len(TOPICS):
            self.index += 1
        self.asked = self.answered = False
        return True

    def response_created(self, response_id):
        # Bind before a tool call can change the active topic. Old response.done
        # events cannot validate a question for a newly selected topic.
        if response_id and self.current:
            self.responses[response_id] = self.index

    def response_done(self, response, allowed):
        index = self.responses.pop(response.get("id"), None)
        if (not allowed or index != self.index or not self.current or self.asked
                or response.get("status", "completed") != "completed"):
            return
        expected = " " + normalized(self.current.question(self.language)) + " "
        for item in response.get("output", []):
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            text = " ".join(part.get("transcript") or ""
                            for part in item.get("content", []))
            if expected in " " + normalized(text) + " ":
                self.asked = True
                break

    def speech_started(self, item_id):
        if item_id:
            self.candidate_topics[item_id] = self.index if self.asked else None

    def audio_committed(self, item_id):
        if not item_id or item_id in self.committed:
            return
        self.committed.add(item_id)
        if (self.current and self.asked
                and self.candidate_topics.get(item_id) == self.index):
            self.answered = True

    def progress(self):
        return {"completed": list(self.completed), "remaining": self.remaining,
                "current": self.current.key if self.current else None,
                "question_generated": self.asked,
                "candidate_turn_received": self.answered,
                "next_question": self.current.question(self.language) if self.current else None}

    def response_options(self, base_instructions, seconds_remaining):
        """Refresh guidance at response creation, never by waiting for ASR."""
        notes = ["SERVER QUESTION PLAN (authoritative for topic order and completion)",
                 "Completed opportunities: " + (", ".join(self.completed) or "none"),
                 "Still required: " + (", ".join(self.remaining) or "none"),
                 f"Approximately {max(0, int(seconds_remaining))} seconds remain.",
                 "Do not read this checklist or tool results aloud."]
        options = {}
        if self.index == -1:
            notes.append("If the candidate has confirmed readiness, call next_interview_topic. "
                         "Otherwise address their readiness concern and wait. Do not invent a warm-up question.")
        elif not self.current:
            notes.append("All core opportunities are complete. Call finish_interview without extra questions.")
        elif not self.asked:
            notes.append("The next core question has not yet been verified in a completed spoken response. "
                         "Ask this question exactly as written, then listen. Do not replace it with another "
                         "topic or add a second question. Respect an explicit request to wait or stop.\n"
                         + self.current.question(self.language))
            options.update(tools=[], tool_choice="none")
        else:
            notes.append("Current topic: " + self.current.key + ". Listen to the latest candidate audio. "
                         "A request to wait, repeat or clarify needs a response on this SAME topic; "
                         "it is not permission to advance. A weak answer or an explicit inability to "
                         "answer does not justify endless probing. After the answer and at most one "
                         "useful follow-up, call next_interview_topic. Do not choose a different core "
                         "question yourself. Never treat a question opportunity as a strong answer.")
            if not self.answered:
                options.update(tools=[], tool_choice="none")
            if seconds_remaining < 70 * len(self.remaining):
                notes.append("Time is limited relative to the remaining topics: skip optional follow-ups "
                             "and move to the next core question after an answer. Do not interrupt the candidate.")
        options["instructions"] = base_instructions + "\n\n" + "\n".join(notes)
        return options
