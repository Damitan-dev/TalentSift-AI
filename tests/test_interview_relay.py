"""Exercise the real FastAPI relay with controlled OpenAI events."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from types import SimpleNamespace
from fastapi import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
import pytest

from interview_coverage import TOPICS


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    async def send(self, raw):
        await self.sent.put(json.loads(raw))

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason
        await self.incoming.put(None)

    async def emit(self, event):
        await self.incoming.put(json.dumps(event))

    def __aiter__(self):
        async def iterate():
            while (raw := await self.incoming.get()) is not None:
                if isinstance(raw, Exception):
                    raise raw
                yield raw
        return iterate()


class Browser(Socket):
    async def accept(self):
        pass

    async def send_json(self, event):
        await self.sent.put(event)

    async def receive_text(self):
        raw = await self.incoming.get()
        if raw is None:
            raise WebSocketDisconnect()
        return raw


async def message(socket, kind):
    async with asyncio.timeout(2):
        while True:
            event = await socket.sent.get()
            if event['type'] == kind:
                return event


@pytest.mark.parametrize("advance_tool", ["next_interview_topic", "finish_interview"])
def test_relay_covers_all_topics_without_waiting_for_asr_but_waits_to_score(tmp_path, monkeypatch, advance_tool):
    monkeypatch.setenv('TALENTSIFT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-value')
    monkeypatch.setenv('TALENTSIFT_LIVE_CAPTIONS', '0')
    monkeypatch.setenv('TALENTSIFT_VAD_MODE', 'semantic_vad')
    app = importlib.import_module('app')
    session = SimpleNamespace(id='synthetic-session', status='pending', started_at=None, language='en')
    saved, scored = [], []
    monkeypatch.setattr(app, 'load_session', lambda _: session)
    monkeypatch.setattr(app, 'save_session', lambda _: None)
    monkeypatch.setattr(app, 'save_transcript_turn', lambda _, turn: saved.append(turn.text))

    def score(_):
        scored.append(list(saved))
        return SimpleNamespace(overall=4), 'synthetic-scorecard'
    monkeypatch.setattr(app, 'score_database_session', score)

    async def scenario():
        engine, browser = Socket(), Browser()

        @asynccontextmanager
        async def connect(config):
            assert config['session']['audio']['input']['turn_detection']['type'] == 'semantic_vad'
            yield engine
        monkeypatch.setattr(app, 'connect_to_engine_with_retry', connect)
        task = asyncio.create_task(app.interview_relay(browser, session.id))

        async def created(request, rid):
            await engine.emit({'type': 'response.created', 'response': {
                'id': rid, 'metadata': request['response']['metadata']}})

        async def audio_and_done(rid, iid, transcript):
            await engine.emit({'type': 'response.output_audio.delta', 'response_id': rid,
                               'item_id': iid, 'delta': 'AAA='})
            await engine.emit({'type': 'response.output_audio_transcript.done', 'response_id': rid,
                               'item_id': iid, 'transcript': transcript})
            await engine.emit({'type': 'response.done', 'response': {'id': rid, 'output': [{
                'id': iid, 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'audio', 'transcript': transcript}]}]}})

        async def vad(kind, iid):
            await engine.emit({'type': 'input_audio_buffer.' + kind, 'item_id': iid})

        async def tool_call(request, rid, name, missing_id_first=False):
            await created(request, rid)
            if missing_id_first:
                await engine.emit({'type': 'response.output_item.done', 'response_id': rid,
                                   'item': {'type': 'function_call', 'name': name}})
            await engine.emit({'type': 'response.output_item.done', 'response_id': rid,
                               'item': {'type': 'function_call', 'name': name, 'call_id': rid}})
            result = await message(engine, 'conversation.item.create')
            assert engine.sent.empty(), 'Continuation cannot overlap its tool response'
            # A duplicate tool event must not skip the next question.
            await engine.emit({'type': 'response.output_item.done', 'response_id': rid,
                               'item': {'type': 'function_call', 'name': name, 'call_id': rid}})
            await engine.emit({'type': 'response.done', 'response': {'id': rid, 'output': []}})
            return result, await message(engine, 'response.create')

        async def final_asr(iid, text):
            await engine.emit({'type': 'conversation.item.input_audio_transcription.completed',
                               'item_id': iid, 'transcript': text})
            while (await message(browser, 'transcript')).get('text') != text:
                pass

        try:
            opening = await message(engine, 'response.create')
            await created(opening, 'opening')
            await audio_and_done('opening', 'hello', 'Hello')
            await message(browser, 'opening_generated')
            await browser.emit({'type': 'opening_playback_finished'})
            await message(browser, 'interview_timer_started')
            await vad('speech_started', 'readiness')
            await vad('speech_stopped', 'readiness')
            await vad('committed', 'readiness')
            # No ASR has arrived. Native audio must already trigger a reply.
            reply = await message(engine, 'response.create')
            result, question_request = await tool_call(reply, 'start-plan', advance_tool, True)
            progress = json.loads(result['item']['output'])
            assert progress['finish_accepted'] is False
            assert progress['current'] == 'experience'
            expected = ['Hello', 'Yes']
            for index, topic in enumerate(TOPICS):
                question = topic.question('en')
                assert question in question_request['response']['instructions']
                assert question_request['response']['tools'] == []
                assert question_request['response']['metadata']['talentsift_kind'] == 'candidate'
                await created(question_request, f'question-response-{index}')
                await audio_and_done(f'question-response-{index}', f'question-{index}', question)
                # Drain through this question to establish event processing.
                while (await message(browser, 'transcript')).get('text') != question:
                    pass
                iid = f'answer-{index}'
                expected.append(question)
                await vad('speech_started', iid)
                if index == 0:
                    assert saved == ['Hello']
                    await final_asr('readiness', 'Yes')
                    assert saved == expected
                    assert engine.sent.empty(), 'Late ASR must not reply over new speech'
                await vad('speech_stopped', iid)
                await vad('committed', iid)
                # Again, the next reply must not wait for this answer's ASR.
                reply = await message(engine, 'response.create')
                answer_text = f'Answer for {topic.key}'
                expected.append(answer_text)
                if index < len(TOPICS) - 1:
                    await final_asr(iid, answer_text)
                    result, question_request = await tool_call(reply, f'advance-{index}', advance_tool)
                    progress = json.loads(result['item']['output'])
                    assert progress['finish_accepted'] is False
                    assert progress['current'] == TOPICS[index + 1].key
                    assert progress['completed'] == [t.key for t in TOPICS[:index + 1]]
                else:
                    result, closing = await tool_call(reply, 'finish', 'finish_interview')
                    assert result['item']['output'] == 'Interview completion accepted.'
            assert closing['response']['metadata']['talentsift_kind'] == 'closing'
            await created(closing, 'closing')
            await audio_and_done('closing', 'goodbye', 'Goodbye')
            await message(browser, 'closing_generated')
            await browser.emit({'type': 'closing_playback_finished'})
            await message(browser, 'interview_complete')
            assert scored == [], 'Must not score before the final answer is stored'
            await final_asr(iid, answer_text)
            async with asyncio.timeout(2):
                while not scored:
                    await asyncio.sleep(0.001)
            assert scored == [expected + ['Goodbye']]
            await browser.close()
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('ending', ['abrupt', 'clean_eof', 'browser_disconnect'])
def test_primary_disconnect_stops_both_pumps_and_marks_failed(ending, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-value')
    monkeypatch.setenv('TALENTSIFT_LIVE_CAPTIONS', '0')
    monkeypatch.setenv('TALENTSIFT_DATA_DIR', str(tmp_path))
    app = importlib.import_module('app')
    session = SimpleNamespace(id='synthetic-disconnect', status='pending', started_at=None, language='en')
    saved_statuses, saved_text = [], []
    monkeypatch.setattr(app, 'load_session', lambda _: session)
    monkeypatch.setattr(app, 'save_session', lambda item: saved_statuses.append(item.status))
    monkeypatch.setattr(app, 'save_transcript_turn', lambda _, turn: saved_text.append(turn.text))

    async def scenario():
        engine, browser = Socket(), Browser()
        @asynccontextmanager
        async def connect(config):
            try:
                yield engine
            finally:
                await engine.close()
        monkeypatch.setattr(app, 'connect_to_engine_with_retry', connect)
        task = asyncio.create_task(app.interview_relay(browser, session.id))
        try:
            opening = await message(engine, 'response.create')
            await engine.emit({'type': 'response.created', 'response': {
                'id': 'opening', 'metadata': opening['response']['metadata']}})
            await engine.emit({'type': 'response.done', 'response': {'id': 'opening', 'output': []}})
            await message(browser, 'opening_generated')
            await browser.emit({'type': 'opening_playback_finished'})
            await message(browser, 'interview_timer_started')
            await browser.emit({'type': 'audio', 'data': 'AAA='})
            await message(engine, 'input_audio_buffer.append')
            # Ready text behind a missing ASR result must survive shutdown.
            await engine.emit({'type': 'input_audio_buffer.committed', 'item_id': 'missing-asr'})
            await engine.emit({'type': 'response.output_audio_transcript.done', 'item_id': 'partial-question',
                               'transcript': 'Already received text'})
            await message(browser, 'transcript')
            assert not saved_text
            if ending == 'abrupt':
                await engine.incoming.put(ConnectionClosedError(None, None))
            elif ending == 'clean_eof':
                await engine.close()
            else:
                await browser.close()
            await asyncio.wait_for(task, 2)
            assert session.status == 'failed'
            assert session.ended_at is not None
            assert saved_statuses[-1] == 'failed'
            assert saved_text == ['Already received text']
            assert not [t for t in asyncio.all_tasks() if t.get_name().startswith('interview:')]
            if ending != 'browser_disconnect':
                failure = await message(browser, 'interview_error')
                assert failure['code'] == 'connection_lost'
                assert browser.close_code == 1011
            output = capsys.readouterr().out
            assert '[audio] First candidate microphone chunk forwarded to OpenAI' in output
            assert '"forwarded_audio_chunks": 1' in output
            assert '"opening_complete": true' in output
            assert 'AAA=' not in output
            if ending == 'abrupt':
                assert 'no close frame received or sent' in output
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('ending', ['candidate_stop', 'time_limit'])
def test_incomplete_coverage_does_not_block_explicit_stop_or_deadline(ending, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-value')
    monkeypatch.setenv('TALENTSIFT_LIVE_CAPTIONS', '0')
    monkeypatch.setenv('TALENTSIFT_DATA_DIR', str(tmp_path))
    app = importlib.import_module('app')
    session = SimpleNamespace(id='incomplete-interview', status='pending', started_at=None, language='en')
    monkeypatch.setattr(app, 'load_session', lambda _: session)
    monkeypatch.setattr(app, 'save_session', lambda _: None)
    monkeypatch.setattr(app, 'save_transcript_turn', lambda *_: None)
    monkeypatch.setattr(app, 'score_database_session', lambda _: (SimpleNamespace(overall=None), 'score'))
    if ending == 'time_limit':
        monkeypatch.setattr(app, 'INTERVIEW_MAX_SECONDS', 0.03)
        monkeypatch.setattr(app, 'INTERVIEW_WARNING_SECONDS', 0.01)

    async def scenario():
        engine, browser = Socket(), Browser()

        @asynccontextmanager
        async def connect(config):
            yield engine
        monkeypatch.setattr(app, 'connect_to_engine_with_retry', connect)
        task = asyncio.create_task(app.interview_relay(browser, session.id))
        try:
            opening = await message(engine, 'response.create')
            await engine.emit({'type': 'response.created', 'response': {
                'id': 'opening', 'metadata': opening['response']['metadata']}})
            await engine.emit({'type': 'response.done', 'response': {'id': 'opening', 'output': []}})
            await message(browser, 'opening_generated')
            await browser.emit({'type': 'opening_playback_finished'})
            await message(browser, 'interview_timer_started')
            if ending == 'candidate_stop':
                await browser.emit({'type': 'end_interview'})
                await message(browser, 'interview_ended_early')
                assert session.status == 'ended_early'
                assert engine.sent.empty()
            else:
                deadline = await message(engine, 'response.create')
                assert deadline['response']['metadata']['talentsift_kind'] == 'deadline'
                await engine.emit({'type': 'response.created', 'response': {
                    'id': 'deadline', 'metadata': deadline['response']['metadata']}})
                await engine.emit({'type': 'response.output_item.done', 'response_id': 'deadline',
                                   'item': {'type': 'function_call', 'name': 'finish_interview', 'call_id': 'timeout'}})
                result = await message(engine, 'conversation.item.create')
                assert result['item']['output'] == 'Interview completion accepted.'
                await engine.emit({'type': 'response.done', 'response': {'id': 'deadline', 'output': []}})
                closing = await message(engine, 'response.create')
                assert closing['response']['metadata']['talentsift_kind'] == 'closing'
                output = capsys.readouterr().out
                assert '"reason": "time_limit"' in output
                assert '"completed": []' in output
                assert 'collaboration' in output
            await browser.close()
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
