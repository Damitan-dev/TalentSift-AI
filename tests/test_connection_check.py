"""The connection diagnostic must be bounded and safe to share."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

import check_realtime_connection as check


class Socket:
    latency = 0.001

    def __init__(self, events=()):
        self.events = list(events)
        self.sent = []
        self.closed = False
        self.waiting = asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.events:
            event = self.events.pop(0)
            if isinstance(event, Exception):
                raise event
            return json.dumps(event)
        self.waiting.set()
        await asyncio.Future()

    async def ping(self):
        pong = asyncio.get_running_loop().create_future()
        pong.set_result(self.latency)
        return pong


def connector(socket, seen=None, failure=None):
    @asynccontextmanager
    async def connect(url, **kwargs):
        if seen is not None:
            seen.update(url=url, **kwargs)
        if failure is not None:
            raise failure
        try:
            yield socket
        finally:
            socket.closed = True
    return connect


def reports(lines):
    return [json.loads(line.split(' ', 1)[1]) for line in lines]


def test_idle_connection_needs_ready_ack_and_pong_and_sends_no_audio_or_reply_request():
    async def scenario():
        socket = Socket([{'type': 'session.created'}, {'type': 'session.updated'}])
        lines, seen = [], {}
        result = await check.check_connection('test-secret', 0.02, emit=lines.append,
                                               connector=connector(socket, seen))
        assert result == 0 and socket.closed
        assert seen['additional_headers'] == {'Authorization': 'Bearer test-secret'}
        assert seen['ping_interval'] == 20 and seen['ping_timeout'] == 60
        assert seen['open_timeout'] == 12
        assert seen['url'].endswith('model=gpt-realtime')
        assert len(socket.sent) == 1
        assert socket.sent[0]['type'] == 'session.update'
        assert socket.sent[0]['session']['audio']['input']['turn_detection'] is None
        final = reports(lines)[-1]
        assert final['result'] == 'stable' and final['final_ping_latency_ms'] == 1
        assert final['active_connection_ms'] >= 20
        assert 'test-secret' not in '\n'.join(lines)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [
    ConnectionClosedError(None, None),
    ConnectionClosedOK(Close(1000, 'Remote stopped'), Close(1000, 'Remote stopped'), True),
])
def test_early_remote_close_is_a_failure_even_with_normal_close_code(failure):
    async def scenario():
        socket = Socket([{'type': 'session.updated'}, failure])
        lines = []
        assert await check.check_connection('test-secret', 0.02, emit=lines.append,
                                             connector=connector(socket)) == 1
        final = reports(lines)[-1]
        assert final['phase'] == 'observing'
        assert final['result'] == 'failed'
        assert final['active_connection_ms'] is not None
        assert not any(report['result'] == 'stable' for report in reports(lines))
        assert socket.closed
    asyncio.run(scenario())


def test_handshake_failure_reports_phase_without_logging_credentials():
    async def scenario():
        error = TimeoutError('handshake failed Bearer test-secret')
        lines = []
        assert await check.check_connection('test-secret', emit=lines.append,
                                             connector=connector(Socket(), failure=error)) == 1
        final = reports(lines)[-1]
        assert final['phase'] == 'handshake' and final['active_connection_ms'] is None
        assert final['exception'] == 'TimeoutError'
        assert 'test-secret' not in '\n'.join(lines)
    asyncio.run(scenario())


def test_rejected_session_preserves_error_code_and_redacts_messages():
    async def scenario():
        socket = Socket([{'type': 'error', 'error': {
            'code': 'insufficient_quota', 'message': 'Rejected sk-othersecret and Bearer test-secret'}}])
        lines = []
        assert await check.check_connection('test-secret', emit=lines.append,
                                             connector=connector(socket)) == 1
        final = reports(lines)[-1]
        assert final['provider_error_code'] == 'insufficient_quota'
        assert final['phase'] == 'session_setup'
        assert 'test-secret' not in '\n'.join(lines) and 'sk-othersecret' not in '\n'.join(lines)
        assert socket.closed
    asyncio.run(scenario())


def test_missing_configuration_acknowledgement_times_out_and_closes(monkeypatch):
    monkeypatch.setattr(check, 'SETUP_TIMEOUT', 0.01)
    async def scenario():
        socket = Socket([{'type': 'session.created'}])
        lines = []
        assert await check.check_connection('test-secret', emit=lines.append,
                                             connector=connector(socket)) == 1
        final = reports(lines)[-1]
        assert final['phase'] == 'session_setup' and final['exception'] == 'TimeoutError'
        assert final['last_engine_event'] == 'session.created'
        assert socket.closed
    asyncio.run(scenario())


def test_half_open_connection_cannot_pass_without_a_fresh_pong(monkeypatch):
    monkeypatch.setattr(check, 'PONG_TIMEOUT', 0.01)
    async def scenario():
        socket = Socket([{'type': 'session.updated'}])
        async def unanswered_ping():
            return asyncio.Future()
        socket.ping = unanswered_ping
        lines = []
        assert await check.check_connection('test-secret', 0.01, emit=lines.append,
                                             connector=connector(socket)) == 1
        final = reports(lines)[-1]
        assert final['phase'] == 'final_ping' and final['exception'] == 'TimeoutError'
        assert socket.closed
    asyncio.run(scenario())


def test_cancelling_check_closes_its_connection():
    async def scenario():
        socket = Socket([{'type': 'session.updated'}])
        task = asyncio.create_task(check.check_connection('test-secret', connector=connector(socket), emit=lambda _: None))
        await asyncio.wait_for(socket.waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert socket.closed
    asyncio.run(scenario())


def test_unexpected_response_stops_check_without_printing_its_contents():
    async def scenario():
        socket = Socket([{'type': 'session.updated'}, {'type': 'response.created',
                          'response': {'text': 'Content must not be printed'}}])
        lines = []
        assert await check.check_connection('test-secret', emit=lines.append,
                                             connector=connector(socket)) == 1
        assert socket.closed and 'Content must not be printed' not in '\n'.join(lines)
    asyncio.run(scenario())


def test_diagnostic_redacts_authenticated_proxy_urls():
    result = check.safe_text('https://user:password@proxy.test and socks5://user@proxy.test', 'other-key')
    assert 'password' not in result and 'user' not in result


def test_missing_key_stops_before_a_connection(monkeypatch, capsys):
    monkeypatch.setattr(check, 'load_dotenv', lambda *_: None)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(check, 'check_connection', lambda *_: pytest.fail('Must not connect without a key'))
    assert check.main([]) == 2
    assert 'missing_api_key' in capsys.readouterr().out


@pytest.mark.parametrize('value', ['0', '301', 'nan'])
def test_cli_rejects_unbounded_observation_time(value):
    with pytest.raises(SystemExit) as error:
        check.main(['--seconds', value])
    assert error.value.code == 2
