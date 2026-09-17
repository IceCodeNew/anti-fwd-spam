"""Process provenance matches and permission-checked spam reports."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from .evidence import EvidenceError, ReportStore
from .policy import matches_update
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


def sent_as_chat_itself(message: dict[str, object]) -> bool:
    """Recognize anonymous administrators, whom Telegram sends on behalf of the chat itself."""
    chat = message.get("chat")
    sender_chat = message.get("sender_chat")
    if not isinstance(chat, dict) or not isinstance(sender_chat, dict):
        return False
    chat_id, sender_id = chat.get("id"), sender_chat.get("id")
    return type(chat_id) is int and type(sender_id) is int and sender_id == chat_id


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


@dataclass(frozen=True, slots=True)
class AppResponse:
    """An HTTP outcome for webhook delivery and stored report completion."""

    status: int
    body: str
    target_removed: bool = False


class Moderator:
    """Apply automatic restrictions and administrator-authorized bans."""

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

    async def process(self, content_type: str | None, body: bytes) -> AppResponse:  # noqa: PLR0911
        """Parse an authenticated update and apply its moderation policy."""
        if content_type is None or content_type.partition(";")[0].strip().lower() != "application/json":
            return AppResponse(415, "expected application/json")
        try:
            raw_json = body.decode("utf-8")
            update = json.loads(raw_json)
            automatic = matches_update(update, self.config)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return AppResponse(400, "invalid update")
        message = update.get("message", update.get("edited_message"))
        if not isinstance(message, dict) or message["chat"]["type"] not in {"group", "supergroup"}:
            return AppResponse(200, "ignored")
        if automatic:
            return await self.moderate(message)
        target = message.get("reply_to_message")
        if not isinstance(target, dict) or (user_id(message) is None and not sent_as_chat_itself(message)):
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
        anonymous = reporter_id is None and sent_as_chat_itself(message)
        if (
            (reporter_id is None and not anonymous)
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
        saved = await self.store.save(bot_id, update_id, raw_json, target, int(time.time()))
        if saved.status is not None:
            return AppResponse(saved.status, saved.body)
        if saved.moderation_result in {"banned", "deleted; banned"} or (
            saved.moderation_result == "target removed before upgrade"
            and (saved.body.startswith("deleted; banned") or saved.body == "deletion pending retry; banned")
        ):
            # Older releases also inferred target deletion from a successful ban.
            response = await self.remove_banned_target(chat_id, target["message_id"])
        elif saved.moderation_result is not None:
            response = AppResponse(200, saved.moderation_result, target_removed=True)
        elif saved.ban_claimed:
            # A lost response may hide a successful ban followed by an administrator's unban.
            return AppResponse(200, "ban confirmation failed; check membership and submit a new report if needed")
        else:
            try:
                if reporter_id is not None:
                    authorized = await self.member_status(chat_id, reporter_id) in ADMIN_STATUSES
                else:
                    # Telegram reserves send-as-chat for this chat's own administrators.
                    authorized = True
                response = (
                    await self.moderate(target, report_key=(bot_id, update_id))
                    if authorized
                    else AppResponse(200, "report recorded")
                )
            except TelegramError as error:
                response = AppResponse(503 if error.retryable else 200, "report recorded; authority check failed")
        if response.target_removed:
            await self.store.remember_moderation(bot_id, update_id, response.body)
        response = await self.remove_report_message(chat_id, message, response)
        await self.store.finish(bot_id, update_id, response.status, response.body)
        return response

    async def remove_banned_target(self, chat_id: int, message_id: int) -> AppResponse:
        """Confirm target removal separately from a ban or history-revocation request."""
        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message_id)
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, "deletion rejected; banned")
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, "deletion pending retry; banned")
        deletion = "deleted" if outcome is DeleteOutcome.DELETED else "already absent"
        return AppResponse(200, f"{deletion}; banned", target_removed=True)

    async def remove_report_message(
        self,
        chat_id: int,
        message: dict[str, Any],
        response: AppResponse,
    ) -> AppResponse:
        """Delete the reporter's message once its target finished moderation."""
        if response.status != HTTPStatus.OK or not response.target_removed:
            return response
        message_id = message.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            return response
        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message_id)
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, response.body + "; report cleanup pending")
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, response.body + "; report cleanup rejected")
        return AppResponse(200, response.body + "; report removed")

    async def moderate(  # noqa: C901, PLR0911
        self,
        message: dict[str, Any],
        *,
        report_key: tuple[int, int] | None = None,
    ) -> AppResponse:
        """Preserve history for automatic matches; request revocation for reports."""
        ban = report_key is not None
        action = "ban" if ban else "mute"
        chat_id = message["chat"]["id"]
        identifier = user_id(message)
        restrictable = identifier is not None and message["chat"]["type"] == "supergroup"
        if report_key is not None and restrictable:
            claimed = False
            try:
                if await self.member_status(chat_id, identifier) not in ADMIN_STATUSES:
                    claimed = await self.store.claim_ban(*report_key)
                    if not claimed:
                        return AppResponse(503, "ban already attempted; retry pending")
                    result = await self.call(
                        "banChatMember",
                        {"chat_id": chat_id, "user_id": identifier, "until_date": 0, "revoke_messages": True},
                    )
                    if result is not True:
                        return AppResponse(503, "ban failed")
                    await self.store.remember_moderation(*report_key, "banned")
                    return await self.remove_banned_target(chat_id, message["message_id"])
            except TelegramError as error:
                if claimed and error.rejected:
                    await self.store.release_ban(*report_key)
                return AppResponse(503 if error.retryable else 200, "ban failed")

        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message["message_id"])
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, f"deletion rejected; {action} skipped")
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, f"deletion pending retry; {action} skipped")
        deletion = "deleted" if outcome is DeleteOutcome.DELETED else "already absent"
        if ban or not restrictable:
            return AppResponse(200, f"{deletion}; {action} skipped", target_removed=True)
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES or status == "kicked":
                return AppResponse(200, deletion + "; mute skipped")
            result = await self.call(
                "restrictChatMember",
                {
                    "chat_id": chat_id,
                    "user_id": identifier,
                    "until_date": 0,
                    "permissions": MUTE_PERMISSIONS,
                    "use_independent_chat_permissions": True,
                },
            )
            if result is not True:
                return AppResponse(503, deletion + "; mute failed")
        except TelegramError as error:
            return AppResponse(503 if error.retryable else 200, deletion + "; mute failed")
        return AppResponse(200, deletion + "; muted")
