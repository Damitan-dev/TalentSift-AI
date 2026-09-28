"""Incremental, consented browser audio recordings, kept outside static files."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
from uuid import UUID

from config import DATA_DIR

FORMATS = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a"}
MAX_CHUNK_BYTES = 1024 * 1024
MAX_RECORDING_BYTES = 32 * 1024 * 1024


class RecordingError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_metadata(directory: Path, metadata: dict) -> None:
    temporary = directory / "metadata.tmp"
    temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    temporary.replace(directory / "metadata.json")


class RecordingStore:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else DATA_DIR / "recordings"

    def directory(self, session_id: str) -> Path:
        try:
            if str(UUID(session_id)) != session_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RecordingError("Invalid recording session.") from None
        return self.root / session_id

    def prepare(self, session_id: str) -> str:
        """Called only when the candidate explicitly chooses audio storage."""
        directory = self.directory(session_id)
        try:
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            # Consent/microphone setup may be reloaded before any audio starts.
            # Replace only an unused upload credential; never reset saved audio.
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            if (directory / "started.lock").exists() or metadata.get("status") != "not_started":
                raise RecordingError("Recording already started for this interview.") from None
        token = secrets.token_urlsafe(32)
        write_metadata(directory, {
            "session_id": session_id,
            "consent_version": "audio-recording-v1",
            "consented_at": now(),
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "status": "not_started",
            "bytes": 0,
        })
        return token

    def begin(self, session_id: str, token: str, mime_type: str):
        directory = self.directory(session_id)
        try:
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RecordingError("Audio recording was not enabled for this interview.") from None
        if not isinstance(token, str) or not secrets.compare_digest(
            metadata.get("token_hash", ""), hashlib.sha256(token.encode()).hexdigest()
        ):
            raise RecordingError("Recording authorization failed.")
        mime_type = mime_type.split(";", 1)[0].strip().lower() if isinstance(mime_type, str) else ""
        if mime_type not in FORMATS:
            raise RecordingError("Unsupported audio format.")
        # An exclusive, permanent marker prevents concurrent writers and replay
        # of a used token, including across Uvicorn workers. Never overwrite audio.
        try:
            with (directory / "started.lock").open("xb"):
                pass
        except FileExistsError:
            raise RecordingError("A recording already started for this interview.") from None
        path = directory / ("audio" + FORMATS[mime_type])
        stream = path.open("xb")
        try:
            metadata.update(status="recording", mime_type=mime_type, started_at=now())
            write_metadata(directory, metadata)
        except BaseException:
            stream.close()
            raise
        return RecordingWriter(directory, metadata, stream)

    def details(self, session_id: str) -> dict:
        directory = self.directory(session_id)
        try:
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"available": False, "status": "not_requested"}
        extension = FORMATS.get(metadata.get("mime_type"))
        path = directory / ("audio" + extension) if extension else None
        size = path.stat().st_size if path and path.is_file() else 0
        # Never return the upload token hash to a template or download client.
        return {
            "available": size > 0,
            "status": metadata["status"],
            "mime_type": metadata.get("mime_type"),
            "bytes": size,
            "size_mb": round(size / (1024 * 1024), 1),
            "path": path,
            "consented_at": metadata.get("consented_at"),
        }


class RecordingWriter:
    def __init__(self, directory, metadata, stream):
        self.directory = directory
        self.metadata = metadata
        self.stream = stream
        self.size = 0
        self.closed = False
        self.lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self.lock:
            self._append(chunk)

    def _append(self, chunk: bytes) -> None:
        if self.closed:
            raise RecordingError("Recording is already closed.")
        if len(chunk) > MAX_CHUNK_BYTES or self.size + len(chunk) > MAX_RECORDING_BYTES:
            raise RecordingError("Audio recording reached its storage limit.")
        self.stream.write(chunk)
        self.stream.flush()  # Received chunks survive an ordinary process restart.
        self.size += len(chunk)

    def finish(self, complete: bool = False) -> dict:
        # Serialize against a disk write still finishing during task shutdown.
        with self.lock:
            return self._finish(complete)

    def _finish(self, complete: bool) -> dict:
        if self.closed:
            return self.metadata
        try:
            self.stream.flush()
            os.fsync(self.stream.fileno())
        finally:
            self.stream.close()
            self.closed = True
        self.metadata.update(
            status=("saved" if complete else "interrupted") if self.size else "empty",
            bytes=self.size,
            ended_at=now(),
        )
        write_metadata(self.directory, self.metadata)
        return self.metadata


recording_store = RecordingStore()
