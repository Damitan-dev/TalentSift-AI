"""Schedule replies from committed audio, independently of transcript latency."""

import asyncio
import json
import os
import time
from uuid import uuid4


def build_turn_detection():
    """Semantic VAD handles thinking pauses; the app still owns response creation."""
    if os.getenv("TALENTSIFT_VAD_MODE", "semantic_vad") == "server_vad":
        return {
            "type": "server_vad", "threshold": 0.5,
            "prefix_padding_ms": 300, "silence_duration_ms": 1500,
            "create_response": False, "interrupt_response": True,
        }
    eagerness = os.getenv("TALENTSIFT_VAD_EAGERNESS", "medium")
    if eagerness not in ("low", "medium", "high", "auto"):
        eagerness = "medium"
    return {
        "type": "semantic_vad", "eagerness": eagerness,
        "create_response": False, "interrupt_response": True,
    }


class InterviewTurns:
    def __init__(self, engine, allow_reply, log=print, clock=time.monotonic,
                 candidate_response=None):
        self.engine = engine
        self.allow_reply = allow_reply
        self.log = log
        self.clock = clock
        self.candidate_response = candidate_response or (lambda: {})
        self.speaking = False
        self.current_item = None
        self.epoch = 0
        self.epochs = {}
        self.stopped_at = {}
        self.committed_items = set()
        self.replied_epoch = 0
        self.pending_candidate = None
        self.pending_control = None
        self.awaiting_response = False
        self.request_kind = None
        self.request_event_id = None
        self.request_time = None
        self.request_interrupted = False
        self.cancel_on_created = False
        self.active_id = None
        self.active_kind = None
        self.kinds = {}
        self.request_times = {}
        self.first_audio_seen = set()
        self.suppressed = set()
        self.closed = False

    async def send(self, event):
        await self.engine.send(json.dumps(event))

    async def speech_started(self, item_id):
        self.epoch += 1
        self.current_item = item_id
        if item_id:
            self.epochs[item_id] = self.epoch
        self.speaking = True
        # Realtime automatically cancels an active response with
        # interrupt_response=True. Also reject its already-generated tail.
        if self.active_id and self.active_kind == "candidate":
            self.suppressed.add(self.active_id)
        if self.awaiting_response and self.request_kind == "candidate":
            self.request_interrupted = True

    async def speech_stopped(self, item_id):
        self.stopped_at[item_id] = self.clock()
        if item_id == self.current_item:
            self.speaking = False
        await self.maybe_reply()

    async def audio_committed(self, item_id):
        if not item_id or item_id in self.committed_items:
            return
        self.committed_items.add(item_id)
        epoch = self.epochs.get(item_id)
        # Only VAD turns observed on this interview connection can request a
        # reply. A stale/unknown commit must not answer an unrelated turn.
        if epoch is None or epoch <= self.replied_epoch:
            return
        if self.pending_candidate is None or epoch > self.pending_candidate[0]:
            self.pending_candidate = (epoch, item_id)
        await self.maybe_reply()

    async def control_response(self, kind, response=None, interrupt=False):
        """All responses share one slot; normal tool continuations also wait for speech."""
        if self.closed:
            return
        if kind != "candidate":
            self.pending_candidate = None
        self.pending_control = (kind, response or {})
        if interrupt and self.active_id:
            self.suppressed.add(self.active_id)
            await self.send({"type": "response.cancel", "response_id": self.active_id})
        elif interrupt and self.awaiting_response:
            self.cancel_on_created = True
        await self.maybe_reply()

    async def maybe_reply(self):
        if self.closed or self.awaiting_response or self.active_id:
            return
        if self.pending_control:
            kind, response = self.pending_control
            if kind == "candidate":
                if self.speaking or not self.allow_reply():
                    return
                if self.epoch > self.replied_epoch:
                    # A tool result may be ready while the candidate resumes.
                    # Wait for that new audio, then send ONE continuation.
                    if not self.pending_candidate or self.pending_candidate[0] != self.epoch:
                        return
                    self.replied_epoch = self.epoch
                    self.pending_candidate = None
            self.pending_control = None
            await self.request(kind, response)
            return
        if self.speaking or not self.allow_reply() or not self.pending_candidate:
            return
        epoch, item_id = self.pending_candidate
        # A candidate can resume before an older commit/transcript arrives.
        # Wait for the newest speaking segment to stop AND be committed.
        if epoch != self.epoch or epoch <= self.replied_epoch:
            return
        self.pending_candidate = None
        self.replied_epoch = epoch
        stopped = self.stopped_at.get(item_id)
        if stopped is not None:
            self.log(f"[turn] vad_stop_to_request_ms={round((self.clock() - stopped) * 1000)} item={item_id}")
        await self.request("candidate", {})

    async def request(self, kind, response):
        self.awaiting_response = True  # Set before yielding to the socket.
        self.request_kind = kind
        self.request_event_id = "talentsift_" + uuid4().hex
        self.request_time = self.clock()
        self.request_interrupted = False
        payload = dict(self.candidate_response() if kind == "candidate" else {})
        payload.update(response)
        payload["metadata"] = {**payload.get("metadata", {}), "talentsift_kind": kind}
        try:
            await self.send({
                "type": "response.create", "event_id": self.request_event_id,
                "response": payload,
            })
        except BaseException:
            self.awaiting_response = False
            raise

    async def response_created(self, response):
        response_id = response.get("id")
        kind = (response.get("metadata") or {}).get("talentsift_kind") or self.request_kind or "candidate"
        self.awaiting_response = False
        self.active_id = response_id
        self.active_kind = kind
        self.kinds[response_id] = kind
        if self.request_time is not None:
            self.request_times[response_id] = self.request_time
        reject = self.cancel_on_created or (
            kind == "candidate" and
            (self.request_interrupted or self.speaking or not self.allow_reply())
        )
        self.request_interrupted = False
        self.cancel_on_created = False
        if reject:
            self.suppressed.add(response_id)
            await self.send({"type": "response.cancel", "response_id": response_id})
            return False
        return True

    async def response_done(self, response):
        if response.get("id") == self.active_id:
            self.active_id = None
            self.active_kind = None
        await self.maybe_reply()

    def response_allowed(self, event):
        response_id = event.get("response_id") or self.active_id
        if self.closed or response_id in self.suppressed:
            return False
        if self.speaking and self.kinds.get(response_id) == "candidate":
            return False
        return True

    def allow_audio(self, event):
        if not self.response_allowed(event):
            return False
        response_id = event.get("response_id") or self.active_id
        if response_id not in self.first_audio_seen:
            self.first_audio_seen.add(response_id)
            requested = self.request_times.get(response_id)
            if requested is not None:
                self.log(f"[turn] request_to_first_audio_ms={round((self.clock() - requested) * 1000)} response={response_id}")
        return True

    def response_error(self, error):
        if error.get("event_id") == self.request_event_id:
            self.awaiting_response = False
            self.log(f"[turn] response_request_failed code={error.get('code', 'unknown')}")

    def close(self):
        self.closed = True
        self.pending_candidate = None
        self.pending_control = None


