"""Exercise the real FastAPI relay with controlled OpenAI events."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from types import SimpleNamespace
from fastapi import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
import pytest


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


def test_relay_replies_before_asr_but_saves_in_order_and_waits_to_score(tmp_path, monkeypatch):
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

        try:
            opening = await message(engine, 'response.create')
            await created(opening, 'opening')
            await audio_and_done('opening', 'hello', 'Hello')
            await message(browser, 'opening_generated')
            await browser.emit({'type': 'opening_playback_finished'})
            await message(browser, 'interview_timer_started')
            await vad('speech_started', 'answer-1')
            await vad('speech_stopped', 'answer-1')
            await vad('committed', 'answer-1')
            # No ASR has arrived. Native audio must already trigger a reply.
            reply = await message(engine, 'response.create')
            await created(reply, 'reply-1')
            await audio_and_done('reply-1', 'question-1', 'Next question')
            await message(browser, 'transcript')
            assert saved == ['Hello']
            await vad('speech_started', 'answer-2')
            await engine.emit({'type': 'conversation.item.input_audio_transcription.completed',
                               'item_id': 'answer-1', 'transcript': 'Yes'})
            while (await message(browser, 'transcript')).get('speaker') != 'candidate':
                pass
            assert engine.sent.empty(), 'Late ASR must not answer over new speech'
            assert saved == ['Hello', 'Yes', 'Next question']
            await vad('speech_stopped', 'answer-2')
            await vad('committed', 'answer-2')
            last = await message(engine, 'response.create')
            await created(last, 'tool-response')
            await engine.emit({'type': 'response.output_item.done', 'response_id': 'tool-response',
                               'item': {'type': 'function_call', 'name': 'finish_interview', 'call_id': 'call'}})
            await message(engine, 'conversation.item.create')
            assert engine.sent.empty(), 'Closing cannot overlap the active tool response'
            await engine.emit({'type': 'response.done', 'response': {'id': 'tool-response', 'output': []}})
            closing = await message(engine, 'response.create')
            assert closing['response']['metadata']['talentsift_kind'] == 'closing'
            await created(closing, 'closing')
            await audio_and_done('closing', 'goodbye', 'Goodbye')
            await message(browser, 'closing_generated')
            await browser.emit({'type': 'closing_playback_finished'})
            await message(browser, 'interview_complete')
            assert scored == [], 'Must not score before final answer is stored'
            await engine.emit({'type': 'conversation.item.input_audio_transcription.completed',
                               'item_id': 'answer-2', 'transcript': 'Final answer'})
            async with asyncio.timeout(2):
                while not scored:
                    await asyncio.sleep(0.001)
            assert scored == [['Hello', 'Yes', 'Next question', 'Final answer', 'Goodbye']]
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
