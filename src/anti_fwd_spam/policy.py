"""Validate configuration and match Telegram bot provenance."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BLACKLIST_BOT_IDS = "273234066"
MAX_TELEGRAM_ID = (1 << 52) - 1
MAX_CONFIG_LENGTH = 4_096
MAX_ID_ENTRIES = 500
TOKEN_PART_COUNT = 2
SUPPORTED_CHAT_TYPES = frozenset({"group", "supergroup", "private", "channel"})
SUPPORTED_FORWARD_ORIGINS = frozenset({"user", "hidden_user", "chat", "channel"})


class ConfigError(ValueError):
    """The Worker configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated immutable Worker configuration."""

    bot_token: str
    webhook_secret: str
    bot_ids: frozenset[int]
    reporter_ids: frozenset[int] = frozenset()

    @classmethod
    def from_values(
        cls,
        *,
        bot_token: object,
        webhook_secret: object,
        bot_ids: object = DEFAULT_BLACKLIST_BOT_IDS,
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

        parsed_bot_ids = _parse_ids("BLACKLIST_BOT_IDS", bot_ids)
        if not parsed_bot_ids:
            message = "at least one blacklisted bot ID is required"
            raise ConfigError(message)
        return cls(
            token,
            secret,
            parsed_bot_ids,
            _parse_ids("REPORTER_IDS", reporter_ids, allow_negative=True),
        )


def _message_matches(message: dict[str, object], config: Config) -> bool:
    via_bot = message.get("via_bot")
    if via_bot is not None and _bot_id(via_bot, require_bot=True) in config.bot_ids:
        return True

    origin = message.get("forward_origin")
    if origin is None:
        return False
    if not isinstance(origin, dict):
        error_detail = "message.forward_origin is invalid"
        raise TypeError(error_detail)
    origin_type = origin.get("type")
    if not isinstance(origin_type, str) or origin_type not in SUPPORTED_FORWARD_ORIGINS:
        error_detail = "message.forward_origin is invalid"
        raise ValueError(error_detail)
    if origin_type != "user":
        return False
    return _bot_id(origin.get("sender_user"), require_bot=False) in config.bot_ids


def matches_update(update: object, config: Config) -> bool:
    """Validate consumed fields and match explicit bot provenance in groups."""
    if not isinstance(update, dict):
        message = "update must be an object"
        raise TypeError(message)

    has_message = "message" in update
    has_edited_message = "edited_message" in update
    if has_message and has_edited_message:
        message = "update must contain at most one supported message"
        raise ValueError(message)
    if not has_message and not has_edited_message:
        return False

    raw_message = update.get("message" if has_message else "edited_message")
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
    if chat_type not in {"group", "supergroup"}:
        return False

    _telegram_id(chat.get("id"), allow_negative=True)
    _telegram_id(raw_message.get("message_id"), allow_negative=False)
    return _message_matches(raw_message, config)


def _required_string(name: str, value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        message = f"{name} must contain 1 to {maximum} characters"
        raise ConfigError(message)
    return value


def _parse_ids(name: str, value: object, *, allow_negative: bool = False) -> frozenset[int]:
    if not isinstance(value, str) or len(value) > MAX_CONFIG_LENGTH:
        message = f"{name} must be a string no longer than {MAX_CONFIG_LENGTH} characters"
        raise ConfigError(message)
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if len(entries) > MAX_ID_ENTRIES:
        message = f"{name} supports at most {MAX_ID_ENTRIES} entries"
        raise ConfigError(message)
    parsed: set[int] = set()
    for entry in entries:
        digits = entry.removeprefix("-") if allow_negative else entry
        if not digits.isascii() or not digits.isdecimal():
            message = f"{name} entries must be decimal integers"
            raise ConfigError(message)
        try:
            parsed.add(_telegram_id(int(entry), allow_negative=allow_negative))
        except ValueError as error:
            message = f"{name} entry is outside Telegram's range"
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
