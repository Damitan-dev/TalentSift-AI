import asyncio
import json
from unittest.mock import patch

from live_captions import LiveCaptionRelay


class Browser:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class LiveSocket:
    def __init__(self, events=()):
        self.events = list(events)
        self.sent = []
        self.two_sends = asyncio.Event()

    def __aiter__(self):
        async def iterate():
            for event in self.events:
                yield json.dumps(event)
        return iterate()

    async def send(self, message):
        self.sent.append(json.loads(message))
        if len(self.sent) == 2:
            self.two_sends.set()


def test_provisional_words_use_the_main_turn_id_and_final_wins():
    async def scenario():
        browser = Browser()
        relay = LiveCaptionRelay(browser, "unused", "en", enabled=False)
        relay.ready = True

        # A live fragment can arrive before the main VAD names the turn.
        await relay._receive(LiveSocket([{
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "live-1",
            "delta": "Yes",
        }]))
        assert browser.messages == []

        await relay.speech_started("main-1")
        assert browser.messages == [{
            "type": "transcript_delta",
            "speaker": "candidate",
            "text": "Yes",
            "item_id": "main-1",
        }]

        relay.final_transcript_arrived("main-1")
        await relay._receive(LiveSocket([{
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "live-1",
            "delta": ", please.",
        }]))
        assert len(browser.messages) == 1

    asyncio.run(scenario())


def test_live_audio_is_committed_without_changing_the_main_session():
    async def scenario():
        relay = LiveCaptionRelay(Browser(), "unused", "fr", enabled=False)
        relay.ready = True
        socket = LiveSocket()
        relay.offer_audio("base64-pcm")
        await relay.speech_started("main-2")
        relay.speech_stopped("main-2")
        sender = asyncio.create_task(relay._send(socket))
        try:
            await asyncio.wait_for(socket.two_sends.wait(), 1)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        assert socket.sent == [
            {"type": "input_audio_buffer.append", "audio": "base64-pcm"},
            {"type": "input_audio_buffer.commit"},
        ]

    asyncio.run(scenario())


def test_second_turn_waits_for_first_commit_id():
    async def scenario():
        browser = Browser()
        relay = LiveCaptionRelay(browser, "unused", "en", enabled=False)
        relay.ready = True
        await relay.speech_started("main-1")
        relay.offer_audio("first-turn-audio")
        relay.speech_stopped("main-1")
        await relay.speech_started("main-2")

        # Live turn 2 can emit text before the ack for live turn 1.
        await relay._receive(LiveSocket([{
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "live-2",
            "delta": "Second answer",
        }]))
        assert browser.messages == []

        socket = LiveSocket()
        sender = asyncio.create_task(relay._send(socket))
        try:
            await asyncio.wait_for(socket.two_sends.wait(), 1)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        await relay._receive(LiveSocket([{
            "type": "input_audio_buffer.committed",
            "item_id": "live-1",
        }]))
        assert browser.messages == [{
            "type": "transcript_delta",
            "speaker": "candidate",
            "text": "Second answer",
            "item_id": "main-2",
        }]

    asyncio.run(scenario())


def test_session_setup_streams_words_before_the_candidate_stops():
    class ConnectedSocket:
        def __init__(self):
            self.events = asyncio.Queue()
            self.sent = []

        async def __aenter__(self):
            await self.events.put({"type": "session.updated"})
            return self

        async def __aexit__(self, *exc):
            pass

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            return json.dumps(await self.events.get())

        def __aiter__(self):
            async def iterate():
                while True:
                    yield json.dumps(await self.events.get())
            return iterate()

    async def scenario():
        browser = Browser()
        upstream = ConnectedSocket()
        with patch.dict("os.environ", {"TALENTSIFT_CAPTION_DELAY": "medium"}):
            relay = LiveCaptionRelay(browser, "test-key", "en")
        with patch("live_captions.websockets.connect", return_value=upstream):
            relay.start()
            try:
                await asyncio.wait_for(_wait_until(lambda: relay.ready), 1)
                assert upstream.sent[0]["session"]["type"] == "transcription"
                assert upstream.sent[0]["session"]["audio"]["input"][
                    "turn_detection"
                ] is None
                assert upstream.sent[0]["session"]["audio"]["input"][
                    "transcription"
                ]["delay"] == "medium"
                await relay.speech_started("main-3")
                relay.offer_audio("base64-pcm")
                await upstream.events.put({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "live-3",
                    "delta": "I built",
                })
                await asyncio.wait_for(
                    _wait_until(lambda: bool(browser.messages)), 1
                )
                assert browser.messages[0]["item_id"] == "main-3"
                assert browser.messages[0]["text"] == "I built"
                # No speech_stopped or commit was needed to display words.
                assert not any(
                    sent["type"] == "input_audio_buffer.commit"
                    for sent in upstream.sent
                )
            finally:
                await relay.stop()

    asyncio.run(scenario())


async def _wait_until(predicate):
    while not predicate():
        await asyncio.sleep(0.001)
