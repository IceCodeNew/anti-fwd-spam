"""Shared delete-and-mute and history-cleanup-and-ban actions."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING

from .evidence import BAN_FAILED, BANNED, DELETE_BATCH_SIZE, EvidenceError
from .telegram import DeleteOutcome, TelegramError, call_method, delete_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .evidence import ReportStore, StoredReport
    from .policy import TelegramUpdate
    from .telegram import Fetch

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


def user_id(message: dict[str, object], *, include_bots: bool = False) -> int | None:
    """Return a real sender, never the fake user attached to sender_chat."""
    if "sender_chat" in message:
        return None
    sender = message.get("from")
    if not isinstance(sender, dict) or type(sender.get("is_bot")) is not bool:
        return None
    if sender["is_bot"] and not include_bots:
        return None
    identifier = sender.get("id")
    return identifier if type(identifier) is int and identifier > 0 else None


@dataclass(frozen=True, slots=True)
class AppResponse:
    """An HTTP outcome for webhook delivery and stored report completion."""

    status: int
    body: str
    target_removed: bool = False
    sender_banned: bool = False
    needs_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class BanTarget:
    """An account and its cleanup boundary in one chat.

    Reports supply an explicit message ID, including messages absent from the
    index. Automatic matches rely on the index to select only this account's messages.
    """

    chat_id: int
    user_id: int | None
    before_message_id: int
    message_id: int | None = None


class Actions:
    """Apply moderation with durable deduplication and bounded history cleanup."""

    def __init__(self, fetcher: Fetch, token: str, store: ReportStore) -> None:
        """Bind Telegram transport and this bot's progress store."""
        self.fetcher, self.token, self.store = fetcher, token, store
        self.bot_id = int(token.split(":", 1)[0])

    async def member_status(self, chat_id: int, identifier: int) -> str:
        """Reject malformed or mismatched membership responses before acting."""
        result = await call_method(
            self.fetcher, self.token, "getChatMember", {"chat_id": chat_id, "user_id": identifier}
        )
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

    async def delete_and_mute(
        self,
        message: dict[str, object],
        *,
        can_act: Callable[[], Awaitable[bool]] | None = None,
        mute: bool = True,
    ) -> AppResponse:
        """Delete one message and permanently mute its sender, preserving administrators and history."""
        chat, message_id = message.get("chat"), message.get("message_id")
        if not isinstance(chat, dict) or type(chat.get("id")) is not int or type(message_id) is not int:
            return AppResponse(400, "invalid moderation target")
        chat_id, identifier = chat["id"], user_id(message, include_bots=True)
        sender_chat = message.get("sender_chat")
        try:
            if (isinstance(sender_chat, dict) and sender_chat.get("id") == chat_id) or (
                identifier is not None and await self.member_status(chat_id, identifier) in ADMIN_STATUSES
            ):
                return AppResponse(200, "automatic moderation skipped; protected administrator")
        except TelegramError as error:
            return AppResponse(503 if error.retryable else 200, "automatic authority check failed")
        if can_act is not None and not await can_act():
            return AppResponse(200, "moderation cancelled")
        response = await self._delete_target(chat_id, message_id)
        identifier = user_id(message)
        if not mute or not response.target_removed or identifier is None or chat.get("type") != "supergroup":
            return replace(response, body=response.body + "; mute skipped")
        try:
            response = await self._mute(chat_id, message_id, identifier, response.body, can_act)
        except EvidenceError:
            response = AppResponse(503, "mute storage unavailable; retry pending")
        return response

    async def delete_history_and_ban(
        self,
        target: BanTarget,
        update: TelegramUpdate,
        *,
        subject_id: int,
        remove_report: bool = True,
    ) -> AppResponse:
        """Persist and resume a ban, target deletion and indexed history cleanup.

        Explicit targets are reports: preserve their evidence and clean up the
        reporter's message after successful moderation. Automatic matches store
        the triggering message and only delete this account's indexed history.
        Command handlers can own report removal after acknowledging multiple subjects.
        """
        if update.update_id is None:
            return AppResponse(400, "invalid moderation update")
        key = (self.bot_id, update.update_id)
        saved = await self.store.save(key, update.raw_json, int(time.time()), subject_id)
        if saved.status is not None:
            return AppResponse(saved.status, saved.body)
        if saved.ban_claimed and saved.moderation_result is None:
            # Another delivery may still be recording success. Do not finalize its progress.
            return AppResponse(
                200,
                "ban confirmation failed; check membership and submit a new report if needed",
                needs_confirmation=True,
            )
        try:
            reporter_id = user_id(update.message)
            authorized = (
                target.message_id is None
                or saved.moderation_result is not None
                or saved.ban_claimed
                or reporter_id is None
                or await self.member_status(target.chat_id, reporter_id) in ADMIN_STATUSES
            )
            response = (
                await self._execute_ban(target, key, saved, subject_id=subject_id)
                if authorized
                else AppResponse(200, "report recorded")
            )
        except TelegramError as error:
            response = AppResponse(503 if error.retryable else 200, "report recorded; authority check failed")
        if target.message_id is not None and remove_report:
            response = await self._remove_report_message(target.chat_id, target.before_message_id, response)
        await self.store.finish(*key, response.status, response.body, subject_id)
        return response

    async def _execute_ban(
        self,
        target: BanTarget,
        report_key: tuple[int, int],
        saved: StoredReport,
        *,
        subject_id: int,
    ) -> AppResponse:
        chat_id, identifier = target.chat_id, target.user_id
        response = await self._resume_ban(chat_id, identifier, report_key, saved, subject_id)
        if response is None:
            # No account to ban, or a protected administrator: only an explicit report target remains.
            if target.message_id is None:
                return AppResponse(200, "account moderation skipped; protected administrator")
            response = AppResponse(200, "ban skipped")
        # A report deletes its target unless the target is already removed or the ban waits for a retry.
        if target.message_id is not None and response.status == HTTPStatus.OK and not response.target_removed:
            deletion = await self._delete_target(chat_id, target.message_id)
            response = replace(deletion, body=f"{deletion.body}; {response.body}", sender_banned=response.sender_banned)
        if response.target_removed:
            await self.store.remember_moderation(*report_key, response.body, subject_id)
        if identifier is not None and response.sender_banned:
            await self.store.blacklist_user(self.bot_id, identifier, int(time.time()))
            if response.status == HTTPStatus.OK:
                response = await self._remove_recent_history(chat_id, identifier, target.before_message_id, response)
        return response

    async def _resume_ban(
        self,
        chat_id: int,
        identifier: int | None,
        report_key: tuple[int, int],
        saved: StoredReport,
        subject_id: int,
    ) -> AppResponse | None:
        if saved.moderation_result == BANNED:
            # The ban succeeded, but the target deletion or report cleanup did not finish.
            return AppResponse(200, BANNED, sender_banned=True)
        if saved.moderation_result == BAN_FAILED:
            # Telegram rejected the ban permanently; only a pending target deletion remains.
            return AppResponse(200, BAN_FAILED)
        if saved.moderation_result is not None:
            return AppResponse(
                200,
                saved.moderation_result,
                target_removed=True,
                sender_banned=saved.moderation_result.endswith(f"; {BANNED}"),
            )
        return await self._ban(chat_id, identifier, report_key, subject_id) if identifier is not None else None

    async def _delete_target(self, chat_id: int, message_id: int) -> AppResponse:
        outcome = await delete_message(self.fetcher, self.token, chat_id, message_id)
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, "deletion rejected")
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, "deletion pending retry")
        deletion = "deleted" if outcome is DeleteOutcome.DELETED else "already absent"
        return AppResponse(200, deletion, target_removed=True)

    async def _remove_report_message(self, chat_id: int, message_id: int, response: AppResponse) -> AppResponse:
        if response.status != HTTPStatus.OK or not response.target_removed:
            return response
        outcome = await delete_message(self.fetcher, self.token, chat_id, message_id)
        if outcome is DeleteOutcome.RETRYABLE_FAILURE:
            return AppResponse(503, response.body + "; report cleanup pending")
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return AppResponse(200, response.body + "; report cleanup rejected")
        return AppResponse(200, response.body + "; report removed")

    async def _remove_recent_history(
        self, chat_id: int, identifier: int, before_message_id: int, response: AppResponse
    ) -> AppResponse:
        message_ids = await self.store.recent_messages(
            self.bot_id, chat_id, identifier, before_message_id, int(time.time())
        )
        if not message_ids:
            return response
        batch = message_ids[:DELETE_BATCH_SIZE]
        try:
            if await self.member_status(chat_id, identifier) in ADMIN_STATUSES:
                return AppResponse(200, response.body + "; history cleanup skipped", response.target_removed)
            result = await call_method(
                self.fetcher, self.token, "deleteMessages", {"chat_id": chat_id, "message_ids": batch}
            )
            if result is not True:
                return AppResponse(503, response.body + "; history cleanup pending retry")
        except TelegramError as error:
            return AppResponse(
                503 if error.retryable else 200,
                response.body
                + ("; history cleanup pending retry" if error.retryable else "; history cleanup rejected"),
            )
        await self.store.forget_messages(self.bot_id, chat_id, batch)
        if len(message_ids) > DELETE_BATCH_SIZE:
            return AppResponse(503, response.body + "; history cleanup pending retry")
        return AppResponse(200, response.body + "; recent history cleared", response.target_removed)

    async def _ban(
        self, chat_id: int, identifier: int, report_key: tuple[int, int], subject_id: int
    ) -> AppResponse | None:
        claimed = False
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES:
                return None
            if status != "kicked":
                claimed = await self.store.claim_ban(*report_key, subject_id)
                if not claimed:
                    return AppResponse(503, "ban already attempted; retry pending")
                result = await call_method(
                    self.fetcher,
                    self.token,
                    "banChatMember",
                    {"chat_id": chat_id, "user_id": identifier, "until_date": 0},
                )
                if result is not True:
                    return AppResponse(503, BAN_FAILED)
            await self.store.remember_moderation(*report_key, BANNED, subject_id)
        except TelegramError as error:
            if claimed and error.rejected:
                await self.store.release_ban(*report_key, subject_id)
            if not error.retryable:
                await self.store.remember_moderation(*report_key, BAN_FAILED, subject_id)
            return AppResponse(503 if error.retryable else 200, BAN_FAILED)
        return AppResponse(200, BANNED, sender_banned=True)

    async def _mute(
        self,
        chat_id: int,
        message_id: int,
        identifier: int,
        deletion: str,
        can_act: Callable[[], Awaitable[bool]] | None,
    ) -> AppResponse:
        mute_key = (self.bot_id, chat_id, message_id)
        claimed = False
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES or status == "kicked":
                return AppResponse(200, deletion + "; mute skipped")
            claimed = await self.store.claim_mute(*mute_key, int(time.time()))
            if not claimed:
                return AppResponse(200, deletion + "; mute retry skipped; check permissions if needed")
            can_mute = False
            try:
                can_mute = can_act is None or await can_act()
            finally:
                if not can_mute:
                    await self.store.release_mute(*mute_key)
            if not can_mute:
                return AppResponse(200, deletion + "; moderation cancelled")
            result = await call_method(
                self.fetcher,
                self.token,
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
            # A lost response can hide success followed by an administrator's manual unmute.
            if claimed and error.rejected and error.retryable:
                await self.store.release_mute(*mute_key)
            return AppResponse(503 if error.retryable else 200, deletion + "; mute failed")
        return AppResponse(200, deletion + "; muted")