class FinalTranscripts:
    """Persist turns in audio order even when candidate ASR finishes late."""
    def __init__(self, save):
        self.save = save
        self.entries = {}
        self.finished = set()
        self.failed = set()
        self.drained = asyncio.Event()
        self.drained.set()

    def expect(self, item_id):
        if item_id and item_id not in self.finished:
            self.entries.setdefault(item_id, [False, None])
            self.drained.clear()

    def complete(self, item_id, turn=None, failed=False):
        if not item_id or item_id in self.finished:
            return
        self.expect(item_id)
        self.entries[item_id] = [True, turn]
        if failed:
            self.failed.add(item_id)
        while self.entries:
            first_id = next(iter(self.entries))
            ready, first_turn = self.entries[first_id]
            if not ready:
                break
            if first_turn is not None:
                self.save(first_turn)
            del self.entries[first_id]
            self.finished.add(first_id)
        if not self.entries:
            self.drained.set()

    async def wait_for_scoring(self, timeout=15):
        try:
            await asyncio.wait_for(self.drained.wait(), timeout)
        except TimeoutError as error:
            raise TimeoutError(
                "Final transcripts are still missing; automatic scoring was skipped."
            ) from error
        if self.failed:
            raise RuntimeError("Candidate transcription failed; automatic scoring was skipped.")
