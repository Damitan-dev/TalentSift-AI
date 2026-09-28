import asyncio
import json

import pytest

from interview_turns import FinalTranscripts, InterviewTurns, build_turn_detection


class Engine:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def setup():
    engine = Engine()
    turns = InterviewTurns(engine, lambda: True, log=lambda _: None)
    return engine, turns


async def say(turns, item):
    await turns.speech_started(item)
    await turns.speech_stopped(item)
    await turns.audio_committed(item)


def test_native_audio_answers_without_waiting_for_transcription():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'answer')
        assert [e['type'] for e in engine.sent] == ['response.create']
        await turns.audio_committed('answer')
        assert len(engine.sent) == 1
    asyncio.run(scenario())


def test_resumed_speech_blocks_late_old_commit_until_newest_turn_commits():
    async def scenario():
        engine, turns = setup()
        await turns.speech_started('old')
        await turns.speech_stopped('old')
        await turns.speech_started('continued')
        await turns.audio_committed('old')
        assert engine.sent == []
        await turns.speech_stopped('continued')
        assert engine.sent == []
        await turns.audio_committed('continued')
        assert len(engine.sent) == 1
        await turns.response_created({'id': 'reply'})
        await turns.response_done({'id': 'reply'})
        await turns.audio_committed('old')
        assert len(engine.sent) == 1
    asyncio.run(scenario())


def test_new_speech_cancels_late_response_and_suppresses_its_audio():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'first')
        await turns.speech_started('continued')
        assert not await turns.response_created({'id': 'stale'})
        assert engine.sent[-1] == {'type': 'response.cancel', 'response_id': 'stale'}
        assert not turns.allow_audio({'response_id': 'stale'})
        await turns.speech_stopped('continued')
        await turns.audio_committed('continued')
        assert len(engine.sent) == 2
        await turns.response_done({'id': 'stale'})
        assert engine.sent[-1]['type'] == 'response.create'
        assert await turns.response_created({'id': 'fresh'})
        assert turns.allow_audio({'response_id': 'fresh'})
        assert not turns.allow_audio({'response_id': 'stale'})
    asyncio.run(scenario())


def test_active_reply_interrupted_then_latest_answer_waits_for_done():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'first')
        await turns.response_created({'id': 'first-reply'})
        await say(turns, 'barge-in')
        assert not turns.allow_audio({'response_id': 'first-reply'})
        assert not turns.response_allowed({'response_id': 'first-reply', 'type': 'response.output_item.done'})
        assert len(engine.sent) == 1
        await turns.response_done({'id': 'first-reply', 'status': 'cancelled'})
        assert len(engine.sent) == 2
    asyncio.run(scenario())


def test_closing_is_queued_until_tool_response_finishes():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'answer')
        await turns.response_created({'id': 'tool-response'})
        await turns.control_response('closing', {'instructions': 'Goodbye'})
        assert len(engine.sent) == 1
        await turns.response_done({'id': 'tool-response'})
        assert engine.sent[-1]['response']['instructions'] == 'Goodbye'
        assert engine.sent[-1]['response']['metadata']['talentsift_kind'] == 'closing'
    asyncio.run(scenario())


def test_deadline_cancels_current_generation_before_requesting_finish():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'answer')
        await turns.response_created({'id': 'question'})
        await turns.control_response('deadline', {'instructions': 'Finish'}, interrupt=True)
        assert engine.sent[-1]['type'] == 'response.cancel'
        assert not turns.allow_audio({'response_id': 'question'})
        await turns.response_done({'id': 'question'})
        assert engine.sent[-1]['response']['metadata']['talentsift_kind'] == 'deadline'
    asyncio.run(scenario())


def test_tool_continuation_waits_for_resumed_speech_and_uses_fresh_guidance_once():
    async def scenario():
        engine, turns = setup()
        guidance = ["first"]
        turns.candidate_response = lambda: {"instructions": guidance[0]}
        await say(turns, 'first-answer')
        await turns.response_created({'id': 'tool'})
        await turns.control_response('candidate')
        await turns.speech_started('continued-answer')
        await turns.response_done({'id': 'tool'})
        assert len(engine.sent) == 1
        await turns.speech_stopped('continued-answer')
        assert len(engine.sent) == 1, 'Wait for resumed audio to be committed'
        guidance[0] = 'fresh'
        await turns.audio_committed('continued-answer')
        assert len(engine.sent) == 2
        assert engine.sent[-1]['response']['instructions'] == 'fresh'
        assert engine.sent[-1]['response']['metadata']['talentsift_kind'] == 'candidate'
        await turns.response_created({'id': 'question'})
        await turns.response_done({'id': 'question'})
        assert len(engine.sent) == 2, 'The resumed turn was consumed by the continuation'
    asyncio.run(scenario())


