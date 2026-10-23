"""Validate configuration and match Telegram provenance and spam patterns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .constants import (
    GROUP_CHAT_TYPES,
    MAX_CONFIG_LENGTH,
    MAX_ID_ENTRIES,
    MAX_TELEGRAM_ID,
    SUPPORTED_CHAT_TYPES,
    SUPPORTED_FORWARD_ORIGINS,
    TOKEN_PART_COUNT,
)
from .rules import AUTHORED_SPAM_PATTERNS, REPEATED_SYMBOLS_RE, SPAM_PATTERNS


def matches_spam_pattern(message: dict[str, object]) -> bool:
    """Check current visible fields without inspecting replies."""
    texts = _pattern_texts(message)
    if any(matches_spam_text(text) for text in texts):
        return True
    markup = message.get("reply_markup")
    keyboard = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if isinstance(keyboard, list):
        texts.extend(
            label
            for row in keyboard
            if isinstance(row, list)
            for button in row
            if isinstance(button, dict)
            if isinstance(label := button.get("text"), str)
        )
    if REPEATED_SYMBOLS_RE.search("\n".join(texts)):
        return True
    return any(
        isinstance(text := message.get(field), str) and any(pattern.search(text) for pattern in AUTHORED_SPAM_PATTERNS)
        for field in ("text", "caption")
    )


def matches_spam_text(text: object) -> bool:
    """Search one profile string with patterns that are safe outside the current message."""
    return isinstance(text, str) and any(pattern.search(text) is not None for pattern in SPAM_PATTERNS)


def display_name(account: dict[str, object]) -> str:
    """Join the first and last name of a Telegram user or shared contact."""
    return " ".join(name for field in ("first_name", "last_name") if isinstance(name := account.get(field), str))


def _pattern_texts(message: dict[str, object]) -> list[str]:
    texts = [text for field in ("text", "caption") if isinstance(text := message.get(field), str)]
    accounts = (message.get("contact"), message.get("from"))
    texts.extend(display_name(account) for account in accounts if isinstance(account, dict))
    return texts


def normalize_sticker_set(value: object) -> str | None:
    """Return an ASCII sticker set name in lowercase, or None for unusable metadata."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None or "__" in value:
        return None
    return value.lower()


def sticker_set_name(message: dict[str, object]) -> str | None:
    """Read only the current sticker's set name, without inspecting replies."""
    sticker = message.get("sticker")
    return normalize_sticker_set(sticker.get("set_name")) if isinstance(sticker, dict) else None


