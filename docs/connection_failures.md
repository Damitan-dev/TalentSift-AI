# Checking an unexpected interview disconnect

The recruiter page's connection-ended message describes the outcome, not the
cause. Read the complete `[relay] failed` line from the Uvicorn terminal first.

| Exception or code | What it establishes |
| --- | --- |
| `AudioRateError` / `audio_rate_exceeded` | TalentSift rejected excess microphone bytes before forwarding them. |
| `ConnectionClosedError` with no close frames | The underlying connection ended without a normal WebSocket shutdown. It does not identify which device or service caused it. |
| `TimeoutError` during an opening handshake | The connection did not complete within the configured startup timeout. |
| A provider error code such as `insufficient_quota` | The provider rejected a request for the stated reason. |
| `browser_disconnected: true` | The browser-to-Python receiver observed a disconnect. |

An initial handshake failure followed by a successful retry and abrupt closure
is a transport clue. The overall relay duration includes retries, configuration
and the opening, so it is not the lifetime of the successful socket. A 30-second
proxy cutoff is one possibility, not a conclusion. A phone hotspot is still a
network path; its use alone neither proves nor disproves a network failure.
Likewise, `Proxy types: none` means Python did not find a configured proxy; it
does not detect every intermediary or network interruption.

## Run the isolated connection check

Stop Uvicorn after the failed interview. In the project directory with the
virtual environment active, run:

```bash
python check_realtime_connection.py
```

The script loads the local `.env` file, respecting an existing `OPENAI_API_KEY`
environment variable. Keep the key in that environment or file. Do not pass it
as a command-line argument. The checker opens an authenticated OpenAI connection
using `gpt-realtime`, matching the interview model and handshake/keepalive
settings. It sends one configuration update with automatic turns disabled. It
sends no microphone audio, conversation items or response requests.

It does not import `app.py`, write candidate data, create an interview, consume
an invitation or change TalentSift's usage quotas. It does connect to the API;
normal provider authentication and service limits apply. It does not generate
interviewer speech or claim a guaranteed billing outcome.

After OpenAI accepts the configuration, it observes the connection for 120
seconds and prints progress about every ten seconds. It then requires a fresh
ping reply before reporting `stable`. The final check can wait up to 60 seconds
for that reply. The observation interval can be adjusted within a limit:

```bash
python check_realtime_connection.py --seconds 60
```

The check deliberately uses one startup attempt so a retry cannot hide its
failure. Ctrl+C cancels it and closes its connection.

## Interpret the result

| Final result | Next investigation |
| --- | --- |
| `stable` | The idle, single-socket path survived this interval and answered a ping. Investigate audio traffic, model requests, simultaneous caption/recording activity and app processing next. This does not prove speech streaming is healthy. |
| `failed`, phase `handshake` | Check initial API reachability, the handshake error, Python/library versions and the network path. |
| `failed`, phase `session_setup` | Inspect the provider error code or configuration-acknowledgement timeout. |
| `failed`, phase `observing` | Compare the exception, close metadata and timing with the interview. A matching abrupt closure reproduces that failure without the interview flow and narrows investigation to the shared Python/WebSocket/TLS/network/provider path. |
| `failed`, phase `final_ping` | A quiet connection did not answer a fresh ping; absence of messages was not a successful stability check. |
| `missing_api_key` | Set the key locally; the checker did not connect. |

Share the `[connection-check]` output, especially the final line. It includes
Python/library versions, phases, active connection time, last event type, close
metadata and provider error codes. It excludes API headers, audio and event
contents; known keys, bearer credentials and authenticated proxy URLs are
redacted from exception messages.

The requirements pin `websockets==17.1` and the test environment uses that
version. A user's 16.1 environment is a difference to track, not proof of the
reported failure. Make one diagnostic change at a time rather than changing
the package, network, model and timeouts together.

## Recovery and grading

The interview relay retries startup. It does not silently start a fresh model
session halfway through an interview. Recovery would need to preserve what
the candidate actually said and heard, unfinished turns, elapsed time, recording
state and invitation/usage accounting. A fresh socket alone cannot establish
that those were restored correctly. Failed interviews retain their actual
status; an interrupted connection must not be treated as poor candidate ability.

These checks are diagnostic, not a verified repair. Automated tests use mocked
sockets and clocks; a check on the affected computer is needed to localize this
reported disconnect.

Official references:

- https://websockets.readthedocs.io/en/17.1/faq/connection.html
- https://websockets.readthedocs.io/en/17.1/project/changelog.html
- https://developers.openai.com/api/docs/guides/realtime-websocket
