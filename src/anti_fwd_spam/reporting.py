"""Authorize reporting plugins before they can resolve accounts or change state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .actions import AppResponse, user_id
from .evidence import EvidenceError
from .telegram import TelegramError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True, slots=True)
class ReportingPlugin:
    """A side-effect-free recognizer and an authorized reporting handler."""

    name: str
    matches: Callable[[dict[str, object]], bool]
    handle: Callable[[dict[str, object], dict[str, object], str], Awaitable[AppResponse]]


class Reporting:
    """Apply the same identity check on every plugin dispatch, including redelivery."""

    def __init__(self, reporter_ids: frozenset[int]) -> None:
        """Bind the runtime reporter allowlist."""
        self.reporter_ids = reporter_ids

    async def dispatch(
        self, plugins: tuple[ReportingPlugin, ...], update: dict[str, object], raw_json: str
    ) -> AppResponse | None:
        """Run the first matching plugin after authorization.

        Unmatched and unauthorized updates return None, so automatic moderation still applies to them.
        """
        message = update.get("message", update.get("edited_message"))
        if not isinstance(message, dict):
            return None
        for plugin in plugins:
            if not plugin.matches(message):
                continue
            sender_chat = message.get("sender_chat")
            identifier = sender_chat.get("id") if isinstance(sender_chat, dict) else user_id(message)
            if type(identifier) is not int or identifier not in self.reporter_ids:
                return None
            try:
                return await plugin.handle(update, message, raw_json)
            except EvidenceError:
                return AppResponse(503, f"{plugin.name} storage unavailable; retry pending")
            except TelegramError as error:
                return AppResponse(503 if error.retryable else 200, f"{plugin.name} failed")
        return None
