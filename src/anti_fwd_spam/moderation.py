"""Process provenance matches and permission-checked spam reports."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .app import AppResponse
from .evidence import EvidenceError, ReportStore
from .telegram import DeleteOutcome, Fetch, TelegramError, call_method, delete_message

if TYPE_CHECKING:
    from .policy import Config

ADMIN_STATUSES = frozenset({"creator", "administrator"})
MEMBER_STATUSES = ADMIN_STATUSES | {"member", "restricted", "left", "kicked"}
MUTE_PERMISSIONS = dict.fromkeys(
    [
        "can_send_messages",
        "can_send_audios",
        "can_send_documents",
        "can_send_photos",
        "can_send_videos",
        "can_send_video_notes",
        "can_send_voice_notes",
        "can_send_polls",
        "can_send_other_messages",
        "can_add_web_page_previews",
        "can_change_info",
        "can_invite_users",
        "can_pin_messages",
        "can_manage_topics",
    ],
    False,
)


def user_id(message: dict[str, object]) -> int | None:
    """Return a real sender, never the fake user attached to sender_chat."""
    if "sender_chat" in message:
        return None
    sender = message.get("from")
    if not isinstance(sender, dict) or sender.get("is_bot") is not False:
        return None
    identifier = sender.get("id")
    return identifier if type(identifier) is int and identifier > 0 else None


def mention_username(text: object, entity: dict[str, object]) -> str | None:
    """Read the account username, never a text mention's display name."""
    if entity.get("type") == "text_mention":
        account = entity.get("user")
        username = account.get("username") if isinstance(account, dict) else None
        return username if isinstance(username, str) else None
    if entity.get("type") != "mention" or not isinstance(text, str):
        return None
    offset, length = entity.get("offset"), entity.get("length")
    if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
        return None
    try:
        encoded = text.encode("utf-16-le")
        if 2 * (offset + length) > len(encoded):
            return None
        mention = encoded[2 * offset : 2 * (offset + length)].decode("utf-16-le")
    except UnicodeError:
        return None
    return mention[1:] if mention.startswith("@") else None


def mentions_bot(message: dict[str, object], bot_username: str) -> bool:
    """Compare usernames from Telegram mention entities, ignoring case."""
    for text_field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        entities = message.get(entity_field)
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            username = mention_username(message.get(text_field), entity)
            if username is not None and username.casefold() == bot_username.casefold():
                return True
    return False


class Moderator:
    """Orchestrate evidence, deletion and history-preserving restrictions."""

    def __init__(self, config: Config, fetcher: Fetch, store: ReportStore, bot_username: str) -> None:
        """Bind one request's configuration and capabilities."""
        self.config, self.fetcher, self.store = config, fetcher, store
        self.bot_username = bot_username

    async def call(self, method: str, parameters: dict[str, object]) -> object:
        """Call Telegram through the existing Workers transport."""
        return await call_method(self.fetcher, self.config.bot_token, method, parameters)

    async def member_status(self, chat_id: int, identifier: int) -> str:
        """Reject malformed or mismatched membership responses before acting."""
        result = await self.call("getChatMember", {"chat_id": chat_id, "user_id": identifier})
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("status"), str)
            or result["status"] not in MEMBER_STATUSES
            or not isinstance(result.get("user"), dict)
            or type(result["user"].get("id")) is not int
            or result["user"]["id"] != identifier
        ):
            raise TelegramError(retryable=True)
        return result["status"]

    async def process(self, update: dict[str, object], automatic: tuple[int, int] | None, raw_json: str) -> AppResponse:
        """Keep automatic moderation independent from report storage failures."""
        message = update.get("message", update.get("edited_message"))
        if not isinstance(message, dict) or message["chat"]["type"] not in {"group", "supergroup"}:
            return AppResponse(200, "ignored")
        if automatic is not None:
            return await self.moderate(message)
        target = message.get("reply_to_message")
        if not isinstance(target, dict) or user_id(message) is None:
            return AppResponse(200, "ignored")
        if not mentions_bot(message, self.bot_username):
            return AppResponse(200, "ignored")
        try:
            return await self.report(update, message, target, raw_json)
        except EvidenceError:
            return AppResponse(503, "report storage unavailable; retry pending")

    async def report(
        self,
        update: dict[str, Any],
        message: dict[str, Any],
        target: dict[str, Any],
        raw_json: str,
    ) -> AppResponse:
        """Save evidence before checking reporter authority or moderating."""
        bot_id = int(self.config.bot_token.split(":", 1)[0])
        chat_id = message["chat"]["id"]
        reporter_id = user_id(message)
        if (
            reporter_id is None
            or type(chat_id) is not int
            or chat_id >= 0
            or not isinstance(target.get("chat"), dict)
            or target["chat"].get("id") != chat_id
            or target["chat"].get("type") != message["chat"]["type"]
            or type(target.get("message_id")) is not int
            or target["message_id"] <= 0
            or type(update.get("update_id")) is not int
            or update["update_id"] < 0
        ):
            return AppResponse(400, "invalid report target")
        update_id = update["update_id"]
        completed = await self.store.save(bot_id, update_id, raw_json, target, int(time.time()))
        if completed is not None:
            return AppResponse(*completed)
        try:
            status = await self.member_status(chat_id, reporter_id)
            response = await self.moderate(target) if status in ADMIN_STATUSES else AppResponse(200, "report recorded")
        except TelegramError as error:
            response = AppResponse(503 if error.retryable else 200, "report recorded; authority check failed")
        await self.store.finish(bot_id, update_id, response.status, response.body)
        return response

    async def moderate(self, message: dict[str, Any]) -> AppResponse:
        """Delete only this message; never ban or revoke historical messages."""
        chat_id = message["chat"]["id"]
        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message["message_id"])
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, "deletion rejected; mute skipped")
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, "deletion pending retry; mute skipped")
        deletion = "deleted" if outcome is DeleteOutcome.DELETED else "already absent"
        identifier = user_id(message)
        if identifier is None or message["chat"]["type"] != "supergroup":
            return AppResponse(200, deletion + "; mute skipped")
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES or status == "kicked":
                mute = "mute skipped"
            else:
                result = await self.call(
                    "restrictChatMember",
                    {
                        "chat_id": chat_id,
                        "user_id": identifier,
                        "permissions": MUTE_PERMISSIONS,
                        "use_independent_chat_permissions": True,
                        "until_date": 0,
                    },
                )
                if result is not True:
                    return AppResponse(503, deletion + "; mute failed")
                mute = "muted"
        except TelegramError as error:
            return AppResponse(503 if error.retryable else 200, deletion + "; mute failed")
        return AppResponse(200, deletion + "; " + mute)
