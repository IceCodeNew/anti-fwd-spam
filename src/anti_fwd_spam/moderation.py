"""Process provenance matches and permission-checked spam reports."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from .evidence import DELETE_BATCH_SIZE, MESSAGE_WINDOW_SECONDS, EvidenceError, ReportStore
from .model import MODEL_CONTENT_FIELDS, model_input
from .policy import matches_update
from .tasks import ModelTasks
from .telegram import DeleteOutcome, Fetch, TelegramError, call_method, delete_message

if TYPE_CHECKING:
    from .model import ModelConfig
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


def reporter_allowed(message: dict[str, object], config: Config) -> bool:
    """Match the sender's identity against its configured allowlist."""
    sender_chat = message.get("sender_chat")
    if isinstance(sender_chat, dict):
        identifier = sender_chat.get("id")
        return type(identifier) is int and identifier in config.reporter_ids
    return user_id(message) in config.reporter_ids


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
    sender_banned: bool = False


class Moderator:
    """Apply automatic restrictions and administrator-authorized bans."""

    def __init__(
        self,
        config: Config,
        fetcher: Fetch,
        store: ReportStore,
        bot_username: str,
        models: tuple[ModelConfig, ...] = (),
    ) -> None:
        """Bind one request's configuration and capabilities."""
        self.config, self.fetcher, self.store = config, fetcher, store
        self.bot_username = bot_username
        self.models = models

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

    async def process(self, content_type: str | None, body: bytes) -> AppResponse:
        """Parse an authenticated update and apply its moderation policy."""
        if content_type is None or content_type.partition(";")[0].strip().lower() != "application/json":
            return AppResponse(415, "expected application/json")
        try:
            raw_json = body.decode("utf-8")
            update = json.loads(raw_json)
            automatic = matches_update(update, self.config)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return AppResponse(400, "invalid update")
        return await self.process_update(update, raw_json, automatic=automatic)

    # Distinct route outcomes keep storage failures separate from Telegram failures.
    async def process_update(  # noqa: PLR0911
        self,
        update: dict[str, object],
        raw_json: str,
        *,
        automatic: bool,
    ) -> AppResponse:
        """Route a validated update through account, source, report and model policies."""
        message = update.get("message", update.get("edited_message"))
        if not isinstance(message, dict) or message["chat"]["type"] not in {"group", "supergroup"}:
            return AppResponse(200, "ignored")
        if "edited_message" in update:
            try:
                await self.check_model(message, edited=True)
            except EvidenceError:
                return AppResponse(503, "model task storage unavailable; retry pending")
        account_response = await self.check_account_blacklist(update, message, raw_json)
        if account_response is not None:
            return account_response
        if automatic:
            return await self.moderate(message)
        try:
            await self.index_message(message)
        except EvidenceError:
            return AppResponse(503, "message index unavailable; retry pending")
        target = message.get("reply_to_message")
        if isinstance(target, dict) and mentions_bot(message, self.bot_username):
            try:
                return (
                    await self.report(update, message, target, raw_json)
                    if reporter_allowed(message, self.config)
                    else AppResponse(200, "report ignored; reporter not allowed")
                )
            except EvidenceError:
                return AppResponse(503, "report storage unavailable; retry pending")
        try:
            return await self.check_model(message) if "message" in update else AppResponse(200, "ignored")
        except EvidenceError:
            return AppResponse(503, "model task storage unavailable; retry pending")

    async def check_model(self, message: dict[str, object], *, edited: bool = False) -> AppResponse:
        """Persist one inference task or cancel the old version on an edit."""
        chat, message_id = message.get("chat"), message.get("message_id")
        if not isinstance(chat, dict) or type(chat.get("id")) is not int or type(message_id) is not int:
            return AppResponse(400, "invalid model moderation target")
        sent_at, now = message.get("date"), int(time.time())
        if type(sent_at) is not int or not now - MESSAGE_WINDOW_SECONDS < sent_at <= now:
            return AppResponse(200, "ignored")
        tasks = ModelTasks(self.store, int(self.config.bot_token.split(":", 1)[0]))
        if edited:
            await tasks.enqueue(chat["id"], message_id, sent_at, None, now)
            return AppResponse(200, "model task cancelled")
        if not self.models or user_id(message) is None or not MODEL_CONTENT_FIELDS.intersection(message):
            return AppResponse(200, "ignored")
        state = await model_input(self.fetcher, self.config.bot_token, message)
        now = int(time.time())
        if await tasks.enqueue(chat["id"], message_id, sent_at, state, now):
            task = await tasks.claim(now, (chat["id"], message_id))
            if task is not None:
                await tasks.run(task, self.fetcher, self.config.bot_token, self.models, now)
        return AppResponse(200, "model task recorded")

    async def index_message(self, message: dict[str, object]) -> None:
        """Index only eligible user messages, sharing the same cutoff for both moderation paths."""
        identifier, sent_at, now = user_id(message), message.get("date"), int(time.time())
        chat, message_id = message.get("chat"), message.get("message_id")
        if (
            isinstance(chat, dict)
            and chat.get("type") == "supergroup"
            and type(chat.get("id")) is int
            and type(message_id) is int
            and identifier is not None
            and type(sent_at) is int
            and now - MESSAGE_WINDOW_SECONDS < sent_at <= now
            and not {"supergroup_chat_created", "channel_chat_created", "forum_topic_created"}.intersection(message)
        ):
            await self.store.remember_message(
                int(self.config.bot_token.split(":", 1)[0]),
                chat["id"],
                message_id,
                identifier,
                sent_at,
            )

    async def check_account_blacklist(
        self,
        update: dict[str, object],
        message: dict[str, object],
        raw_json: str,
    ) -> AppResponse | None:
        """Ban confirmed accounts and delete indexed messages without single-message fallbacks."""
        identifier = user_id(message)
        chat = message.get("chat")
        if (
            "message" not in update
            or identifier is None
            or not isinstance(chat, dict)
            or chat.get("type") != "supergroup"
        ):
            return None
        try:
            bot_id = int(self.config.bot_token.split(":", 1)[0])
            if await self.store.is_blacklisted(bot_id, identifier):
                return await self.moderate_account(update, message, raw_json, bot_id, identifier)
        except EvidenceError:
            return AppResponse(503, "account moderation storage unavailable; retry pending")
        return None

    async def moderate_account(
        self,
        update: dict[str, object],
        message: dict[str, object],
        raw_json: str,
        bot_id: int,
        identifier: int,
    ) -> AppResponse:
        """Resume a message-triggered ban and its bounded indexed cleanup."""
        chat, message_id, update_id = message.get("chat"), message.get("message_id"), update.get("update_id")
        if (
            not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or type(message_id) is not int
            or type(update_id) is not int
            or update_id < 0
        ):
            return AppResponse(400, "invalid account update")
        await self.index_message(message)
        saved = await self.store.save(bot_id, update_id, raw_json, message, int(time.time()))
        if saved.status is not None:
            return AppResponse(saved.status, saved.body)
        if saved.moderation_result == "banned":
            response = AppResponse(200, "banned", sender_banned=True)
        elif saved.ban_claimed:
            return AppResponse(200, "ban confirmation failed; check membership")
        else:
            response = await self.ban(chat["id"], identifier, (bot_id, update_id))
            if response is None:
                response = AppResponse(200, "account moderation skipped; protected administrator")
        if response.sender_banned:
            response = await self.remove_recent_history(bot_id, chat["id"], identifier, message_id + 1, response)
        await self.store.finish(bot_id, update_id, response.status, response.body)
        return response

    async def report(
        self,
        update: dict[str, Any],
        message: dict[str, Any],
        target: dict[str, Any],
        raw_json: str,
    ) -> AppResponse:
        """Save an accepted report before checking group authority or moderating."""
        bot_id = int(self.config.bot_token.split(":", 1)[0])
        chat_id = message["chat"]["id"]
        reporter_id = user_id(message)
        if (
            type(chat_id) is not int
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
            response = AppResponse(
                200,
                saved.moderation_result,
                target_removed=True,
                sender_banned=saved.moderation_result == "already absent; banned",
            )
        elif saved.ban_claimed:
            # A lost response may hide a successful ban followed by an administrator's unban.
            return AppResponse(200, "ban confirmation failed; check membership and submit a new report if needed")
        else:
            try:
                # The report route accepts a missing user ID only for an allowlisted sender_chat.
                authorized = reporter_id is None or await self.member_status(chat_id, reporter_id) in ADMIN_STATUSES
                response = (
                    await self.moderate(target, report_key=(bot_id, update_id))
                    if authorized
                    else AppResponse(200, "report recorded")
                )
            except TelegramError as error:
                response = AppResponse(503 if error.retryable else 200, "report recorded; authority check failed")
        if response.target_removed:
            await self.store.remember_moderation(bot_id, update_id, response.body)
        identifier = user_id(target)
        if identifier is not None and response.sender_banned:
            await self.store.blacklist_user(bot_id, identifier, int(time.time()))
            if response.status == HTTPStatus.OK:
                response = await self.remove_recent_history(
                    bot_id,
                    chat_id,
                    identifier,
                    message["message_id"],
                    response,
                )
        response = await self.remove_report_message(chat_id, message, response)
        await self.store.finish(bot_id, update_id, response.status, response.body)
        return response

    async def remove_banned_target(self, chat_id: int, message_id: int) -> AppResponse:
        """Confirm target removal separately from a ban or history-revocation request."""
        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message_id)
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, "deletion rejected; banned", sender_banned=True)
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, "deletion pending retry; banned", sender_banned=True)
        deletion = "deleted" if outcome is DeleteOutcome.DELETED else "already absent"
        return AppResponse(200, f"{deletion}; banned", target_removed=True, sender_banned=True)

    async def remove_recent_history(
        self,
        bot_id: int,
        chat_id: int,
        sender_id: int,
        before_message_id: int,
        response: AppResponse,
    ) -> AppResponse:
        """Resume one bounded batch without extending cleanup beyond the original report."""
        message_ids = await self.store.recent_messages(
            bot_id,
            chat_id,
            sender_id,
            before_message_id,
            int(time.time()),
        )
        if not message_ids:
            return response
        batch = message_ids[:DELETE_BATCH_SIZE]
        try:
            if await self.member_status(chat_id, sender_id) in ADMIN_STATUSES:
                return AppResponse(200, response.body + "; history cleanup skipped", response.target_removed)
            result = await self.call("deleteMessages", {"chat_id": chat_id, "message_ids": batch})
            if result is not True:
                return AppResponse(503, response.body + "; history cleanup pending retry")
        except TelegramError as error:
            return AppResponse(
                503 if error.retryable else 200,
                response.body
                + ("; history cleanup pending retry" if error.retryable else "; history cleanup rejected"),
            )
        await self.store.forget_messages(bot_id, chat_id, batch)
        if len(message_ids) > DELETE_BATCH_SIZE:
            return AppResponse(503, response.body + "; history cleanup pending retry")
        return AppResponse(200, response.body + "; recent history cleared", response.target_removed)

    async def remove_report_message(
        self,
        chat_id: int,
        message: dict[str, Any],
        response: AppResponse,
    ) -> AppResponse:
        """Delete the reporter's message once its target finished moderation."""
        if response.status != HTTPStatus.OK or not response.target_removed:
            return response
        message_id = message["message_id"]
        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message_id)
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, response.body + "; report cleanup pending")
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, response.body + "; report cleanup rejected")
        return AppResponse(200, response.body + "; report removed")

    async def moderate(
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
            response = await self.ban(chat_id, identifier, report_key)
            if response is not None:
                return (
                    await self.remove_banned_target(chat_id, message["message_id"])
                    if response.sender_banned
                    else response
                )

        outcome = await delete_message(self.fetcher, self.config.bot_token, chat_id, message["message_id"])
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, f"deletion rejected; {action} skipped")
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, f"deletion pending retry; {action} skipped")
        deletion = "deleted" if outcome is DeleteOutcome.DELETED else "already absent"
        if ban or not restrictable:
            return AppResponse(200, f"{deletion}; {action} skipped", target_removed=True)
        try:
            return await self.mute(chat_id, message["message_id"], identifier, deletion)
        except EvidenceError:
            return AppResponse(503, "mute storage unavailable; retry pending")

    async def ban(self, chat_id: int, identifier: int, report_key: tuple[int, int]) -> AppResponse | None:
        """Persist one ban attempt, keeping target deletion separate from Telegram's ban side effects."""
        claimed = False
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES:
                return None
            if status != "kicked":
                claimed = await self.store.claim_ban(*report_key)
                if not claimed:
                    return AppResponse(503, "ban already attempted; retry pending")
                result = await self.call("banChatMember", {"chat_id": chat_id, "user_id": identifier, "until_date": 0})
                if result is not True:
                    return AppResponse(503, "ban failed")
            await self.store.remember_moderation(*report_key, "banned")
        except TelegramError as error:
            if claimed and error.rejected:
                await self.store.release_ban(*report_key)
            return AppResponse(503 if error.retryable else 200, "ban failed")
        return AppResponse(200, "banned", sender_banned=True)

    async def mute(self, chat_id: int, message_id: int, identifier: int, deletion: str) -> AppResponse:
        """Do not repeat a possibly successful restriction after an administrator's unmute."""
        mute_key = (int(self.config.bot_token.split(":", 1)[0]), chat_id, message_id)
        claimed = False
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES or status == "kicked":
                return AppResponse(200, deletion + "; mute skipped")
            claimed = await self.store.claim_mute(*mute_key, int(time.time()))
            if not claimed:
                return AppResponse(200, deletion + "; mute retry skipped; check permissions if needed")
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
            # A lost response can hide a successful mute followed by an administrator's unmute.
            if claimed and error.rejected and error.retryable:
                await self.store.release_mute(*mute_key)
            return AppResponse(503 if error.retryable else 200, deletion + "; mute failed")
        return AppResponse(200, deletion + "; muted")
