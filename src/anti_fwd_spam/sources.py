"""Resolve authorized source-list commands and acknowledge durable additions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .policy import MAX_TELEGRAM_ID, SUPPORTED_CHAT_TYPES, normalize_sticker_set, reply_target
from .telegram import TelegramError, call_method

if TYPE_CHECKING:
    from .telegram import Fetch


def command_argument(message: dict[str, object], bot_username: str, command: str) -> str | None:
    """Recognize a leading Telegram command addressed to this bot."""
    text, entities = message.get("text"), message.get("entities")
    if not isinstance(text, str) or not isinstance(entities, list):
        return None
    parts = text.split(maxsplit=1)
    if not parts or parts[0].casefold() not in {command, f"{command}@{bot_username.casefold()}"}:
        return None
    if not any(
        isinstance(entity, dict)
        and entity.get("type") == "bot_command"
        and entity.get("offset") == 0
        and entity.get("length") == len(parts[0])
        for entity in entities
    ):
        return None
    if len(parts) == 1:
        return ""
    return parts[1].strip()


def sticker_set_argument(argument: str) -> str | None:
    """Accept one complete HTTPS sticker-set link without resolving external content."""
    prefix = "https://t.me/addstickers/"
    return normalize_sticker_set(argument[len(prefix) :]) if argument.startswith(prefix) else None


def replied_source(message: dict[str, object]) -> int | None:
    """Read the inline source or actual bot sender, never a guest caller or fake chat sender."""
    target = reply_target(message)
    if target is None:
        return None
    source = target.get("via_bot")
    if source is None and "sender_chat" not in target:
        source = target.get("from")
    if not isinstance(source, dict) or source.get("is_bot") is not True:
        return None
    identifier = source.get("id")
    return identifier if type(identifier) is int and 0 < identifier <= MAX_TELEGRAM_ID else None


async def resolve_source(fetcher: Fetch, token: str, username: str, *, known_id: int | None = None) -> int | None:
    """Resolve a username without requiring the returned account to be a bot."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return None
    try:
        account = await call_method(
            fetcher, token, "getChat", {"chat_id": known_id if known_id is not None else f"@{username}"}
        )
    except TelegramError as error:
        if error.retryable:
            raise
        return None
    if not isinstance(account, dict) or account.get("type") not in SUPPORTED_CHAT_TYPES:
        raise TelegramError(retryable=True)
    known_identity = known_id is None or (account.get("id") == known_id and account.get("type") == "private")
    names = account.get("active_usernames", [])
    if not isinstance(names, list):
        raise TelegramError(retryable=True)
    if not known_identity or not any(
        isinstance(name, str) and name.casefold() == username.casefold() for name in [account.get("username"), *names]
    ):
        if known_id is not None:
            return None
        raise TelegramError(retryable=True)
    identifier = account.get("id")
    if type(identifier) is not int or not 0 < abs(identifier) <= MAX_TELEGRAM_ID:
        raise TelegramError(retryable=True)
    if (account["type"] == "private") != (identifier > 0):
        raise TelegramError(retryable=True)
    return identifier
