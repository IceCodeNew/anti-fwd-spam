"""Parse authenticated Telegram updates independently of the Workers SDK."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .policy import Config, extract_deletion_target


@dataclass(frozen=True, slots=True)
class AppResponse:
    """A minimal HTTP response returned to the Worker entrypoint."""

    status: int
    body: str


ProcessUpdate = Callable[[dict[str, object], tuple[int, int] | None, str], Awaitable[AppResponse]]


async def handle_update(
    *,
    content_type: str | None,
    body: bytes,
    config: Config,
    process_update: ProcessUpdate,
) -> AppResponse:
    """Parse one authenticated, size-bounded body and await moderation."""
    if content_type is None or content_type.partition(";")[0].strip().lower() != "application/json":
        return AppResponse(415, "expected application/json")

    try:
        raw_json = body.decode("utf-8")
        update = json.loads(raw_json)
        target = extract_deletion_target(update, config)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return AppResponse(400, "invalid update")

    return await process_update(update, target, raw_json)
