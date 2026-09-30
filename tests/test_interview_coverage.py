import pytest

from interview_coverage import InterviewCoverage, TOPICS


def spoken(coverage, rid="question", text=None, allowed=True, status="completed"):
    coverage.response_created(rid)
    coverage.response_done({"id": rid, "status": status, "output": [{
        "type": "message", "role": "assistant", "content": [{"type": "audio",
        "transcript": text or coverage.current.question(coverage.language)}]}]}, allowed)


def answer(coverage, item="answer"):
    coverage.speech_started(item)
    coverage.audio_committed(item)


@pytest.mark.parametrize("language", ["en", "fr"])
def test_every_core_question_and_subsequent_audio_turn_are_required(language):
    coverage = InterviewCoverage(language)
    assert not coverage.can_finish
    coverage.advance()
    for index, topic in enumerate(TOPICS):
        assert coverage.current == topic
        assert not coverage.can_finish
        # A tool call, a related word or a candidate turn alone isn't coverage.
        answer(coverage, f"before-{index}")
        assert not coverage.advance()
        spoken(coverage, text="I heard you mention teamwork and Python.")
        assert not coverage.advance()
        spoken(coverage, text="D'accord. " + topic.question(language))
        assert coverage.asked and not coverage.answered
        assert not coverage.advance()
        answer(coverage, f"answer-{index}")
        assert coverage.can_finish == (index == len(TOPICS) - 1)
        coverage.advance()
    assert coverage.can_finish and coverage.remaining == []
    assert len(coverage.completed) == 6
    assert {topic.competency for topic in TOPICS} == {
        "Relevant Experience", "Problem Solving", "Communication",
        "Role Motivation", "Culture & Values Fit"}


@pytest.mark.parametrize("allowed,status", [(False, "completed"), (True, "cancelled"), (True, "failed")])
def test_suppressed_or_failed_generation_cannot_complete_topic(allowed, status):
    coverage = InterviewCoverage()
    coverage.advance()
    spoken(coverage, allowed=allowed, status=status)
    answer(coverage)
    assert not coverage.asked and not coverage.advance()


def test_late_and_duplicate_commits_do_not_answer_another_topic():
    coverage = InterviewCoverage()
    coverage.advance()
    coverage.speech_started("before-question")
    spoken(coverage)
    coverage.audio_committed("before-question")
    coverage.audio_committed("unknown")
    assert not coverage.answered
    answer(coverage, "real-answer")
    coverage.speech_started("old-continuation")
    coverage.advance()
    spoken(coverage, "second-question")
    coverage.audio_committed("real-answer")
    coverage.audio_committed("old-continuation")
    assert not coverage.answered
    assert not coverage.advance()


def test_old_response_cannot_validate_new_topic_and_punctuation_is_tolerated():
    coverage = InterviewCoverage("fr")
    coverage.advance()
    spoken(coverage)
    answer(coverage)
    coverage.response_created("old-tool-response")
    coverage.advance()
    output = {"id": "old-tool-response", "output": [{"type": "message", "role": "assistant",
              "content": [{"transcript": coverage.current.question("fr")}]}]}
    coverage.response_done(output, True)
    assert not coverage.asked
    text = coverage.current.question("fr").replace("'", "’").replace(" ", "  ").upper()
    spoken(coverage, "current", text=text)
    assert coverage.asked


def test_per_turn_guidance_retains_base_rules_and_prioritizes_unasked_topics():
    coverage = InterviewCoverage()
    first = coverage.response_options("BASE RULES", 600)
    assert first["instructions"].startswith("BASE RULES")
    assert "confirmed readiness" in first["instructions"]
    coverage.advance()
    pending = coverage.response_options("BASE RULES", 500)
    assert pending["tools"] == []
    assert coverage.current.question("en") in pending["instructions"]
    spoken(coverage)
    assert coverage.response_options("BASE", 500)["tools"] == []
    answer(coverage)
    short = coverage.response_options("BASE", 100)
    assert "tools" not in short
    assert "skip optional follow-ups" in short["instructions"]
    assert "request to wait, repeat or clarify" in short["instructions"]
