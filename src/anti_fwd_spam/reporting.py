"""Authorize reporting plugins before they can resolve accounts or change state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .actions import AppResponse, user_id
from .evidence import EvidenceError
from .telegram import TelegramError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .policy import TelegramUpdate


@dataclass(frozen=True, slots=True)
class ReportingPlugin:
    """A side-effect-free recognizer and an authorized reporting handler."""

    name: str
    matches: Callable[[TelegramUpdate], bool]
    handle: Callable[[TelegramUpdate], Awaitable[AppResponse]]


class Reporting:
    """Apply the same identity check on every plugin dispatch, including redelivery."""

    def __init__(self, reporter_ids: frozenset[int]) -> None:
        """Bind the runtime reporter allowlist."""
        self.reporter_ids = reporter_ids

    async def dispatch(self, plugins: tuple[ReportingPlugin, ...], update: TelegramUpdate) -> AppResponse | None:
        """Run the first matching plugin after authorization.

        Unmatched and unauthorized updates return None, so automatic moderation still applies to them.
        """
        plugin = next((plugin for plugin in plugins if plugin.matches(update)), None)
        if plugin is None:
            return None
        sender_chat = update.message.get("sender_chat")
        identifier = sender_chat.get("id") if isinstance(sender_chat, dict) else user_id(update.message)
        if type(identifier) is not int or identifier not in self.reporter_ids:
            return None
        try:
            return await plugin.handle(update)
        except EvidenceError:
            return AppResponse(503, f"{plugin.name} storage unavailable; retry pending")
        except TelegramError as error:
            return AppResponse(503 if error.retryable else 200, f"{plugin.name} failed")
