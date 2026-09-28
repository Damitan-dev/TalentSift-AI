"""Recording persistence and transport checks; no microphone or OpenAI required."""

import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import recording_routes
import recording_storage
from recording_storage import RecordingError, RecordingStore


@pytest.fixture
def recording(tmp_path):
    store = RecordingStore(tmp_path / "recordings")
    session_id = str(uuid4())
    token = store.prepare(session_id)
    return store, session_id, token


def test_save_appends_every_chunk_and_hides_token(recording):
    store, sid, token = recording
    writer = store.begin(sid, token, "audio/webm;codecs=opus")
    writer.append(b"first chunk")
    writer.append(b"final chunk")
    writer.finish(True)
    detail = store.details(sid)
    assert detail["status"] == "saved"
    assert detail["path"].read_bytes() == b"first chunkfinal chunk"
    assert "token_hash" not in detail
    assert token not in (store.directory(sid) / "metadata.json").read_text()
    assert writer.finish(False)["status"] == "saved"  # Late disconnect is harmless.


def test_requires_consent_and_valid_token_and_cannot_overwrite(recording):
    store, sid, token = recording
    with pytest.raises(RecordingError, match="not enabled"):
        store.begin(str(uuid4()), token, "audio/webm")
    with pytest.raises(RecordingError, match="authorization"):
        store.begin(sid, "wrong-token", "audio/webm")
    with pytest.raises(RecordingError, match="Unsupported"):
        store.begin(sid, token, "text/html")
    writer = store.begin(sid, token, "audio/webm")
    writer.append(b"keep this")
    with pytest.raises(RecordingError, match="already started"):
        store.begin(sid, token, "audio/mp4")
    writer.finish(False)
    assert store.details(sid)["path"].read_bytes() == b"keep this"


@pytest.mark.parametrize("sid", ["../private", "../../data", "not-a-session", ""])
def test_recording_paths_cannot_escape_root(recording, sid):
    store, _, _ = recording
    with pytest.raises(RecordingError):
        store.details(sid)


def test_oversize_chunk_keeps_previously_received_audio(recording, monkeypatch):
    store, sid, token = recording
    monkeypatch.setattr(recording_storage, "MAX_RECORDING_BYTES", 10)
    writer = store.begin(sid, token, "audio/ogg")
    writer.append(b"valid")
    with pytest.raises(RecordingError, match="limit"):
        writer.append(b"too much audio")
    writer.finish(False)
    detail = store.details(sid)
    assert detail["status"] == "interrupted"
    assert detail["path"].read_bytes() == b"valid"


@pytest.fixture
def client(recording, monkeypatch):
    store, sid, token = recording
    monkeypatch.setattr(recording_routes, "recording_store", store)

    def load_session(session_id):
        if session_id != sid:
            raise KeyError(session_id)
        return SimpleNamespace(consent_given=True, status="in_progress")

    monkeypatch.setattr(recording_routes, "load_session", load_session)
    app = FastAPI()
    app.include_router(recording_routes.router)
    with TestClient(app) as http:
        yield http, store, sid, token


def start(websocket, token):
    websocket.send_json({"type": "start", "token": token, "mime_type": "audio/webm"})
    assert websocket.receive_json()["type"] == "recording_ready"


def test_websocket_final_chunk_playback_download_and_seek(client):
    http, store, sid, token = client
    with http.websocket_connect(f"/ws/recording/{sid}") as websocket:
        start(websocket, token)
        websocket.send_bytes(b"first-")
        websocket.send_bytes(b"last")
        websocket.send_json({"type": "finish", "complete": True})
        assert websocket.receive_json() == {
            "type": "recording_saved", "status": "saved", "bytes": 10
        }
    response = http.get(f"/recruiter/session/{sid}/audio")
    assert response.content == b"first-last"
    assert response.headers["content-type"] == "audio/webm"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith("inline;")
    download = http.get(f"/recruiter/session/{sid}/audio?download=true")
    assert download.headers["content-disposition"].startswith("attachment;")
    partial = http.get(f"/recruiter/session/{sid}/audio", headers={"Range": "bytes=0-4"})
    assert partial.status_code == 206
    assert partial.content == b"first"


def test_lost_interview_still_finalizes_recording_as_partial(client):
    http, store, sid, token = client
    with http.websocket_connect(f"/ws/recording/{sid}") as websocket:
        start(websocket, token)
        websocket.send_bytes(b"received before disconnect")
        websocket.send_json({"type": "finish", "complete": False})
        assert websocket.receive_json()["status"] == "interrupted"
    assert store.details(sid)["available"]


def test_abrupt_recording_disconnect_keeps_received_bytes(client):
    http, store, sid, token = client
    with http.websocket_connect(f"/ws/recording/{sid}") as websocket:
        start(websocket, token)
        websocket.send_bytes(b"partial")
        websocket.close()
    # The server runs the recording's finalizer when it consumes disconnect.
    # Querying through the same ASGI portal lets pending tasks finish first.
    assert http.get(f"/recruiter/session/{sid}/audio").content == b"partial"


def test_bad_token_is_rejected_without_writing_audio(client):
    http, store, sid, token = client
    with http.websocket_connect(f"/ws/recording/{sid}") as websocket:
        websocket.send_json({"type": "start", "token": "incorrect", "mime_type": "audio/webm"})
        assert websocket.receive_json()["type"] == "recording_error"
    assert not store.details(sid)["available"]
    assert http.get(f"/recruiter/session/{sid}/audio").status_code == 404


def test_empty_recording_is_not_offered_for_playback(client):
    http, store, sid, token = client
    with http.websocket_connect(f"/ws/recording/{sid}") as websocket:
        start(websocket, token)
        websocket.send_json({"type": "finish", "complete": True})
        assert websocket.receive_json()["status"] == "empty"
    assert not store.details(sid)["available"]
