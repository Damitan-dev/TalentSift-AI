"""Security/admission tests use isolated SQLite files and never call OpenAI."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import importlib
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import database
from access_control import AccessError, AccessSettings, AccessStore, password_hash, verify_password
from models import JobListing, TranscriptTurn
from recording_storage import RecordingStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unused-test-value')
    monkeypatch.delenv('TALENTSIFT_RECRUITER_USERNAME', raising=False)
    monkeypatch.delenv('TALENTSIFT_RECRUITER_PASSWORD_HASH', raising=False)
    monkeypatch.delenv('TALENTSIFT_PUBLIC_URL', raising=False)
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'test.db')
    database.initialize_database()
    module = importlib.import_module('app')
    store = AccessStore(AccessSettings())
    store.initialize()
    monkeypatch.setattr(module.app.state, 'access', store)
    recording = RecordingStore(tmp_path / 'recordings')
    monkeypatch.setattr(module, 'recording_store', recording)
    import recording_routes
    monkeypatch.setattr(recording_routes, 'recording_store', recording)
    job = JobListing(title='Junior Python Backend Developer')
    database.save_job(job)
    return module, store, job


@pytest.fixture
def http(env):
    with TestClient(env[0].app, base_url='http://localhost', headers={'Origin': 'http://localhost'}, follow_redirects=False) as client:
        yield client


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def recruiter(http, store):
    store.configure_recruiter('damitan', password_hash('a long synthetic test password'))
    login = http.get('/login')
    result = http.post('/login', data={'csrf_token': csrf(login.text), 'username': 'damitan', 'password': 'a long synthetic test password'})
    assert result.status_code == 303
    return csrf(http.get('/recruiter/jobs').text)


def invite(http, store, job, name='David Adebayo'):
    iid, token = store.create_invitation(job.id, name, 72)
    response = http.post('/api/invitations/exchange', json={'token': token})
    assert response.status_code == 200
    return iid, token, response.json()['csrf_token']


def prepare(http, token, **fields):
    return http.post('/api/session', headers={'X-CSRF-Token': token},
                     json={'language':'en', 'consent':True, **fields})


def test_no_default_account_and_password_not_stored_plaintext(env, http):
    _, store, _ = env
    assert http.get('/login').status_code == 503
    encoded = password_hash('a long synthetic test password')
    store.configure_recruiter('Damitan', encoded)
    assert store.account()['username'] == 'damitan'
    assert 'synthetic' not in store.account()['password_hash']
    assert verify_password('a long synthetic test password', encoded)
    assert not verify_password('wrong password', encoded)
    with pytest.raises(ValueError):
        password_hash('short')


def test_all_recruiter_routes_and_direct_downloads_require_login(env, http):
    module, _, job = env
    paths = [r.path for r in module.app.routes if getattr(r, 'path', '').startswith('/recruiter/')]
    assert len(paths) >= 10
    for template in paths:
        path = template.replace('{job_id}', job.id).replace('{session_id}', str(uuid4())).replace('{invitation_id}', str(uuid4()))
        route = next(r for r in module.app.routes if getattr(r,'path','') == template)
        for method in getattr(route, 'methods', []):
            response = http.request(method, path)
            assert response.status_code == (303 if method == 'GET' else 401), (method, path, response.text)
    assert http.get('/api/jobs/' + job.id).status_code == 401
    assert http.get('/docs').status_code == 404


def test_login_csrf_cookies_logout_and_password_reset(env, http):
    _, store, _ = env
    store.configure_recruiter('damitan', password_hash('a long synthetic test password'))
    page = http.get('/login')
    assert 'HttpOnly' in page.headers['set-cookie'] and 'SameSite=lax' in page.headers['set-cookie']
    assert http.post('/login', data={'username':'damitan','password':'a long synthetic test password'}).status_code == 403
    bad = http.post('/login', data={'csrf_token':csrf(page.text),'username':'damitan','password':'wrong'})
    assert bad.status_code == 401
    result = http.post('/login', data={'csrf_token':csrf(page.text),'username':'damitan','password':'a long synthetic test password'})
    assert result.status_code == 303
    old_cookie = http.cookies.get('ts_recruiter')
    assert 'HttpOnly' in result.headers['set-cookie']
    dashboard = http.get('/recruiter/jobs')
    assert dashboard.status_code == 200 and dashboard.headers['cache-control'] == 'no-store'
    assert http.post('/recruiter/jobs', data={'title':'Hacked'}).status_code == 403
    assert http.post('/logout', data={'csrf_token':csrf(dashboard.text)}).status_code == 303
    assert not store.get_session(old_cookie, 'recruiter')
    http.cookies.set('ts_recruiter', old_cookie)
    assert http.get('/recruiter/jobs').status_code == 303
    session_token, _, _ = store.issue_session('recruiter')
    store.configure_recruiter('damitan', password_hash('another synthetic test password'), replace=True)
    assert store.get_session(session_token, 'recruiter') is None


def test_failed_logins_are_throttled(env, http):
    _, store, _ = env
    store.configure_recruiter('damitan', password_hash('a long synthetic test password'))
    token = csrf(http.get('/login').text)
    for _ in range(10):
        assert http.post('/login', data={'csrf_token':token,'username':'damitan','password':'wrong'}).status_code == 401
    assert http.post('/login', data={'csrf_token':token,'username':'damitan','password':'wrong'}).status_code == 429


def test_cookie_expiry_and_idle_timeout(env):
    _, store, _ = env
    now = store.clock()
    store.clock = lambda: now
    token, _, _ = store.issue_session('recruiter')
    store.clock = lambda: now + 3601
    assert store.get_session(token, 'recruiter') is None
    store.clock = lambda: now
    token, _, _ = store.issue_session('candidate')
    store.clock = lambda: now + 8 * 3600 + 1
    assert store.get_session(token, 'candidate') is None


def test_same_origin_csrf_host_and_public_secure_cookie(env, http):
    module, store, job = env
    iid, secret = store.create_invitation(job.id, 'David', 72)
    assert http.post('/api/invitations/exchange', json={'token':secret}, headers={'Origin':'https://evil.example'}).status_code == 403
    assert http.get('/recruiter/jobs', headers={'Host':'evil.example'}).status_code == 503
    assert http.post('/api/invitations/exchange', content='token=x', headers={'Content-Type':'text/plain'}).status_code == 415
    store.settings = replace(store.settings, public_url='https://talentsift.example')
    with TestClient(module.app, base_url='https://talentsift.example', headers={'Origin':'https://talentsift.example'}) as client:
        response = client.post('/api/invitations/exchange', json={'token':secret})
        assert response.status_code == 200
        cookie = response.headers['set-cookie']
        assert '__Host-ts_candidate=' in cookie and 'Secure' in cookie and 'HttpOnly' in cookie
        assert 'Domain=' not in cookie
        assert client.post('/api/session', json={'consent':True,'language':'en'}, headers={'X-CSRF-Token':response.json()['csrf_token']}).status_code == 200


def test_recruiter_creates_named_invitation_no_shared_link_and_can_revoke(env, http):
    _, store, job = env
    token = recruiter(http, store)
    result = http.post(f'/recruiter/job/{job.id}/invitations', data={'csrf_token':token, 'candidate_name':'  David   Adebayo ', 'expires_hours':72})
    assert result.status_code == 200
    assert '/#invite=' in result.text and 'David Adebayo' in result.text
    secret = re.search(r'/#invite=([A-Za-z0-9_-]+)', result.text).group(1)
    row = store.list_invitations(job.id)[0]
    assert row['candidate_name'] == 'David Adebayo'
    dashboard = http.get(f'/recruiter/job/{job.id}')
    assert '?job_id=' not in dashboard.text and 'Create invitation' in dashboard.text
    assert secret not in dashboard.text  # Displayed only once; only a hash is stored.
    assert http.post('/api/invitations/exchange', json={'token':secret}).status_code == 200
    assert http.post(f'/recruiter/job/{job.id}/invitations/{row["id"]}/revoke', data={'csrf_token':token}).status_code == 303
    assert http.get('/api/invitation').status_code == 401
    assert http.post('/api/invitations/exchange', json={'token':secret}).status_code == 410


def test_expired_and_forged_invites_fail(env, http):
    _, store, job = env
    _, token = store.create_invitation(job.id, 'David', 1)
    now = store.clock()
    store.clock = lambda: now + 3601
    assert http.post('/api/invitations/exchange', json={'token':token}).status_code == 410
    assert http.post('/api/invitations/exchange', json={'token':'x'*43}).status_code == 404


def test_consent_preparation_idempotence_and_identity_from_invitation(env, http):
    _, store, job = env
    iid, secret, token = invite(http, store, job)
    assert prepare(http, 'wrong').status_code == 403
    assert prepare(http, token, full_name='Fake', job_id=job.id, candidate_id='fake').status_code == 422
    assert prepare(http, token, consent=False).status_code == 400
    assert prepare(http, token, language='xx').status_code == 400
    one = prepare(http, token).json()
    two = prepare(http, token, language='fr').json()
    assert one['session_id'] == two['session_id']
    assert database.load_candidate(one['candidate_id']).full_name == 'David Adebayo'
    assert database.load_session(one['session_id']).language == 'fr'
    assert store.usage()['daily'] == 0
    assert store.invitation(token=secret)['started_at'] is None
    assert http.get('/api/invitation').json()['candidate_name'] == 'David Adebayo'


def test_simultaneous_use_of_one_invitation_admits_exactly_once(env):
    _, store, job = env
    iid, _ = store.create_invitation(job.id, 'David', 72)
    sid, _ = store.prepare_interview(iid, 'en')
    def attempt(_):
        try:
            store.claim_interview(iid, sid)
            return True
        except AccessError:
            return False
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert store.usage()['daily'] == 1
    store.finish_interview(sid)
    with pytest.raises(AccessError, match='already been used'):
        store.claim_interview(iid, sid)


def test_atomic_daily_quota_and_restart_persistence(env):
    _, store, job = env
    store.settings = replace(store.settings, daily_limit=1)
    invites = [store.create_invitation(job.id, name, 72)[0] for name in ['David', 'Mary']]
    pairs = [(iid, store.prepare_interview(iid, 'en')[0]) for iid in invites]
    def attempt(pair):
        try:
            store.claim_interview(*pair)
            return True
        except AccessError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, pairs)) == 1
    restarted = AccessStore(store.settings)
    restarted.initialize()
    assert restarted.usage()['daily'] == 1
    with pytest.raises(AccessError, match='limit'):
        iid = store.create_invitation(job.id, 'John', 72)[0]
        restarted.prepare_interview(iid, 'en')


def test_concurrent_quota_releases_but_attempt_count_remains(env):
    _, store, job = env
    store.settings = replace(store.settings, concurrent_limit=1)
    iid = store.create_invitation(job.id, 'David', 72)[0]
    sid, _ = store.prepare_interview(iid, 'en')
    second = store.create_invitation(job.id, 'Mary', 72)[0]
    other, _ = store.prepare_interview(second, 'en')
    store.claim_interview(iid, sid)
    with pytest.raises(AccessError, match='busy'):
        store.claim_interview(second, other)
    assert store.invitation(invitation_id=second)['started_at'] is None
    store.finish_interview(sid)
    store.claim_interview(second, other)
    assert store.usage()['daily'] == 2 and store.usage()['active'] == 1


def test_unknown_session_wrong_invitation_and_no_cookie_never_open_engine(env, http, monkeypatch):
    module, store, job = env
    calls = []
    async def forbidden(*args):
        calls.append(args)
        raise AssertionError('Unauthorized relay')
    monkeypatch.setattr(module, 'interview_relay', forbidden)
    with pytest.raises(WebSocketDisconnect):
        with http.websocket_connect('ws://localhost/ws/interview/' + str(uuid4())):
            pass
    iid, _, token = invite(http, store, job)
    sid = prepare(http, token).json()['session_id']
    other = store.create_invitation(job.id, 'Mary', 72)[0]
    other_sid = store.prepare_interview(other, 'en')[0]
    for target in [other_sid, str(uuid4())]:
        with http.websocket_connect('ws://localhost/ws/interview/' + target) as ws:
            assert ws.receive_json()['code'] == 'access_denied'
    with pytest.raises(WebSocketDisconnect):
        with http.websocket_connect('ws://localhost/ws/interview/' + sid, headers={'Origin':'https://evil.example'}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with http.websocket_connect('ws://localhost/ws/recording/' + other_sid):
            pass
    assert calls == [] and store.usage()['daily'] == 0


def test_authorized_websocket_runs_once_and_releases_slot(env, http, monkeypatch):
    module, store, job = env
    iid, secret, token = invite(http, store, job)
    sid = prepare(http, token).json()['session_id']
    calls = []
    async def complete(ws, session_id):
        calls.append(session_id)
        await ws.accept()
        session = database.load_session(session_id)
        session.status = 'completed'
        database.save_session(session)
        await ws.send_json({'type':'interview_complete'})
        await ws.close()
    monkeypatch.setattr(module, 'interview_relay', complete)
    with http.websocket_connect('ws://localhost/ws/interview/' + sid) as ws:
        assert ws.receive_json()['type'] == 'interview_complete'
    with http.websocket_connect('ws://localhost/ws/interview/' + sid) as ws:
        assert ws.receive_json()['code'] == 'access_denied'
    assert len(calls) == 1
    assert store.usage()['daily'] == 1 and store.usage()['active'] == 0
    assert http.post('/api/invitations/exchange', json={'token':secret}).status_code == 409
    assert prepare(http, token).status_code == 409


def test_hard_deadline_cancels_stalled_start_and_keeps_invitation_used(env, http, monkeypatch):
    module, store, job = env
    class QuickSettings(AccessSettings):
        @property
        def hard_seconds(self):
            return 0.05
    store.settings = QuickSettings()
    iid, _, token = invite(http, store, job)
    sid = prepare(http, token).json()['session_id']
    cancelled = []
    async def stall(ws, sid):
        await ws.accept()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(module, 'interview_relay', stall)
    with http.websocket_connect('ws://localhost/ws/interview/' + sid) as ws:
        assert ws.receive_json()['code'] == 'time_limit'
    assert cancelled == [True]
    assert database.load_session(sid).status == 'failed'
    assert store.usage()['active'] == 0 and store.usage()['daily'] == 1


def test_recording_cookie_binding_and_recruiter_only_audio_transcript(env, http):
    module, store, job = env
    _, _, token = invite(http, store, job)
    prepared = prepare(http, token, record_audio=True).json()
    sid = prepared['session_id']
    with http.websocket_connect('ws://localhost/ws/recording/' + sid) as ws:
        ws.send_json({'type':'start','token':prepared['recording_token'],'mime_type':'audio/webm'})
        assert ws.receive_json()['type'] == 'recording_ready'
        ws.send_bytes(b'synthetic audio bytes')
        ws.send_json({'type':'finish','complete':True})
        assert ws.receive_json()['type'] == 'recording_saved'
    database.save_transcript_turn(sid, TranscriptTurn(speaker='candidate', text='Private candidate response.'))
    assert http.get(f'/recruiter/session/{sid}/audio').status_code == 303
    assert http.get(f'/recruiter/session/{sid}/transcript.txt').status_code == 303
    recruiter(http, store)
    assert http.get(f'/recruiter/session/{sid}/audio').content == b'synthetic audio bytes'
    transcript = http.get(f'/recruiter/session/{sid}/transcript.txt')
    assert transcript.status_code == 200 and 'Private candidate response.' in transcript.text


def test_expired_lease_after_crash_frees_concurrency_without_resetting_usage(env):
    _, store, job = env
    now = store.clock()
    store.clock = lambda: now
    iid, _ = store.create_invitation(job.id, 'David', 72)
    sid, _ = store.prepare_interview(iid, 'en')
    store.claim_interview(iid, sid)
    store.clock = lambda: now + store.settings.hard_seconds + 31
    assert store.usage()['active'] == 0
    assert store.usage()['daily'] == 1
    assert database.load_session(sid).status == 'failed'
    with pytest.raises(AccessError, match='used'):
        store.claim_interview(iid, sid)


def test_pcm_budget_rejects_invalid_and_accelerated_audio():
    import base64
    from access_control import AudioBudget
    now = [0]
    budget = AudioBudget(clock=lambda: now[0])
    second = base64.b64encode(bytes(48000)).decode()
    for _ in range(10):
        budget.accept(second)
    with pytest.raises(AccessError, match='streaming rate'):
        budget.accept(second)
    now[0] += 1
    budget.accept(second)
    for invalid in ['not base64', 'AAA', base64.b64encode(b'odd').decode(), 'a'*128001]:
        with pytest.raises(AccessError):
            AudioBudget().accept(invalid)


@pytest.mark.parametrize('speed', [1, 1.019])
def test_pcm_budget_allows_a_full_interview_with_small_clock_drift(speed):
    import base64
    from access_control import AudioBudget
    now = [0.0]
    budget = AudioBudget(clock=lambda: now[0])
    chunk = base64.b64encode(bytes(1024 * 2)).decode()
    for _ in range(int(600 * 24000 * speed / 1024)):
        now[0] += 1024 / (24000 * speed)
        budget.accept(chunk)


def test_pcm_budget_accepts_bounded_network_catchup_but_not_duplicate_streams():
    import base64
    from access_control import AudioBudget, AudioRateError
    now = [0.0]
    budget = AudioBudget(clock=lambda: now[0])
    second = base64.b64encode(bytes(48000)).decode()
    # A ten-second network backlog arrives in a burst, then normal audio resumes.
    now[0] += 10
    for _ in range(10):
        budget.accept(second)
    for _ in range(600):
        now[0] += 1
        budget.accept(second)
    # Two capture pipelines (or unconverted 48 kHz) must still be bounded.
    for _ in range(12):
        now[0] += 1
        try:
            budget.accept(second)
            budget.accept(second)
        except AudioRateError as error:
            assert error.status == 429
            assert error.details['incoming_pcm_bytes'] == 48000
            assert error.details['audio_seconds_received'] > 0
            assert second not in str(error.details)
            break
    else:
        pytest.fail('A sustained double-rate stream escaped the budget')


def test_ws_rechecks_invitation_expiry_and_quota_after_microphone_setup(env, http, monkeypatch):
    module, store, job = env
    _, _, token = invite(http, store, job)
    sid = prepare(http, token).json()['session_id']
    now = store.clock()
    # Candidate's grant is still alive, but the underlying invitation has expired.
    with store.transaction() as db:
        db.execute('UPDATE invitations SET expires_at=? WHERE session_id=?', (now - 1, sid))
    with http.websocket_connect('ws://localhost/ws/interview/' + sid) as ws:
        assert 'expired' in ws.receive_json()['message']
    assert store.usage()['daily'] == 0


def test_candidate_cannot_open_recruiter_pages_or_forge_session_cookie(env, http):
    _, store, job = env
    _, _, token = invite(http, store, job)
    assert http.get('/recruiter/jobs').status_code == 303
    assert http.post('/recruiter/jobs', json={'title':'Fake'}, headers={'X-CSRF-Token':token}).status_code == 401
    http.cookies.set('ts_recruiter', 'forged-session-token')
    assert http.get('/recruiter/jobs').status_code == 303


def test_reloading_unused_invitation_can_still_opt_into_audio(env, http):
    _, store, job = env
    _, _, token = invite(http, store, job)
    first = prepare(http, token, record_audio=True).json()
    second = prepare(http, token, record_audio=True).json()
    assert first['session_id'] == second['session_id']
    assert first['recording_token'] != second['recording_token']
    assert second['recording_token'] and not second['recording_error']
    with http.websocket_connect('ws://localhost/ws/recording/' + second['session_id']) as ws:
        ws.send_json({'type':'start','token':second['recording_token'],'mime_type':'audio/webm'})
        assert ws.receive_json()['type'] == 'recording_ready'
        ws.send_bytes(b'synthetic')
        ws.send_json({'type':'finish','complete':True})
        assert ws.receive_json()['type'] == 'recording_saved'
    third = prepare(http, token, record_audio=True).json()
    assert third['recording_error'] and third['recording_token'] is None


def test_score_changes_use_authenticated_recruiter_identity(env, http, monkeypatch):
    module, store, _ = env
    token = recruiter(http, store)
    actors = []
    monkeypatch.setattr(module, 'apply_recruiter_override', lambda **kwargs: actors.append(kwargs['overridden_by']))
    monkeypatch.setattr(module, 'restore_ai_score', lambda **kwargs: actors.append(kwargs['restored_by']))
    sid = str(uuid4())
    assert http.post(f'/recruiter/session/{sid}/override', data={'csrf_token':token,'score':4,'reason':'Reviewed evidence'}).status_code == 303
    assert http.post(f'/recruiter/session/{sid}/restore-ai', data={'csrf_token':token,'reason':'Restore assessment'}).status_code == 303
    assert actors == ['damitan', 'damitan']
