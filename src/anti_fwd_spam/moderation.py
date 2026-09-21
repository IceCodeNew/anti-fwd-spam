"""Route provenance matches, authorized reports and content checks to moderation actions."""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from typing import TYPE_CHECKING

from .actions import Actions, AppResponse, BanTarget, user_id
from .evidence import MESSAGE_WINDOW_SECONDS, EvidenceError, ReportStore
from .model import MODEL_CONTENT_FIELDS, model_input
from .policy import MAX_TELEGRAM_ID, matches_spam_pattern, source_ids
from .sources import resolve_source, source_argument
from .tasks import ModelTasks
from .telegram import DeleteOutcome, Fetch, TelegramError, call_method, delete_message

if TYPE_CHECKING:
    from .model import ModelConfig
    from .policy import Config


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


class Moderator:
    """Select moderation policies after validating updates and reporter authority."""

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
        self.actions = Actions(fetcher, config.bot_token, store)

    async def process(self, content_type: str | None, body: bytes) -> AppResponse:
        """Parse an authenticated update and apply its moderation policy."""
        if content_type is None or content_type.partition(";")[0].strip().lower() != "application/json":
            return AppResponse(415, "expected application/json")
        try:
            raw_json = body.decode("utf-8")
            update = json.loads(raw_json)
            sources = source_ids(update)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return AppResponse(400, "invalid update")
        message = update.get("message", update.get("edited_message"))
        if isinstance(message, dict) and (argument := source_argument(message, self.bot_username)) is not None:
            if "message" not in update or not reporter_allowed(message, self.config):
                return AppResponse(200, "command ignored")
            try:
                response = await self.source_command(update, message, raw_json, argument)
            except EvidenceError:
                response = AppResponse(503, "command storage unavailable; retry pending")
            except TelegramError as error:
                response = AppResponse(503 if error.retryable else 200, "command failed")
            return response
        try:
            matches = await self.store.matching_sources(sources) if sources else ()
        except EvidenceError:
            return AppResponse(503, "source list unavailable; retry pending")
        return await self.process_update(update, raw_json, sources=matches)

    # Distinct route outcomes keep storage failures separate from Telegram failures.
    async def process_update(  # noqa: PLR0911
        self, update: dict[str, object], raw_json: str, *, sources: tuple[int, ...]
    ) -> AppResponse:
        """Route a validated update through account, source, report and content policies."""
        message = update.get("message", update.get("edited_message"))
        if not isinstance(message, dict) or message["chat"]["type"] not in {"group", "supergroup"}:
            return AppResponse(200, "ignored")
        if "edited_message" in update:
            try:
                await self.check_content(message, edited=True)
            except EvidenceError:
                return AppResponse(503, "model task storage unavailable; retry pending")
        account_response = await self.check_account_blacklist(update, message, raw_json)
        if sources:
            return await self.moderate_sources(update, message, raw_json, sources, account_response)
        if account_response is not None:
            return account_response
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
            return await self.check_content(message) if "message" in update else AppResponse(200, "ignored")
        except EvidenceError:
            return AppResponse(503, "model task storage unavailable; retry pending")

    async def moderate_sources(
        self,
        update: dict[str, object],
        message: dict[str, object],
        raw_json: str,
        sources: tuple[int, ...],
        account_response: AppResponse | None,
    ) -> AppResponse:
        """Moderate the sender and each matched source with independent retry progress."""
        response = account_response if account_response is not None else await self.actions.delete_and_mute(message)
        for identifier in sources:
            if account_response is not None and identifier == user_id(message, include_bots=True):
                continue
            try:
                source_response = await self.moderate_account(
                    update, message, raw_json, identifier, subject_id=identifier
                )
            except EvidenceError:
                source_response = AppResponse(503, "source moderation storage unavailable; retry pending")
            if source_response.status != HTTPStatus.OK:
                response = source_response
        return response

    async def source_command(
        self, update: dict[str, object], message: dict[str, object], raw_json: str, username: str
    ) -> AppResponse:
        """Save a resolved source, reply, and remove the command from group chats."""
        chat, message_id, update_id = message.get("chat"), message.get("message_id"), update.get("update_id")
        if (
            not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or not 0 < abs(chat["id"]) <= MAX_TELEGRAM_ID
            or type(message_id) is not int
            or not 0 < message_id <= MAX_TELEGRAM_ID
            or type(update_id) is not int
            or update_id < 0
        ):
            return AppResponse(400, "invalid command")
        bot_id = self.actions.bot_id
        identifier = await self.store.command_source(bot_id, update_id)
        if identifier is None:
            identifier = await resolve_source(self.fetcher, self.config.bot_token, username)
        reply = "Could not resolve the account. Use /bs username or /bs @username with one valid Telegram username."
        if identifier is not None:
            await self.store.save((bot_id, update_id), raw_json, message, int(time.time()))
            await self.store.pin_command_source(bot_id, update_id, identifier)
            # Concurrent deliveries must use the first resolution stored for this update.
            identifier = await self.store.command_source(bot_id, update_id)
            if identifier is None:
                raise EvidenceError
            await self.store.add_source(identifier)
            reply = f"Source saved in D1: @{username}\nID: {identifier}"
        await call_method(
            self.fetcher,
            self.config.bot_token,
            "sendMessage",
            {
                "chat_id": chat["id"],
                "text": reply,
                "reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True},
            },
        )
        if chat.get("type") in {"group", "supergroup"}:
            outcome = await delete_message(self.fetcher, self.config.bot_token, chat["id"], message_id)
            if outcome is DeleteOutcome.RETRYABLE_FAILURE:
                return AppResponse(503, "command handled; command cleanup pending retry")
            if outcome is DeleteOutcome.PERMANENT_FAILURE:
                return AppResponse(200, "command handled; command cleanup rejected; check deletion permissions")
        return AppResponse(200, "command handled")

    async def check_content(self, message: dict[str, object], *, edited: bool = False) -> AppResponse:
        """Apply local patterns before inference, or cancel pending inference on edits."""
        chat, message_id = message.get("chat"), message.get("message_id")
        if not isinstance(chat, dict) or type(chat.get("id")) is not int or type(message_id) is not int:
            return AppResponse(400, "invalid content moderation target")
        sent_at, now = message.get("date"), int(time.time())
        if type(sent_at) is not int or not now - MESSAGE_WINDOW_SECONDS < sent_at <= now:
            return AppResponse(200, "ignored")
        tasks = ModelTasks(self.store, self.actions.bot_id)
        if edited:
            await tasks.enqueue(chat["id"], message_id, sent_at, None, now)
            return AppResponse(200, "model task cancelled")
        if matches_spam_pattern(message):
            return await self.actions.delete_and_mute(message)
        if not self.models or user_id(message) is None or not MODEL_CONTENT_FIELDS.intersection(message):
            return AppResponse(200, "ignored")
        state = await model_input(self.fetcher, self.config.bot_token, message)
        payload: dict[str, object] = {
            "state": state,
            "target": {
                "chat": {"id": chat["id"], "type": chat["type"]},
                "message_id": message_id,
                "from": {"id": user_id(message), "is_bot": False},
            },
        }
        now = int(time.time())
        if await tasks.enqueue(chat["id"], message_id, sent_at, payload, now):
            task = await tasks.claim(now, (chat["id"], message_id))
            if task is not None:
                await tasks.run(task, self.fetcher, self.config.bot_token, self.models, now)
        return AppResponse(200, "model task recorded")

    async def index_message(self, message: dict[str, object]) -> None:
        """Index eligible account messages, including bots, within the deletion window."""
        identifier, sent_at, now = user_id(message, include_bots=True), message.get("date"), int(time.time())
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
            await self.store.remember_message(self.actions.bot_id, chat["id"], message_id, identifier, sent_at)

    async def check_account_blacklist(
        self, update: dict[str, object], message: dict[str, object], raw_json: str
    ) -> AppResponse | None:
        """Ban confirmed accounts and delete indexed messages without single-message fallbacks."""
        identifier, chat = user_id(message, include_bots=True), message.get("chat")
        if (
            "message" not in update
            or identifier is None
            or not isinstance(chat, dict)
            or chat.get("type") != "supergroup"
        ):
            return None
        try:
            if await self.store.is_blacklisted(self.actions.bot_id, identifier):
                await self.index_message(message)
                return await self.moderate_account(update, message, raw_json, identifier)
        except EvidenceError:
            return AppResponse(503, "account moderation storage unavailable; retry pending")
        return None

    async def moderate_account(
        self,
        update: dict[str, object],
        message: dict[str, object],
        raw_json: str,
        identifier: int,
        *,
        subject_id: int = 0,
    ) -> AppResponse:
        """Route a message-triggered account ban."""
        chat, message_id, update_id = message.get("chat"), message.get("message_id"), update.get("update_id")
        if (
            not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or type(message_id) is not int
            or type(update_id) is not int
            or update_id < 0
        ):
            return AppResponse(400, "invalid account update")
        return await self.actions.delete_history_and_ban(
            BanTarget(chat["id"], identifier, message_id + 1),
            update,
            raw_json,
            subject_id=subject_id,
        )

    async def report(
        self, update: dict[str, object], message: dict[str, object], target: dict[str, object], raw_json: str
    ) -> AppResponse:
        """Validate the target of an accepted report and select history cleanup."""
        chat, target_chat = message.get("chat"), target.get("chat")
        target_id, message_id, update_id = target.get("message_id"), message.get("message_id"), update.get("update_id")
        if (
            not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or chat["id"] >= 0
            or not isinstance(target_chat, dict)
            or target_chat.get("id") != chat["id"]
            or target_chat.get("type") != chat["type"]
            or type(target_id) is not int
            or target_id <= 0
            or type(message_id) is not int
            or type(update_id) is not int
            or update_id < 0
        ):
            return AppResponse(400, "invalid report target")
        return await self.actions.delete_history_and_ban(
            BanTarget(chat["id"], user_id(target) if chat["type"] == "supergroup" else None, message_id, target_id),
            update,
            raw_json,
        )
