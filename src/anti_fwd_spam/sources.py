"""Resolve authorized source-list commands and acknowledge durable additions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .policy import MAX_TELEGRAM_ID
from .telegram import TelegramError, call_method

if TYPE_CHECKING:
    from .telegram import Fetch


def source_argument(message: dict[str, object], bot_username: str) -> str | None:
    """Recognize a leading Telegram command addressed to this bot."""
    text, entities = message.get("text"), message.get("entities")
    if not isinstance(text, str) or not isinstance(entities, list):
        return None
    parts = text.split(maxsplit=1)
    if not parts or parts[0].casefold() not in {"/bs", f"/bs@{bot_username.casefold()}"}:
        return None
    if not any(
        isinstance(entity, dict)
        and entity.get("type") == "bot_command"
        and entity.get("offset") == 0
        and entity.get("length") == len(parts[0])
        for entity in entities
    ):
        return None
    return parts[1].strip().removeprefix("@") if len(parts) > 1 else ""


async def resolve_source(fetcher: Fetch, token: str, username: str) -> int | None:
    """Resolve a username without requiring the returned account to be a bot."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return None
    try:
        account = await call_method(fetcher, token, "getChat", {"chat_id": f"@{username}"})
    except TelegramError as error:
        if error.retryable:
            raise
        return None
    if not isinstance(account, dict) or account.get("type") not in ("private", "group", "supergroup", "channel"):
        raise TelegramError(retryable=True)
    names = account.get("active_usernames", [])
    if not isinstance(names, list):
        raise TelegramError(retryable=True)
    if not any(
        isinstance(name, str) and name.casefold() == username.casefold() for name in [account.get("username"), *names]
    ):
        raise TelegramError(retryable=True)
    identifier = account.get("id")
    if type(identifier) is not int or not 0 < abs(identifier) <= MAX_TELEGRAM_ID:
        raise TelegramError(retryable=True)
    if (account["type"] == "private") != (identifier > 0):
        raise TelegramError(retryable=True)
    return identifier
