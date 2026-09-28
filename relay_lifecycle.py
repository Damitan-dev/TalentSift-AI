"""Own the lifetime of both halves of a WebSocket relay."""

import asyncio


async def run_relay_pair(sender, receiver, *, name):
    """A stopped/failed half must never leave the other half orphaned."""
    tasks = (
        asyncio.create_task(sender, name=f"{name}:send"),
        asyncio.create_task(receiver, name=f"{name}:receive"),
    )
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Retrieve failures even if both tasks finish in the same event-loop turn.
        for task in tasks:
            if task in done:
                task.result()
    finally:
        # asyncio.wait() and gather() do not cancel siblings when a task fails.
        # This also runs when the parent itself is canceled (caption shutdown).
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def connection_failure_details(error):
    """Diagnostic metadata only: no microphone payloads or API headers."""
    details = {"exception": type(error).__name__, "message": str(error)[:500]}
    for attr, key in (("rcvd", "received_close"), ("sent", "sent_close")):
        frame = getattr(error, attr, None)
        details[key] = None if frame is None else {
            "code": frame.code, "reason": frame.reason[:200],
        }
    cause = error.__cause__
    if cause is not None:
        details["cause"] = {"exception": type(cause).__name__, "message": str(cause)[:500]}
    return details


def is_billing_failure(error):
    text = str(error).lower()
    return any(code in text for code in (
        "insufficient_quota", "credit_balance_exhausted",
        "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    ))
