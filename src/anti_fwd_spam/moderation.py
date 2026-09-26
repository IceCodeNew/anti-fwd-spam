"""Route provenance matches, authorized reports and content checks to moderation actions."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING

from .actions import Actions, AppResponse, BanTarget, user_id
from .evidence import MESSAGE_WINDOW_SECONDS, EvidenceError, ReportStore
from .model import MODEL_CONTENT_FIELDS, model_input
from .policy import matches_spam_pattern, parse_update, reply_target
from .reporting import Reporting, ReportingPlugin
from .sources import replied_source, resolve_source, source_argument
from .tasks import ModelTasks
from .telegram import DeleteOutcome, Fetch, TelegramError, call_method, delete_message

MODEL_TASK_STORAGE_FAILURE = "model task storage unavailable; retry pending"

if TYPE_CHECKING:
    from .model import ModelConfig
    from .policy import Config, TelegramUpdate


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


def reported_target(update: TelegramUpdate) -> BanTarget | None:
    """Validate a group-local reply before recording sources or punishing accounts."""
    target = reply_target(update.message)
    if target is None or not update.in_group or update.chat_id >= 0 or update.update_id is None:
        return None
    target_chat, target_id = target.get("chat"), target.get("message_id")
    if (
        not isinstance(target_chat, dict)
        or target_chat.get("id") != update.chat_id
        or target_chat.get("type") != update.chat_type
        or type(target_id) is not int
        or target_id <= 0
    ):
        return None
    return BanTarget(
        update.chat_id,
        user_id(target, include_bots=True) if update.chat_type == "supergroup" else None,
        update.message_id,
        target_id,
    )


def recent(sent_at: int, now: int) -> bool:
    """Return True while the bot can still delete and index the message."""
    return now - MESSAGE_WINDOW_SECONDS < sent_at <= now


class Moderator:
    """Select moderation policies after validating updates and reporter authority."""

    def __init__(
        self,
        config: Config,
        fetcher: Fetch,
        store: ReportStore,
        bot_username: str,
        models: tuple[ModelConfig, ...],
    ) -> None:
        """Bind one request's configuration and capabilities."""
        self.config, self.fetcher, self.store = config, fetcher, store
        self.bot_username = bot_username
        self.models = models
        self.actions = Actions(fetcher, config.bot_token, store)
        self.reporting = Reporting(config.reporter_ids)
        self.command_plugins = (
            ReportingPlugin(
                "command",
                lambda update: source_argument(update.message, self.bot_username) is not None,
                self.source_command,
            ),
        )
        self.reply_plugins = (
            ReportingPlugin(
                "report",
                lambda update: (
                    reply_target(update.message) is not None and mentions_bot(update.message, self.bot_username)
                ),
                self.report,
            ),
        )

    async def process(self, content_type: str | None, body: bytes) -> AppResponse:
        """Parse an authenticated update and apply its moderation policy."""
        if content_type is None or content_type.partition(";")[0].strip().lower() != "application/json":
            return AppResponse(415, "expected application/json")
        try:
            update = parse_update(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return AppResponse(400, "invalid update")
        if update is None:
            return AppResponse(200, "ignored")
        response = await self.reporting.dispatch(self.command_plugins, update)
        return response or await self.process_update(update)

    async def process_update(self, update: TelegramUpdate) -> AppResponse:
        """Route a group message through account, source, report and content policies."""
        if not update.in_group:
            return AppResponse(200, "ignored")
        try:
            sources = await self.store.matching_sources(update.sources) if update.sources else ()
        except EvidenceError:
            return AppResponse(503, "source list unavailable; retry pending")
        if update.edited:
            try:
                await self.cancel_model_task(update)
            except EvidenceError:
                return AppResponse(503, MODEL_TASK_STORAGE_FAILURE)
        account_response = await self.check_account_blacklist(update)
        if sources:
            return await self.moderate_sources(update, sources, account_response)
        if account_response is not None:
            return account_response
        return await self.moderate_unmatched(update)

    async def moderate_unmatched(self, update: TelegramUpdate) -> AppResponse:
        """Index a message without blacklist matches, then handle a report or check its content."""
        try:
            await self.index_message(update)
        except EvidenceError:
            return AppResponse(503, "message index unavailable; retry pending")
        response = await self.reporting.dispatch(self.reply_plugins, update)
        if response is not None:
            return response
        try:
            return await self.check_content(update)
        except EvidenceError:
            return AppResponse(503, MODEL_TASK_STORAGE_FAILURE)

    async def moderate_sources(
        self, update: TelegramUpdate, sources: tuple[int, ...], account_response: AppResponse | None
    ) -> AppResponse:
        """Moderate the sender and each matched source with independent retry progress."""
        response = account_response or await self.actions.delete_and_mute(update.message)
        for identifier in sources:
            if account_response is not None and identifier == user_id(update.message, include_bots=True):
                continue
            try:
                source_response = await self.moderate_account(update, identifier, subject_id=identifier)
            except EvidenceError:
                source_response = AppResponse(503, "source moderation storage unavailable; retry pending")
            if source_response.status != HTTPStatus.OK:
                response = source_response
        return response

    async def source_command(self, update: TelegramUpdate) -> AppResponse:
        """Save a resolved source, reply, and remove the command from group chats."""
        username = source_argument(update.message, self.bot_username)
        if update.edited or username is None:
            return AppResponse(200, "command ignored")
        reply_report = update.in_group and not username and reply_target(update.message) is not None
        if update.update_id is None or (reply_report and reported_target(update) is None):
            return AppResponse(400, "invalid command")
        source = replied_source(update.message) if reply_report else username
        identifier = await self._register_source(update.update_id, update.raw_json, source)
        reply = "Could not resolve the account. Use /bs @username, or reply to an inline bot message with /bs."
        if identifier is not None:
            label = f": @{username}" if username else ""
            reply = f"Source saved in D1{label}\nID: {identifier}"
        response = AppResponse(200, "command handled")
        if reply_report and identifier is not None:
            response = await self.report(update, source_id=identifier)
            if response.status != HTTPStatus.OK:
                return response
        if response.needs_confirmation:
            reply += "\nBan result is uncertain; check membership before submitting a new report."
        return await self._finish_source_command(update, reply, response)

    async def _finish_source_command(self, update: TelegramUpdate, reply: str, response: AppResponse) -> AppResponse:
        """Acknowledge the result and retain commands that require manual inspection."""
        try:
            await call_method(
                self.fetcher,
                self.config.bot_token,
                "sendMessage",
                {
                    "chat_id": update.chat_id,
                    "text": reply,
                    "reply_parameters": {"message_id": update.message_id, "allow_sending_without_reply": True},
                },
            )
        except TelegramError as error:
            if error.retryable:
                raise
            response = replace(response, body=response.body + "; acknowledgement rejected")
        if response.needs_confirmation:
            return response
        if update.in_group:
            outcome = await delete_message(self.fetcher, self.config.bot_token, update.chat_id, update.message_id)
            if outcome is DeleteOutcome.RETRYABLE_FAILURE:
                return AppResponse(503, "command handled; command cleanup pending retry")
            if outcome is DeleteOutcome.PERMANENT_FAILURE:
                return AppResponse(200, "command handled; command cleanup rejected; check deletion permissions")
        return response

    async def _register_source(self, update_id: int, raw_json: str, source: str | int | None) -> int | None:
        """Pin the resolved source before registration so retries cannot switch accounts."""
        bot_id = self.actions.bot_id
        identifier = await self.store.command_source(bot_id, update_id)
        if identifier is None:
            identifier = (
                await resolve_source(self.fetcher, self.config.bot_token, source) if isinstance(source, str) else source
            )
        if identifier is None:
            return None
        await self.store.save((bot_id, update_id), raw_json, int(time.time()), 0)
        await self.store.pin_command_source(bot_id, update_id, identifier)
        # Concurrent deliveries must use the first resolution stored for this update.
        identifier = await self.store.command_source(bot_id, update_id)
        if identifier is None:
            raise EvidenceError
        await self.store.add_source(identifier)
        return identifier

    async def cancel_model_task(self, update: TelegramUpdate) -> None:
        """Stop pending inference or moderation based on content that an edit replaced."""
        now = int(time.time())
        if update.sent_at is not None and recent(update.sent_at, now):
            await ModelTasks(self.store, self.actions.bot_id).enqueue(
                update.chat_id, update.message_id, update.sent_at, None, now
            )

    def quotes_reported_spam(self, update: TelegramUpdate) -> bool:
        """Recognize a denied report on a local-rule match, where quoting that spam is expected."""
        target = reply_target(update.message)
        return (
            target is not None
            and matches_spam_pattern(target)
            and self.reporting.denied((*self.command_plugins, *self.reply_plugins), update)
        )

    async def check_content(self, update: TelegramUpdate) -> AppResponse:
        """Apply local patterns to new and edited messages, and classify only new messages.

        A denied report on local-rule spam is deleted without a mute, so a member who quotes that spam keeps speaking.
        """
        message = update.message
        if update.sent_at is None or not recent(update.sent_at, int(time.time())):
            return AppResponse(200, "ignored")
        mute = not self.quotes_reported_spam(update)
        if matches_spam_pattern(message):
            return await self.actions.delete_and_mute(message, mute=mute)
        if update.edited:
            return AppResponse(200, "ignored")
        if not self.models or user_id(message) is None or not MODEL_CONTENT_FIELDS.intersection(message):
            return AppResponse(200, "ignored")
        state = await model_input(self.fetcher, self.config.bot_token, message)
        payload: dict[str, object] = {
            "state": state,
            "target": {
                "chat": {"id": update.chat_id, "type": update.chat_type},
                "message_id": update.message_id,
                "from": {"id": user_id(message), "is_bot": False},
            },
            "mute": mute,
        }
        now = int(time.time())
        tasks = ModelTasks(self.store, self.actions.bot_id)
        if await tasks.enqueue(update.chat_id, update.message_id, update.sent_at, payload, now):
            task = await tasks.claim(now, (update.chat_id, update.message_id))
            if task is not None:
                await tasks.run(task, self.fetcher, self.config.bot_token, self.models, now)
        return AppResponse(200, "model task recorded")

    async def index_message(self, update: TelegramUpdate) -> None:
        """Index eligible account messages, including bots, within the deletion window."""
        identifier = user_id(update.message, include_bots=True)
        if (
            update.sent_at is not None
            and recent(update.sent_at, int(time.time()))
            and identifier is not None
            and update.chat_type == "supergroup"
            and not {"supergroup_chat_created", "channel_chat_created", "forum_topic_created"}.intersection(
                update.message
            )
        ):
            await self.store.remember_message(
                self.actions.bot_id, update.chat_id, update.message_id, identifier, update.sent_at
            )

    async def check_account_blacklist(self, update: TelegramUpdate) -> AppResponse | None:
        """Ban confirmed accounts and delete indexed messages without single-message fallbacks."""
        identifier = user_id(update.message, include_bots=True)
        if update.edited or identifier is None or update.chat_type != "supergroup":
            return None
        try:
            if await self.store.is_blacklisted(self.actions.bot_id, identifier):
                await self.index_message(update)
                return await self.moderate_account(update, identifier, subject_id=0)
        except EvidenceError:
            return AppResponse(503, "account moderation storage unavailable; retry pending")
        return None

    async def moderate_account(self, update: TelegramUpdate, identifier: int, *, subject_id: int) -> AppResponse:
        """Route a message-triggered account ban."""
        return await self.actions.delete_history_and_ban(
            BanTarget(update.chat_id, identifier, update.message_id + 1), update, subject_id=subject_id
        )

    async def report(self, update: TelegramUpdate, *, source_id: int | None = None) -> AppResponse:
        """Moderate reported accounts with independent authority checks and retry progress."""
        target = reported_target(update)
        if target is None:
            return AppResponse(400, "invalid report target")
        subjects = [(target, 0)]
        if source_id is not None and source_id != target.user_id:
            identifier = source_id if update.chat_type == "supergroup" else None
            subjects.append((replace(target, user_id=identifier), source_id))
        response = AppResponse(200, "report recorded")
        needs_confirmation = False
        for subject, progress_id in subjects:
            try:
                outcome = await self.actions.delete_history_and_ban(
                    subject, update, subject_id=progress_id, remove_report=source_id is None
                )
            except EvidenceError:
                outcome = AppResponse(503, "report storage unavailable; retry pending")
            needs_confirmation |= outcome.needs_confirmation
            if outcome.status != HTTPStatus.OK or response.status == HTTPStatus.OK:
                response = outcome
        return replace(response, needs_confirmation=needs_confirmation)