def sticker_unique_id(message: dict[str, object]) -> str | None:
    """Read the current sticker's case-sensitive identity, not its file or thumbnail ID."""
    sticker = message.get("sticker")
    value = sticker.get("file_unique_id") if isinstance(sticker, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None else None


def reply_target(message: dict[str, object]) -> dict[str, object] | None:
    """Return an explicit reply, not the topic root that Telegram attaches to forum topic messages."""
    target = message.get("reply_to_message")
    return target if isinstance(target, dict) and "forum_topic_created" not in target else None


class ConfigError(ValueError):
    """The Worker configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated immutable Worker configuration."""

    bot_token: str
    webhook_secret: str
    reporter_ids: frozenset[int]

    @classmethod
    def from_values(
        cls,
        *,
        bot_token: object,
        webhook_secret: object,
        reporter_ids: object = "",
    ) -> Config:
        """Validate raw environment values and construct a configuration."""
        token = _required_string("BOT_TOKEN", bot_token, maximum=128)
        token_parts = token.split(":", maxsplit=1)
        if (
            len(token_parts) != TOKEN_PART_COUNT
            or not token_parts[0].isascii()
            or not token_parts[0].isdecimal()
            or not token_parts[1]
            or not all(
                character.isascii() and (character.isalnum() or character in "_-") for character in token_parts[1]
            )
        ):
            message = "BOT_TOKEN must have Telegram's numeric-id:secret form"
            raise ConfigError(message)
        try:
            _telegram_id(int(token_parts[0]), allow_negative=False)
        except ValueError as error:
            message = "BOT_TOKEN contains an invalid bot ID"
            raise ConfigError(message) from error

        secret = _required_string("TELEGRAM_WEBHOOK_SECRET", webhook_secret, maximum=256)
        if not all(character.isascii() and (character.isalnum() or character in "_-") for character in secret):
            message = "TELEGRAM_WEBHOOK_SECRET contains unsupported characters"
            raise ConfigError(message)

        return cls(
            token,
            secret,
            _parse_reporter_ids(reporter_ids),
        )


def _message_sources(message: dict[str, object]) -> frozenset[int]:
    sources: set[int] = set()
    sender = message.get("from")
    if "sender_chat" not in message and isinstance(sender, dict) and sender.get("is_bot") is True:
        identifier = _bot_id(sender, require_bot=True)
        if identifier is not None:
            sources.add(identifier)
    via_bot = message.get("via_bot")
    if via_bot is not None:
        identifier = _bot_id(via_bot, require_bot=True)
        if identifier is not None:
            sources.add(identifier)

    origin = message.get("forward_origin")
    if origin is None:
        return frozenset(sources)
    if not isinstance(origin, dict):
        error_detail = "message.forward_origin is invalid"
        raise TypeError(error_detail)
    origin_type = origin.get("type")
    if not isinstance(origin_type, str) or origin_type not in SUPPORTED_FORWARD_ORIGINS:
        error_detail = "message.forward_origin is invalid"
        raise ValueError(error_detail)
    if origin_type == "user":
        identifier = _bot_id(origin.get("sender_user"), require_bot=False)
        if identifier is not None:
            sources.add(identifier)
    return frozenset(sources)


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    """A validated message update with its raw JSON and explicit bot provenance."""

    raw_json: str
    update_id: int | None
    message: dict[str, object]
    chat_id: int
    chat_type: str
    message_id: int
    sent_at: int | None
    edited: bool
    sources: frozenset[int]

    @property
    def in_group(self) -> bool:
        """Return True for basic groups and supergroups."""
        return self.chat_type in GROUP_CHAT_TYPES


def parse_update(raw_json: str) -> TelegramUpdate | None:
    """Validate every field that routing consumes; return None for updates without a message."""
    update = json.loads(raw_json)
    if not isinstance(update, dict):
        message = "update must be an object"
        raise TypeError(message)
    if "message" in update and "edited_message" in update:
        message = "update must contain at most one supported message"
        raise ValueError(message)
    edited = "edited_message" in update
    if not edited and "message" not in update:
        return None
    raw_message = update["edited_message" if edited else "message"]
    if not isinstance(raw_message, dict):
        message = "message must be an object"
        raise TypeError(message)
    chat = raw_message.get("chat")
    if not isinstance(chat, dict):
        message = "message.chat must be an object"
        raise TypeError(message)
    chat_type = chat.get("type")
    if not isinstance(chat_type, str) or chat_type not in SUPPORTED_CHAT_TYPES:
        message = "message.chat.type is invalid"
        raise ValueError(message)
    update_id = update.get("update_id")
    if update_id is not None and (type(update_id) is not int or update_id < 0):
        message = "update.update_id is invalid"
        raise ValueError(message)
    _validate_join(raw_message, chat_type, update_id)
    sources = _message_sources(raw_message) if chat_type in GROUP_CHAT_TYPES else frozenset()
    if sources and update_id is None:
        message = "update.update_id is required for a source match"
        raise ValueError(message)
    sent_at = raw_message.get("date")
    return TelegramUpdate(
        raw_json=raw_json,
        update_id=update_id,
        message=raw_message,
        chat_id=_telegram_id(chat.get("id"), allow_negative=True),
        chat_type=chat_type,
        message_id=_telegram_id(raw_message.get("message_id"), allow_negative=False),
        sent_at=sent_at if type(sent_at) is int else None,
        edited=edited,
        sources=sources,
    )


def _validate_join(message: dict[str, object], chat_type: str, update_id: int | None) -> None:
    if "new_chat_members" not in message:
        return
    members = message["new_chat_members"]
    if not isinstance(members, list) or not members or chat_type not in GROUP_CHAT_TYPES or update_id is None:
        detail = "message.new_chat_members is invalid or missing its update ID"
        raise ValueError(detail)
    for member in members:
        _bot_id(member, require_bot=False)
        if not isinstance(member.get("first_name"), str) or not member["first_name"]:
            detail = "new member name is invalid"
            raise ValueError(detail)


def _required_string(name: str, value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        message = f"{name} must contain 1 to {maximum} characters"
        raise ConfigError(message)
    return value


def _parse_reporter_ids(value: object) -> frozenset[int]:
    if not isinstance(value, str) or len(value) > MAX_CONFIG_LENGTH:
        message = f"REPORTER_IDS must be a string no longer than {MAX_CONFIG_LENGTH} characters"
        raise ConfigError(message)
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if len(entries) > MAX_ID_ENTRIES:
        message = f"REPORTER_IDS supports at most {MAX_ID_ENTRIES} entries"
        raise ConfigError(message)
    parsed: set[int] = set()
    for entry in entries:
        digits = entry.removeprefix("-")
        if not digits.isascii() or not digits.isdecimal():
            message = "REPORTER_IDS entries must be decimal integers"
            raise ConfigError(message)
        try:
            parsed.add(_telegram_id(int(entry), allow_negative=True))
        except ValueError as error:
            message = "REPORTER_IDS entry is outside Telegram's range"
            raise ConfigError(message) from error
    return frozenset(parsed)


def _telegram_id(value: object, *, allow_negative: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        message = "Telegram identifier must be an integer"
        raise TypeError(message)
    if value == 0 or abs(value) > MAX_TELEGRAM_ID or (value < 0 and not allow_negative):
        message = "Telegram identifier is outside the supported range"
        raise ValueError(message)
    return value


def _bot_id(value: object, *, require_bot: bool) -> int | None:
    if not isinstance(value, dict):
        message = "Telegram user must be an object"
        raise TypeError(message)
    identifier = _telegram_id(value.get("id"), allow_negative=False)
    is_bot = value.get("is_bot")
    if not isinstance(is_bot, bool) or (require_bot and not is_bot):
        message = "Telegram user has an invalid is_bot field"
        raise ValueError(message)
    return identifier if is_bot else None
