"""Provisional candidate captions alongside the authoritative interview stream.

This connection never controls Bianca or writes interview records. The existing
Realtime session remains responsible for turn detection, final transcription,
responses, and scoring.
"""

import asyncio
from collections import deque
import json
import os

import websockets
from relay_lifecycle import run_relay_pair
from transcription_config import build_transcription_settings, interview_language


LIVE_CAPTIONS_URL = os.getenv(
    "TALENTSIFT_LIVE_CAPTIONS_URL",
    "wss://api.openai.com/v1/realtime?intent=transcription",
)
CAPTION_DELAYS = {"minimal", "low", "medium", "high", "xhigh"}


class LiveCaptionRelay:
    def __init__(self, browser_ws, api_key: str, language: str, enabled=True,
                 *, candidate_name=None, job_title=None):
        self.browser_ws = browser_ws
        self.api_key = api_key
        self.language = interview_language(language)
        self.enabled = enabled
        configured_delay = os.getenv("TALENTSIFT_CAPTION_DELAY", "low").lower()
        self.delay = configured_delay if configured_delay in CAPTION_DELAYS else "low"
        self.transcription = build_transcription_settings(
            self.language, "gpt-live-transcribe", candidate_name=candidate_name,
            job_title=job_title, delay=self.delay,
        )
        self.queue = asyncio.Queue(maxsize=128)
        self.task = None
        self.ready = False
        self.current_primary_item = None
        self.primary_by_live_item = {}
        self.pending_by_live_item = {}
        self.commits_waiting_for_id = deque()
        self.pending_commits = 0
        self.finalized_primary_items = set()
        self.previewed_live_items = set()

    def start(self):
        if self.enabled:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.ready = False

    def offer_audio(self, audio_b64: str):
        """Do not let optional captions delay the interview's audio stream."""
        # Retain a bounded startup buffer while the optional socket configures.
        if not self.ready and (self.task is None or self.task.done()):
            return
        try:
            self.queue.put_nowait(("audio", audio_b64))
        except asyncio.QueueFull:
            print("⚠️ Live captions fell behind; using final transcript only")
            self.ready = False
            if self.task is not None:
                self.task.cancel()

    async def speech_started(self, primary_item_id: str | None):
        # The main interview VAD assigns the item ID used by its final text.
        self.current_primary_item = primary_item_id
        if primary_item_id and not self.pending_commits:
            await self._claim_pending(primary_item_id)

    def speech_stopped(self, primary_item_id: str | None):
        if self.current_primary_item == primary_item_id:
            self.current_primary_item = None
        if primary_item_id and (self.ready or (self.task and not self.task.done())):
            try:
                self.queue.put_nowait(("commit", primary_item_id))
                # A later speech start can precede the live socket's commit ack.
                self.pending_commits += 1
            except asyncio.QueueFull:
                print("⚠️ Live captions fell behind; using final transcript only")
                self.ready = False
                if self.task is not None:
                    self.task.cancel()

    def final_transcript_arrived(self, primary_item_id: str | None):
        if primary_item_id:
            # Late provisional fragments must not rewrite a finalized turn.
            self.finalized_primary_items.add(primary_item_id)

    async def _forward_delta(self, primary_item_id: str, text: str):
        if primary_item_id not in self.finalized_primary_items and text:
            await self.browser_ws.send_json({
                "type": "transcript_delta",
                "speaker": "candidate",
                "text": text,
                "item_id": primary_item_id,
            })

    async def _flush_pending(self, live_item_id: str):
        primary_id = self.primary_by_live_item.get(live_item_id)
        if primary_id:
            self.pending_by_live_item.pop(live_item_id, None)
            if live_item_id in self.previewed_live_items:
                await self.browser_ws.send_json({
                    "type": "transcript_link",
                    "provisional_item_id": "live:" + live_item_id,
                    "item_id": primary_id,
                })
                self.previewed_live_items.discard(live_item_id)

    async def _claim_pending(self, primary_item_id: str):
        for live_item_id in list(self.pending_by_live_item):
            self.primary_by_live_item[live_item_id] = primary_item_id
            await self._flush_pending(live_item_id)

    async def _receive(self, ws):
        async for raw in ws:
            event = json.loads(raw)
            kind = event.get("type")
            live_id = event.get("item_id")

            if kind == "input_audio_buffer.committed" and live_id:
                if self.commits_waiting_for_id:
                    primary_id = self.commits_waiting_for_id.popleft()
                    self.pending_commits -= 1
                    self.primary_by_live_item[live_id] = primary_id
                    await self._flush_pending(live_id)
                    if self.current_primary_item and not self.pending_commits:
                        await self._claim_pending(self.current_primary_item)

            elif kind == "conversation.item.input_audio_transcription.delta":
                text = event.get("delta", "")
                if not text or not live_id:
                    continue
                primary_id = self.primary_by_live_item.get(live_id)
                if (not primary_id and self.current_primary_item
                        and not self.pending_commits):
                    primary_id = self.current_primary_item
                    self.primary_by_live_item[live_id] = primary_id
                if primary_id:
                    await self._forward_delta(primary_id, text)
                else:
                    # Display immediately, then reconcile with the main VAD ID.
                    pending = self.pending_by_live_item.setdefault(live_id, [])
                    if sum(map(len, pending)) < 4000:
                        pending.append(text)
                        self.previewed_live_items.add(live_id)
                        await self._forward_delta("live:" + live_id, text)

            elif kind == "conversation.item.input_audio_transcription.completed":
                # The main interview stream supplies the final saved text.
                if live_id in self.primary_by_live_item:
                    self.pending_by_live_item.pop(live_id, None)

            elif kind == "error":
                detail = event.get("error", {})
                raise RuntimeError(
                    f"live transcription error: {detail.get('message', 'unknown')}"
                )

    async def _send(self, ws):
        has_audio = False
        while True:
            kind, value = await self.queue.get()
            if kind == "audio":
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": value,
                }))
                has_audio = True
            elif kind == "commit" and has_audio:
                self.commits_waiting_for_id.append(value)
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                has_audio = False
            elif kind == "commit":
                self.pending_commits -= 1
                if self.current_primary_item and not self.pending_commits:
                    await self._claim_pending(self.current_primary_item)

    async def _run(self):
        try:
            async with websockets.connect(
                LIVE_CAPTIONS_URL,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                ping_interval=20,
                ping_timeout=60,
                open_timeout=10,
                close_timeout=3,
            ) as ws:
                await ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "audio": {"input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "transcription": self.transcription,
                            "turn_detection": None,
                        }},
                    },
                }))

                # A socket being open does not prove the configuration worked.
                while True:
                    event = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=12)
                    )
                    if event.get("type") in (
                        "session.updated",
                        "transcription_session.updated",
                    ):
                        break
                    if event.get("type") == "error":
                        raise RuntimeError(
                            f"live transcription setup: {event.get('error', {})}"
                        )

                self.ready = True
                print("📝 Live candidate captions ready")
                await run_relay_pair(
                    self._send(ws), self._receive(ws), name="live-captions"
                )
                print("⚠️ Live caption stream closed; final transcripts remain enabled")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print("⚠️ Live captions unavailable; final transcripts remain:", error)
        finally:
            self.ready = False
            # If this optional socket fails before IDs are linked, its previews
            # must not remain beside the authoritative final transcript.
            for live_item_id in list(self.previewed_live_items):
                try:
                    await self.browser_ws.send_json({
                        "type": "transcript_retract", "item_id": "live:" + live_item_id,
                    })
                except Exception:
                    break  # The browser may already be disconnected.
            self.previewed_live_items.clear()