def test_deadline_overrides_queued_topic_continuation():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'answer')
        await turns.response_created({'id': 'tool'})
        await turns.control_response('candidate')
        await turns.control_response('deadline', {'instructions': 'Finish'}, interrupt=True)
        await turns.response_done({'id': 'tool'})
        assert engine.sent[-1]['response']['metadata']['talentsift_kind'] == 'deadline'
        turns.close()
        await turns.response_done({'id': 'deadline'})
        assert len(engine.sent) == 3
    asyncio.run(scenario())


def test_deadline_while_request_pending_cancels_it_when_created():
    async def scenario():
        engine, turns = setup()
        await say(turns, 'answer')
        await turns.control_response('deadline', interrupt=True)
        assert len(engine.sent) == 1
        assert not await turns.response_created({'id': 'pending'})
        await turns.response_done({'id': 'pending'})
        assert engine.sent[-1]['response']['metadata']['talentsift_kind'] == 'deadline'
    asyncio.run(scenario())


def test_inactive_or_closed_interview_does_not_reply():
    async def scenario():
        engine, turns = setup()
        turns.allow_reply = lambda: False
        await say(turns, 'answer')
        assert engine.sent == []
        turns.close()
        await turns.control_response('closing')
        assert engine.sent == []
        assert not turns.allow_audio({'response_id': 'late'})
    asyncio.run(scenario())


def test_timings_measure_native_commit_and_generation_separately():
    async def scenario():
        engine = Engine()
        now = [1.0]
        logs = []
        turns = InterviewTurns(engine, lambda: True, log=logs.append, clock=lambda: now[0])
        await turns.speech_started('answer')
        await turns.speech_stopped('answer')
        now[0] = 1.05
        await turns.audio_committed('answer')
        await turns.response_created({'id': 'reply'})
        now[0] = 1.35
        turns.allow_audio({'response_id': 'reply'})
        turns.allow_audio({'response_id': 'reply'})
        assert len(logs) == 2
        assert 'vad_stop_to_request_ms=50' in logs[0]
        assert 'request_to_first_audio_ms=300' in logs[1]
    asyncio.run(scenario())


def test_vad_configuration_preserves_manual_response_creation(monkeypatch):
    monkeypatch.delenv('TALENTSIFT_VAD_MODE', raising=False)
    monkeypatch.delenv('TALENTSIFT_VAD_EAGERNESS', raising=False)
    assert build_turn_detection() == {
        'type': 'semantic_vad', 'eagerness': 'medium',
        'create_response': False, 'interrupt_response': True,
    }
    monkeypatch.setenv('TALENTSIFT_VAD_EAGERNESS', 'low')
    assert build_turn_detection()['eagerness'] == 'low'
    monkeypatch.setenv('TALENTSIFT_VAD_MODE', 'server_vad')
    assert build_turn_detection()['silence_duration_ms'] == 1500


def test_late_candidate_text_is_saved_before_the_following_question():
    async def scenario():
        saved = []
        transcripts = FinalTranscripts(saved.append)
        transcripts.expect('candidate')
        transcripts.expect('interviewer')
        transcripts.complete('interviewer', 'Next question')
        assert saved == []
        waiting = asyncio.create_task(transcripts.wait_for_scoring())
        await asyncio.sleep(0)
        assert not waiting.done()
        transcripts.complete('candidate', 'My answer')
        await waiting
        assert saved == ['My answer', 'Next question']
        transcripts.complete('candidate', 'Duplicate')
        assert len(saved) == 2
    asyncio.run(scenario())


def test_missing_or_failed_transcript_never_produces_partial_score():
    async def scenario():
        transcripts = FinalTranscripts(lambda _: None)
        transcripts.expect('missing')
        with pytest.raises(TimeoutError):
            await transcripts.wait_for_scoring(timeout=0.001)
        transcripts.complete('missing', failed=True)
        with pytest.raises(RuntimeError, match='scoring was skipped'):
            await transcripts.wait_for_scoring()
    asyncio.run(scenario())
