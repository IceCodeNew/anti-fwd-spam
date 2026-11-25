"""Route provenance matches, authorized reports and content checks to moderation actions."""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING

from .actions import ADMIN_STATUSES, Actions, AppResponse, BanTarget, user_id
from .constants import MAX_TELEGRAM_ID, MODEL_CONTENT_FIELDS
from .evidence import MESSAGE_WINDOW_SECONDS, EvidenceError, PendingReview, ReportStore
from .model import join_model_input, model_input, profile_ids
from .policy import (
    matches_spam_pattern,
    only_mentions_bot,
    parse_update,
    reply_target,
    sticker_set_name,
    sticker_unique_id,
)
from .reporting import Reporting, ReportingPlugin
from .sources import command_argument, replied_source, resolve_source, sticker_set_argument
from .tasks import ModelTasks
from .telegram import DeleteOutcome, Fetch, TelegramError, call_method, delete_message

MODEL_TASK_STORAGE_FAILURE = "model task storage unavailable; retry pending"

if TYPE_CHECKING:
    from collections.abc import Coroutine

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
    sender = target.get("from")
    username = sender.get("username") if isinstance(sender, dict) and "sender_chat" not in target else None
    return BanTarget(
        update.chat_id,
        user_id(target, include_bots=True) if update.chat_type == "supergroup" else None,
        update.message_id,
        target_id,
        username=username if isinstance(username, str) else None,
    )


def _review_outcome(response: AppResponse) -> str:
    """Summarize a replayed report's moderation result as one review outcome."""
    if response.body == "evidence missing":
        return "evidence_missing"
    if response.needs_confirmation:
        return "needs_confirmation"
    if response.sender_banned:
        return "banned"
    if "moderation cancelled" in response.body:
        return "superseded_by_allow"
    if response.target_removed:
        return "deleted"
    return "no_action"


def reporter_id(update: TelegramUpdate) -> int | None:
    """Identify a report's sender: its sender chat when present, otherwise its genuine user."""
    sender_chat = update.message.get("sender_chat")
    if isinstance(sender_chat, dict) and type(sender_chat.get("id")) is int:
        return sender_chat["id"]
    return user_id(update.message, include_bots=True)


def _review_payload(
    update: TelegramUpdate,
    target_info: BanTarget,
    target: dict[str, object],
    *,
    source_id: int | None,
    sticker: tuple[str, bool] | None,
) -> dict[str, object]:
    """Shape one reported message into the bounded payload stored for external review."""
    sender_chat = target.get("sender_chat")
    return {
        "reporter_id": reporter_id(update),
        "source_id": source_id,
        "sticker": {"identity": sticker[0], "whole_set": sticker[1]} if sticker is not None else None,
        "target": {
            "chat_id": update.chat_id,
            "message_id": target_info.message_id,
            "sender_id": target_info.user_id,
            "sender_chat_id": sender_chat.get("id") if isinstance(sender_chat, dict) else None,
            "state": model_input(target),
        },
    }


def _review_item(review: PendingReview) -> dict[str, object]:
    """Render one stored review or sample as the external review API's item shape."""
    try:
        payload = json.loads(review.payload_json)
    except json.JSONDecodeError:
        payload = {}
    return {
        "update_id": review.update_id,
        "chat_id": review.chat_id,
        "target_message_id": review.target_message_id,
        "target_sender_id": review.target_sender_id,
        "reporter_id": review.reporter_id,
        "created_at": review.created_at,
        "payload": payload if isinstance(payload, dict) else {},
    }


def recent(sent_at: int, now: int) -> bool:
    """Return True while the bot can still delete and index the message."""
    return now - MESSAGE_WINDOW_SECONDS < sent_at <= now


