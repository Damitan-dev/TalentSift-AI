"""Check an OpenAI connection without starting a TalentSift interview.

Uses the same model, handshake timeout and keepalive as the interview relay.
Sends no audio, conversation items or response requests, and does not import
the app, create database records or consume TalentSift invitation quotas.
"""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import platform
import re
import time
from urllib.request import getproxies

from dotenv import load_dotenv
import websockets

from relay_lifecycle import connection_failure_details


ENGINE_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
SETUP_TIMEOUT = 12
PONG_TIMEOUT = 60


class ProviderError(Exception):
    def __init__(self, error):
        self.code = str(error.get("code") or error.get("type") or "unknown")
        super().__init__(str(error.get("message") or "OpenAI rejected the session."))


def safe_text(value, secret):
    text = str(value)
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [redacted]", text)
    text = re.sub(r"((?:https?|socks[45]h?)://)[^/\s@]+@", r"\1[redacted]@", text)
    return text[:500]


def safe_details(value, secret):
    if isinstance(value, dict):
        return {key: safe_details(item, secret) for key, item in value.items()}
    if isinstance(value, str):
        return safe_text(value, secret)
    return value


def event_from(raw):
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("OpenAI sent an invalid event.")
    if event.get("type") == "error":
        error = event.get("error")
        raise ProviderError(error if isinstance(error, dict) else {})
    return event


async def check_connection(api_key, seconds=120, *, emit=print,
                           connector=None, clock=time.monotonic):
    """Return 0 only when a ready connection survives the observation interval."""
    connector = connector or websockets.connect
    started_at = clock()
    connected_at = None
    ready_at = None
    last_event = None
    phase = "handshake"

    def report(result, **extra):
        now = clock()
        data = {"result": result, "phase": phase,
                "elapsed_ms": round((now - started_at) * 1000),
                "active_connection_ms": None if connected_at is None else round((now - connected_at) * 1000),
                "last_engine_event": last_event, **extra}
        emit("[connection-check] " + json.dumps(safe_details(data, api_key), ensure_ascii=False))

    proxies = getproxies()
    report("starting", python=platform.python_version(),
           websockets=websockets.__version__, model="gpt-realtime",
           observation_seconds=seconds,
           proxy_types=[kind for kind in ("http", "https", "ws", "wss", "socks", "all")
                        if proxies.get(kind)])
    try:
        async with connector(
            ENGINE_URL, additional_headers={"Authorization": f"Bearer {api_key}"},
            ping_interval=20, ping_timeout=60, open_timeout=12, close_timeout=3,
        ) as socket:
            connected_at = clock()
            phase = "session_setup"
            report("socket_connected")
            # Disable automatic turns even though this check never sends audio.
            await socket.send(json.dumps({
                "type": "session.update",
                "session": {"type": "realtime", "audio": {"input": {"turn_detection": None}}},
            }))
            async with asyncio.timeout(SETUP_TIMEOUT):
                while True:
                    event = event_from(await socket.recv())
                    last_event = event.get("type")
                    if last_event == "session.updated":
                        break
                    if last_event == "response.created":
                        raise RuntimeError("An unexpected response started; the check has stopped.")

            ready_at = clock()
            phase = "observing"
            report("session_ready")
            deadline = ready_at + seconds
            while (remaining := deadline - clock()) > 0:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=min(10, remaining))
                except TimeoutError:
                    latency = getattr(socket, "latency", None)
                    latency_ms = (round(latency * 1000, 1)
                                  if isinstance(latency, (int, float)) and math.isfinite(latency) and latency > 0
                                  else None)
                    report("still_connected", ping_latency_ms=latency_ms)
                    continue
                event = event_from(raw)
                last_event = event.get("type")
                if last_event == "response.created":
                    raise RuntimeError("An unexpected response started; the check has stopped.")
            # A quiet socket may still be half-open. Require a fresh pong rather
            # than counting the absence of received errors as a successful check.
            observed_ms = round((clock() - ready_at) * 1000)
            phase = "final_ping"
            pong = await socket.ping()
            ping_latency = await asyncio.wait_for(pong, timeout=PONG_TIMEOUT)
            phase = "closing"
        phase = "finished"
        report("stable", ready_connection_ms=observed_ms,
               final_ping_latency_ms=round(ping_latency * 1000, 1))
        return 0
    except Exception as error:
        extra = connection_failure_details(error)
        if isinstance(error, ProviderError):
            extra["provider_error_code"] = error.code
        response = getattr(error, "response", None)
        if response is not None:
            extra["http_status"] = getattr(response, "status_code", None)
        report("failed", **extra)
        return 1


def duration(value):
    seconds = int(value)
    if not 30 <= seconds <= 300:
        raise argparse.ArgumentTypeError("Choose 30 to 300 seconds.")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=duration, default=120,
                        help="Observe a ready connection for 30 to 300 seconds (default: 120).")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).with_name(".env"))
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print('[connection-check] {"result": "missing_api_key", "message": "Configure OPENAI_API_KEY in your local environment or .env file."}')
        return 2
    try:
        return asyncio.run(check_connection(api_key, args.seconds))
    except KeyboardInterrupt:
        print('[connection-check] {"result": "cancelled"}')
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