def edit_revision(update: TelegramUpdate) -> int:
    """Use Telegram's update ID, or its edit timestamp when the ID is unavailable."""
    candidate = update.update_id if update.update_id is not None else update.message.get("edit_date")
    return candidate if type(candidate) is int and candidate > 0 else 1


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
        # The entrypoint keeps this work alive after it sends the webhook response.
        self.background: Coroutine[object, object, None] | None = None
        self.actions = Actions(fetcher, config.bot_token, store)
        self.reporting = Reporting(config.reporter_ids)
        self.command_plugins = (
            ReportingPlugin(
                "command",
                lambda update: command_argument(update.message, self.bot_username, "/bs") is not None,
                self.source_command,
                self.record_denied_source,
            ),
            ReportingPlugin(
                "allow",
                lambda update: command_argument(update.message, self.bot_username, "/allow") is not None,
                self.allow_command,
            ),
        )
        self.reply_plugins = (
            ReportingPlugin(
                "report",
                lambda update: (
                    reply_target(update.message) is not None and mentions_bot(update.message, self.bot_username)
                ),
                self._report_listed,
                self.record_report,
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
        if self.reporting.authorized(update):
            response = await self.reporting.dispatch(self.command_plugins, update)
            if response is not None:
                if update.edited and update.in_group:
                    try:
                        await self.cancel_model_task(update)
                    except EvidenceError:
                        return AppResponse(503, MODEL_TASK_STORAGE_FAILURE)
                return response
        response = await self._admin_allow(update)
        return response if response is not None else await self.process_update(update)

    async def process_update(self, update: TelegramUpdate) -> AppResponse:
        """Route a group message through account, source, report and content policies."""
        if not update.in_group:
            return AppResponse(200, "ignored")
        try:
            if "new_chat_members" in update.message:
                return await self.check_join(update)
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
        return account_response if account_response is not None else await self.moderate_unmatched(update)

    async def moderate_unmatched(self, update: TelegramUpdate) -> AppResponse:
        """Index a message without blacklist matches, then handle a report or check its content."""
        try:
            await self.index_message(update)
        except EvidenceError:
            return AppResponse(503, "message index unavailable; retry pending")
        response = await self.reporting.dispatch(self.reply_plugins, update)
        if response is not None:
            return response
        response = await self.reporting.dispatch(self.command_plugins, update)
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
        response = account_response or await self.delete_current(update)
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

    async def _own_source_command(
        self,
        update: TelegramUpdate,
        *,
        argument: str,
        reply_report: bool,
        target: dict[str, object] | None,
        identity: str | None,
    ) -> AppResponse | None:
        """Refuse a /bs that targets the issuer's own message or account.

        Registering or reporting yourself would blacklist and ban the person
        using the tool, so the command is acknowledged and otherwise ignored.
        """
        issuer = reporter_id(update)
        if reply_report and target is not None and user_id(target, include_bots=True) == issuer:
            return await self._finish_command(
                update,
                "Your own message cannot be reported or registered as a source.",
                AppResponse(200, "command ignored"),
            )
        if not reply_report and identity is None and argument:
            resolved = await resolve_source(self.fetcher, self.config.bot_token, argument.removeprefix("@"))
            if resolved is not None and resolved == issuer:
                return await self._finish_command(
                    update,
                    "Your own account cannot be registered as a source.",
                    AppResponse(200, "command ignored"),
                )
        return None

    async def source_command(self, update: TelegramUpdate) -> AppResponse:
        """Register sources, moderate bare replies, then acknowledge and clean the command."""
        argument = command_argument(update.message, self.bot_username, "/bs")
        if update.edited or argument is None:
            return AppResponse(200, "command ignored")
        target = reply_target(update.message)
        reply_report = update.in_group and not argument and target is not None
        if update.update_id is None or (reply_report and reported_target(update) is None):
            return AppResponse(400, "invalid command")
        set_name = sticker_set_name(target) if reply_report and target is not None else sticker_set_argument(argument)
        identity = set_name or (sticker_unique_id(target) if reply_report and target is not None else None)
        ignored = await self._own_source_command(
            update, argument=argument, reply_report=reply_report, target=target, identity=identity
        )
        if ignored is not None:
            return ignored
        replies = []
        if identity is not None:
            now = int(time.time())
            await self.store.save((self.actions.bot_id, update.update_id), update.raw_json, now, 0)
            await self.store.add_sticker(self.actions.bot_id, identity, now, whole_set=set_name is not None)
            label = "Sticker set" if set_name is not None else "Sticker"
            replies.append(f"{label} saved in D1: {identity}")
        username = argument.removeprefix("@")
        source = replied_source(update.message) if reply_report else username
        identifier = (
            await self._register_source(update.update_id, update.raw_json, source, evidence_saved=identity is not None)
            if reply_report or identity is None
            else None
        )
        if identifier is not None:
            label = f": @{username}" if username else ""
            replies.append(f"Source saved in D1{label}\nID: {identifier}")
        reply = "\n".join(replies) or (
            "Could not resolve the source. Use /bs @username or /bs https://t.me/addstickers/<set_name>, "
            "or reply to a sticker or inline bot message with /bs. "
            "To report only the sender, reply and mention this bot."
        )
        response = AppResponse(200, "command handled")
        if reply_report and replies:
            response = await self.report(update, source_id=identifier, remove_report=False)
            if response.status != HTTPStatus.OK:
                return response
            await self._capture_listed_sample(update, response, source_id=identifier)
        if response.needs_confirmation:
            reply += "\nBan result is uncertain; check membership before submitting a new report."
        return await self._finish_command(update, reply, response)

    async def _admin_allow(self, update: TelegramUpdate) -> AppResponse | None:
        """Run /allow for a supergroup administrator who is not a listed reporter.

        Administrators can already lift restrictions in Telegram itself; the
        command additionally records the allow fence a manual restoration
        cannot write. It restores access in this group only and leaves the
        global blacklist entries to listed reporters. Every other command
        stays limited to listed reporters.
        """
        if (
            update.edited
            or update.chat_type != "supergroup"
            or command_argument(update.message, self.bot_username, "/allow") is None
        ):
            return None
        identifier = user_id(update.message)
        if identifier is None:
            return None
        try:
            status = await self.actions.member_status(update.chat_id, identifier)
        except TelegramError:
            return None
        return await self.allow_command(update, global_cleanup=False) if status in ADMIN_STATUSES else None

    async def allow_command(self, update: TelegramUpdate, *, global_cleanup: bool = True) -> AppResponse:
        """Lift this account's ban in the current supergroup; a global allow also removes its blacklist rules."""
        argument = command_argument(update.message, self.bot_username, "/allow")
        if update.edited or update.chat_type != "supergroup" or argument is None or update.update_id is None:
            return AppResponse(200, "allow ignored")
        identifier = await self._allow_identity(argument, update.update_id)
        if identifier is None or identifier <= 0:
            return await self._finish_command(
                update,
                "Use /allow @username or /allow <account ID> in this supergroup.",
                AppResponse(200, "allow ignored"),
            )
        saved = await self.store.save((self.actions.bot_id, update.update_id), update.raw_json, int(time.time()), 0)
        await self.store.pin_command_source(self.actions.bot_id, update.update_id, identifier)
        identifier = await self.store.command_source(self.actions.bot_id, update.update_id)
        if identifier is None:
            raise EvidenceError
        response = AppResponse(saved.status, saved.body) if saved.status is not None else None
        if response is None:
            await self.store.request_allow(self.actions.bot_id, update.update_id)
            response = await self.actions.allow_account(update, identifier, global_cleanup=global_cleanup)
        if response.status != HTTPStatus.OK:
            return response
        if response.needs_confirmation:
            reply = f"Recovery not completed; superseded by newer moderation. Check membership.\nID: {identifier}"
        elif global_cleanup:
            reply = f"Account removed from D1 blacklists; access restored in this group.\nID: {identifier}"
        else:
            reply = f"Access restored in this group; D1 blacklist entries unchanged.\nID: {identifier}"
        return await self._finish_command(update, reply, response)

    async def _allow_identity(self, argument: str, update_id: int) -> int | None:
        """Resolve a pinned, numeric or uniquely recorded private account identity."""
        pinned = await self.store.command_source(self.actions.bot_id, update_id)
        if pinned is not None:
            return pinned
        if len(argument) <= len(str(MAX_TELEGRAM_ID)) and argument.isascii() and argument.isdecimal():
            identifier = int(argument)
            return identifier if 0 < identifier <= MAX_TELEGRAM_ID else None
        if not argument.startswith("@") or any(character.isspace() for character in argument):
            return None
        candidates = await self.store.blacklisted_username(self.actions.bot_id, argument[1:])
        if len(candidates) not in {0, 1}:
            return None
        return await resolve_source(
            self.fetcher, self.config.bot_token, argument[1:], known_id=candidates[0] if candidates else None
        )

    async def record_denied_source(self, update: TelegramUpdate) -> AppResponse | None:
        """Keep a bare reply command as evidence without registering its source or sticker."""
        if command_argument(update.message, self.bot_username, "/bs") != "" or not update.in_group:
            return None
        target = reply_target(update.message)
        set_name = sticker_set_name(target) if target is not None else None
        identity = set_name or (sticker_unique_id(target) if target is not None else None)
        sticker = (identity, set_name is not None) if identity is not None else None
        return await self.record_report(update, source_id=replied_source(update.message), sticker=sticker)

    async def record_report(
        self,
        update: TelegramUpdate,
        *,
        source_id: int | None = None,
        sticker: tuple[str, bool] | None = None,
    ) -> AppResponse:
        """Save an unlisted identity's valid reply and queue it for external review.

        No moderation action is taken here. The queue entry carries the reported
        message's bounded model input so a reviewer can judge it before its
        evidence expires. A bare /bs reply also carries its source and sticker
        identity, still unregistered, so a later spam verdict can apply the
        same registration a listed reporter's command would have made.
        """
        target_info = reported_target(update)
        target = reply_target(update.message)
        if update.update_id is None or target_info is None or target_info.message_id is None or target is None:
            return AppResponse(400, "invalid report target")
        if target_info.user_id is not None and target_info.user_id == reporter_id(update):
            return AppResponse(200, "report ignored")
        key = (self.actions.bot_id, update.update_id)
        now = int(time.time())
        await self.store.save(key, update.raw_json, now, 0)
        payload = _review_payload(update, target_info, target, source_id=source_id, sticker=sticker)
        await self.store.enqueue_review(
            self.actions.bot_id,
            PendingReview(
                update_id=update.update_id,
                chat_id=update.chat_id,
                target_message_id=target_info.message_id,
                target_sender_id=target_info.user_id,
                reporter_id=reporter_id(update),
                payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created_at=now,
            ),
        )
        return AppResponse(200, "report recorded")

    async def list_pending_reviews(self, limit: int) -> list[dict[str, object]]:
        """Return queued unlisted reports with their bounded target payloads for an external reviewer."""
        reviews = await self.store.pending_reviews(self.actions.bot_id, int(time.time()), limit)
        return [_review_item(review) for review in reviews]

    async def list_review_samples(self, limit: int) -> list[dict[str, object]]:
        """Return acted listed reports kept as samples so a reviewer can learn rules from them."""
        samples = await self.store.review_samples(self.actions.bot_id, int(time.time()), limit)
        return [{**_review_item(sample), "outcome": sample.outcome} for sample in samples]

    async def _report_listed(self, update: TelegramUpdate) -> AppResponse:
        """Apply a listed reporter's reply report and keep it as a rule-learning sample."""
        response = await self.report(update)
        await self._capture_listed_sample(update, response)
        return response

    async def _capture_listed_sample(
        self, update: TelegramUpdate, response: AppResponse, *, source_id: int | None = None
    ) -> None:
        """Keep one acted listed report as a rule-learning sample; never fail the moderation for it."""
        if response.status != HTTPStatus.OK or update.update_id is None:
            return
        target_info = reported_target(update)
        target = reply_target(update.message)
        if target_info is None or target_info.message_id is None or target is None:
            return
        payload = _review_payload(update, target_info, target, source_id=source_id, sticker=None)
        now = int(time.time())
        with contextlib.suppress(EvidenceError):
            await self.store.record_review_sample(
                self.actions.bot_id,
                PendingReview(
                    update_id=update.update_id,
                    chat_id=update.chat_id,
                    target_message_id=target_info.message_id,
                    target_sender_id=target_info.user_id,
                    reporter_id=reporter_id(update),
                    payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at=now,
                    resolved_at=now,
                    outcome=_review_outcome(response),
                ),
            )

    async def resolve_review(self, update_id: int, verdict: str) -> AppResponse:
        """Apply an external reviewer's verdict to one queued unlisted report.

        Being reported is itself strong evidence; the verdict exists to keep
        unlisted identities from abusing that power. A spam verdict replays the
        stored report through the same action a listed reporter's successful
        report performs. A not-spam verdict simply closes the review.
        """
        if verdict not in {"spam", "not_spam"}:
            return AppResponse(400, "invalid verdict")
        try:
            review = await self.store.review(self.actions.bot_id, update_id)
            if review is None:
                return AppResponse(404, "review not found")
            if review.resolved_at is not None:
                return AppResponse(200, "review already resolved")
            if verdict == "not_spam":
                outcome = "kept"
            else:
                response = await self._replay_reviewed_report(review)
                if response.status != HTTPStatus.OK:
                    return AppResponse(503, "review moderation pending; retry")
                outcome = _review_outcome(response)
            await self.store.resolve_review(
                self.actions.bot_id, update_id, verdict=verdict, outcome=outcome, now=int(time.time())
            )
            return AppResponse(200, f"review resolved; {outcome}")
        except EvidenceError:
            return AppResponse(503, "review storage unavailable; retry pending")

    async def _replay_reviewed_report(self, review: PendingReview) -> AppResponse:
        """Run a spam-verdict report through the listed reporter's moderation action.

        The stored report update is replayed unchanged, so ban claiming,
        administrator protection, /allow fences, target deletion, indexed
        history cleanup and blacklist effects are exactly the authorized
        report's. A queued /bs source or sticker is registered first, as the
        authorized command does. No acknowledgement is sent; the report
        message is removed after successful moderation, as with a reply report.
        """
        raw_json = await self.store.report_json(self.actions.bot_id, review.update_id)
        if raw_json is None:
            return AppResponse(200, "evidence missing")
        try:
            update = parse_update(raw_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return AppResponse(200, "evidence missing")
        if update is None:
            return AppResponse(200, "evidence missing")
        try:
            payload = json.loads(review.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        sticker = payload.get("sticker")
        if isinstance(sticker, dict) and isinstance(sticker.get("identity"), str):
            await self.store.add_sticker(
                self.actions.bot_id,
                sticker["identity"],
                int(time.time()),
                whole_set=sticker.get("whole_set") is True,
            )
        source = payload.get("source_id")
        source_id = (
            await self._register_source(review.update_id, raw_json, source)
            if type(source) is int and source != reporter_id(update)
            else None
        )
        return await self.report(update, source_id=source_id, remove_report=True)

    async def _finish_command(self, update: TelegramUpdate, reply: str, response: AppResponse) -> AppResponse:
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

    async def _register_source(
        self, update_id: int, raw_json: str, source: str | int | None, *, evidence_saved: bool = False
    ) -> int | None:
        """Pin the resolved source before registration so retries cannot switch accounts."""
        bot_id = self.actions.bot_id
        identifier = await self.store.command_source(bot_id, update_id)
        if identifier is None:
            identifier = (
                await resolve_source(self.fetcher, self.config.bot_token, source) if isinstance(source, str) else source
            )
        if identifier is None:
            return None
        if not evidence_saved:
            await self.store.save((bot_id, update_id), raw_json, int(time.time()), 0)
        await self.store.pin_command_source(bot_id, update_id, identifier)
        # Concurrent deliveries must use the first resolution stored for this update.
        identifier = await self.store.command_source(bot_id, update_id)
        if identifier is None:
            raise EvidenceError
        await self.store.add_source(identifier, bot_id=bot_id, update_id=update_id)
        return identifier

    async def cancel_model_task(self, update: TelegramUpdate) -> None:
        """Stop pending inference or moderation based on content that an edit replaced."""
        now = int(time.time())
        if update.sent_at is not None and recent(update.sent_at, now):
            identifier = user_id(update.message)
            sender = update.message.get("from")
            classify = (
                reply_target(update.message) is not None
                or isinstance(update.message.get("external_reply"), dict)
                or isinstance(update.message.get("quote"), dict)
                or (isinstance(sender, dict) and sender.get("is_bot") is True and "sender_chat" not in update.message)
                or (
                    identifier is not None
                    and await self.store.is_established(self.actions.bot_id, update.chat_id, identifier)
                )
            )
            await ModelTasks(self.store, self.actions.bot_id).cancel_edit(
                (update.chat_id, update.message_id), update.sent_at, now, edit_revision(update), classify=classify
            )

    def quotes_reported_spam(self, update: TelegramUpdate) -> bool:
        """Recognize a denied report on a local-rule match, where quoting that spam is expected."""
        target = reply_target(update.message)
        return (
            target is not None
            and matches_spam_pattern(target)
            and self.reporting.denied((*self.command_plugins, *self.reply_plugins), update)
        )

    async def matches_sticker(self, message: dict[str, object]) -> bool:
        """Check the current set, then the individual sticker, without a list cache."""
        set_name = sticker_set_name(message)
        if set_name is not None and await self.store.is_sticker_blacklisted(
            self.actions.bot_id, set_name, whole_set=True
        ):
            return True
        unique_id = sticker_unique_id(message)
        return unique_id is not None and await self.store.is_sticker_blacklisted(self.actions.bot_id, unique_id)

    async def delete_current(self, update: TelegramUpdate, *, mute: bool = True) -> AppResponse:
        """Fence immediate deletion and muting after a later account recovery."""
        identifier = user_id(update.message, include_bots=True)

        async def can_act() -> bool:
            return (
                identifier is None
                or (await self.store.newer_allow(self.actions.bot_id, identifier, update.update_id)) is None
            )

        try:
            return await self.actions.delete_and_mute(
                update.message, mute=mute, can_act=can_act, update_id=update.update_id
            )
        except EvidenceError:
            return AppResponse(503, "moderation storage unavailable; retry pending")

    async def check_content(self, update: TelegramUpdate) -> AppResponse:
        """Apply local rules, then queue profile checks and classification for eligible messages and edits.

        A denied report on local-rule spam is deleted without a mute, so a member who quotes that spam keeps speaking.
        """
        message = update.message
        if update.sent_at is None or not recent(update.sent_at, int(time.time())):
            return AppResponse(200, "ignored")
        mute = not self.quotes_reported_spam(update)
        referenced = (
            reply_target(message) is not None
            or isinstance(message.get("external_reply"), dict)
            or isinstance(message.get("quote"), dict)
        )
        sender = message.get("from")
        bot_sender = isinstance(sender, dict) and sender.get("is_bot") is True and "sender_chat" not in message
        identifier = user_id(message, include_bots=True)
        pattern_match = matches_spam_pattern(message)
        if pattern_match and only_mentions_bot(message, self.bot_username):
            # A sole mention of this bot is a report summon, so the mention-only
            # template must not punish it; every other match still applies.
            pattern_match = matches_spam_pattern(message, mention_template=False)
        established = (
            identifier is not None
            and not bot_sender
            and (pattern_match or (update.edited and not referenced))
            and await self.store.is_established(self.actions.bot_id, update.chat_id, identifier)
        )
        spam = pattern_match and not established
        if not spam and (not referenced or bot_sender):
            try:
                spam = await self.matches_sticker(message)
            except EvidenceError:
                return AppResponse(503, "sticker list unavailable; retry pending")
        if spam:
            return await self.delete_current(update, mute=mute)
        if (
            (update.edited and not referenced and not established and not bot_sender)
            or identifier is None
            or not MODEL_CONTENT_FIELDS.intersection(message)
        ):
            return AppResponse(200, "ignored")
        payload: dict[str, object] = {
            "state": model_input(message),
            "target": {
                "chat": {"id": update.chat_id, "type": update.chat_type},
                "message_id": update.message_id,
                "from": {"id": identifier, "is_bot": bot_sender},
            },
            "mute": mute,
            "profile_ids": profile_ids(message),
            "update_id": update.update_id,
        }
        tasks = ModelTasks(self.store, self.actions.bot_id)
        if update.edited:
            queued = await tasks.enqueue_edit(
                (update.chat_id, update.message_id), update.sent_at, payload, int(time.time()), edit_revision(update)
            )
        else:
            queued = await tasks.enqueue(update.chat_id, update.message_id, update.sent_at, payload, int(time.time()))
        if queued:
            self.background = self.run_task(tasks, update.chat_id, update.message_id)
        return AppResponse(200, "model task recorded")

    async def check_join(self, update: TelegramUpdate) -> AppResponse:
        """Classify joining accounts independently, never the notice's inviter."""
        now = int(time.time())
        if (
            update.edited
            or update.chat_type != "supergroup"
            or update.sent_at is None
            or not recent(update.sent_at, now)
        ):
            return AppResponse(200, "join ignored")
        tasks = ModelTasks(self.store, self.actions.bot_id)
        queued: list[int] = []
        seen: set[int] = set()
        members = update.message.get("new_chat_members")
        if not isinstance(members, list):
            return AppResponse(400, "invalid joined members")
        for member in members:
            identifier = member["id"]
            if identifier in seen or identifier == self.actions.bot_id:
                continue
            seen.add(identifier)
            if (await self.store.newer_allow(self.actions.bot_id, identifier, update.update_id)) is not None:
                continue
            if member["is_bot"] and not (
                await self.store.is_blacklisted(self.actions.bot_id, identifier)
                or await self.store.matching_sources(frozenset({identifier}))
            ):
                continue
            payload: dict[str, object] = {
                "state": join_model_input(member),
                "target": {
                    "chat": {"id": update.chat_id, "type": update.chat_type},
                    "message_id": update.message_id,
                    "from": member,
                },
                "joining": True,
                "update_id": update.update_id,
                "update_json": update.raw_json,
            }
            if await tasks.enqueue(
                update.chat_id, update.message_id, update.sent_at, payload, now, subject_id=identifier
            ):
                queued.append(identifier)
        if queued:
            self.background = self.run_join_tasks(tasks, update, queued[:3])
        return AppResponse(200, "join tasks recorded")

    async def run_join_tasks(self, tasks: ModelTasks, update: TelegramUpdate, subjects: list[int]) -> None:
        """Start a bounded number of independent member tasks; the scheduler resumes the rest."""
        for identifier in subjects:
            await self.run_task(tasks, update.chat_id, update.message_id, subject_id=identifier)

    async def run_task(self, tasks: ModelTasks, chat_id: int, message_id: int, *, subject_id: int = 0) -> None:
        """Check the biography and classify after the webhook response; the scheduled trigger retries failures."""
        now = int(time.time())
        task = await tasks.claim(now, (chat_id, message_id), subject_id=subject_id)
        if task is not None:
            await tasks.run(task, self.fetcher, self.config.bot_token, self.models, now)

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
            command = any(
                isinstance(entities := update.message.get(field), list)
                and any(isinstance(entity, dict) and entity.get("type") == "bot_command" for entity in entities)
                for field in ("entities", "caption_entities")
            )
            eligible = (
                not update.edited
                and user_id(update.message) is not None
                and bool(MODEL_CONTENT_FIELDS.intersection(update.message))
                and not command
                and not any(plugin.matches(update) for plugin in self.reply_plugins)
            )
            await self.store.remember_message(
                self.actions.bot_id,
                update.chat_id,
                update.message_id,
                identifier,
                update.sent_at,
                activity_eligible=eligible,
            )

    async def check_account_blacklist(self, update: TelegramUpdate) -> AppResponse | None:
        """Ban confirmed accounts and delete indexed messages without single-message fallbacks."""
        identifier = user_id(update.message, include_bots=True)
        if update.edited or identifier is None or update.chat_type != "supergroup":
            return None
        try:
            if await self.store.is_blacklisted(self.actions.bot_id, identifier):
                await self.index_message(update)
                await self.store.invalidate_activity(self.actions.bot_id, update.chat_id, update.message_id)
                return await self.moderate_account(update, identifier, subject_id=identifier)
        except EvidenceError:
            return AppResponse(503, "account moderation storage unavailable; retry pending")
        return None

    async def moderate_account(self, update: TelegramUpdate, identifier: int, *, subject_id: int) -> AppResponse:
        """Route a message-triggered account ban."""
        sender = update.message.get("from")
        username = sender.get("username") if isinstance(sender, dict) and sender.get("id") == identifier else None
        return await self.actions.delete_history_and_ban(
            BanTarget(
                update.chat_id,
                identifier,
                update.message_id + 1,
                username=username if isinstance(username, str) else None,
            ),
            update,
            subject_id=subject_id,
        )

    async def report(
        self, update: TelegramUpdate, *, source_id: int | None = None, remove_report: bool = True
    ) -> AppResponse:
        """Moderate reported accounts with independent authority checks and retry progress."""
        target = reported_target(update)
        if target is None:
            return AppResponse(400, "invalid report target")
        subjects = [(target, 0)]
        if source_id is not None and source_id != target.user_id:
            identifier = source_id if update.chat_type == "supergroup" else None
            subjects.append((replace(target, user_id=identifier, username=None), source_id))
        reporter = reporter_id(update)
        subjects = [(subject, progress_id) for subject, progress_id in subjects if subject.user_id != reporter]
        if not subjects:
            return AppResponse(200, "report ignored")
        response = AppResponse(200, "report recorded")
        needs_confirmation = False
        for subject, progress_id in subjects:
            try:
                outcome = await self.actions.delete_history_and_ban(
                    subject, update, subject_id=progress_id, remove_report=remove_report
                )
            except EvidenceError:
                outcome = AppResponse(503, "report storage unavailable; retry pending")
            needs_confirmation |= outcome.needs_confirmation
            if outcome.status != HTTPStatus.OK or response.status == HTTPStatus.OK:
                response = outcome
        return replace(response, needs_confirmation=needs_confirmation)
