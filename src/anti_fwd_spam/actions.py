"""Shared delete-and-mute and history-cleanup-and-ban actions."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING

from .evidence import ALLOW_REQUESTED, ALLOWED, BAN_FAILED, BANNED, DELETE_BATCH_SIZE, UNBANNED, EvidenceError
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
    username: str | None = None


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

    async def allow_account(self, update: TelegramUpdate, identifier: int) -> AppResponse:
        """Unban a pinned account once, then atomically remove its retained blacklist entries."""
        if update.update_id is None or update.edited or update.chat_type != "supergroup" or identifier <= 0:
            return AppResponse(400, "invalid allow target")
        key = (self.bot_id, update.update_id)
        try:
            saved = await self.store.save(key, update.raw_json, int(time.time()), 0)
            if saved.status is not None:
                return AppResponse(saved.status, saved.body)
            return await self._complete_allow(update, identifier, saved, update.update_id)
        except TelegramError:
            return AppResponse(503, "unban failed; retry pending")
        except EvidenceError:
            return AppResponse(503, "allow storage unavailable; retry pending")

    async def _complete_allow(
        self, update: TelegramUpdate, identifier: int, saved: StoredReport, update_id: int
    ) -> AppResponse:
        key = (self.bot_id, update_id)
        if await self.store.command_source(*key) != identifier or saved.moderation_result not in {
            ALLOW_REQUESTED,
            UNBANNED,
            ALLOWED,
        }:
            return AppResponse(503, "allow request not saved; retry pending")
        if await self.store.later_confirmed_ban(self.bot_id, identifier, update.chat_id, update_id):
            return AppResponse(200, "allow superseded by a newer ban; check membership", needs_confirmation=True)
        if saved.moderation_result == ALLOW_REQUESTED:
            result = await call_method(
                self.fetcher,
                self.token,
                "unbanChatMember",
                {"chat_id": update.chat_id, "user_id": identifier, "only_if_banned": True},
            )
            if result is not True:
                return AppResponse(503, "unban failed; retry pending")
            await self.store.confirm_unban(*key)
        if await self.store.later_confirmed_ban(self.bot_id, identifier, update.chat_id, update_id):
            return AppResponse(200, "allow superseded by newer moderation; check membership", needs_confirmation=True)
        if not await self.store.complete_allow(self.bot_id, identifier, update_id):
            return AppResponse(200, "allow superseded by newer moderation; check membership", needs_confirmation=True)
        return AppResponse(200, ALLOWED)

    async def ban_joiner(
        self,
        message: dict[str, object],
        update: TelegramUpdate,
        *,
        can_act: Callable[[], Awaitable[bool]] | None = None,
    ) -> AppResponse:
        """Ban only a member named in the original notice, then remove that shared notice."""
        identifier = user_id(message, include_bots=True)
        chat = message.get("chat")
        members = update.message.get("new_chat_members")
        if (
            update.edited
            or update.chat_type != "supergroup"
            or identifier is None
            or not isinstance(chat, dict)
            or chat.get("id") != update.chat_id
            or chat.get("type") != "supergroup"
            or message.get("message_id") != update.message_id
            or not isinstance(members, list)
            or not any(isinstance(member, dict) and member.get("id") == identifier for member in members)
        ):
            return AppResponse(400, "invalid joined moderation target")
        sender = message.get("from")
        username = sender.get("username") if isinstance(sender, dict) else None
        response = await self.delete_history_and_ban(
            BanTarget(
                update.chat_id,
                identifier,
                update.message_id + 1,
                username=username if isinstance(username, str) else None,
            ),
            update,
            subject_id=identifier,
            remove_report=False,
            can_act=can_act,
        )
        if not response.sender_banned or response.needs_confirmation:
            return response
        if can_act is not None and not await can_act():
            return replace(response, body=response.body + "; notice cleanup cancelled")
        deletion = await self._delete_target(update.chat_id, update.message_id)
        return replace(
            response,
            status=deletion.status if deletion.status != HTTPStatus.OK else response.status,
            body=f"{response.body}; join notice {deletion.body}",
            target_removed=deletion.target_removed,
        )

    async def delete_history_and_ban(
        self,
        target: BanTarget,
        update: TelegramUpdate,
        *,
        subject_id: int,
        remove_report: bool = True,
        can_act: Callable[[], Awaitable[bool]] | None = None,
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
            return AppResponse(
                saved.status,
                saved.body,
                target_removed=saved.moderation_result not in {None, BANNED, BAN_FAILED},
                sender_banned=saved.moderation_result == BANNED
                or (saved.moderation_result is not None and BANNED in saved.moderation_result.split("; ")),
            )
        if not await self._may_ban(target.user_id, update.update_id, can_act):
            return AppResponse(200, "moderation cancelled")
        if saved.ban_claimed and saved.moderation_result is None:
            # Another delivery may still be recording success. Do not finalize its progress.
            return AppResponse(
                200,
                "ban confirmation failed; check membership and submit a new report if needed",
                needs_confirmation=True,
            )
        response = await self._execute_ban(target, key, saved, subject_id=subject_id, can_act=can_act)
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
        can_act: Callable[[], Awaitable[bool]] | None,
    ) -> AppResponse:
        chat_id, identifier = target.chat_id, target.user_id
        response = await self._resume_ban(target, report_key, saved, subject_id, can_act=can_act)
        if response is None:
            # No account to ban, or a protected administrator: only an explicit report target remains.
            if target.message_id is None:
                return AppResponse(200, "account moderation skipped; protected administrator")
            response = AppResponse(200, "ban skipped")
        if response.needs_confirmation:
            return response
        if not await self._may_ban(identifier, report_key[1], can_act):
            return AppResponse(200, "moderation cancelled")
        # A report deletes its target unless the target is already removed or the ban waits for a retry.
        if target.message_id is not None and response.status == HTTPStatus.OK and not response.target_removed:
            deletion = await self._delete_target(chat_id, target.message_id)
            response = replace(deletion, body=f"{deletion.body}; {response.body}", sender_banned=response.sender_banned)
        if response.target_removed:
            await self.store.remember_moderation(*report_key, response.body, subject_id)
        if identifier is not None and response.sender_banned:
            await self.store.blacklist_user(
                self.bot_id, identifier, int(time.time()), update_id=report_key[1], username=target.username
            )
            if response.status == HTTPStatus.OK and await self._may_ban(identifier, report_key[1], can_act):
                response = await self._remove_recent_history(chat_id, identifier, target.before_message_id, response)
        return response

    async def _resume_ban(
        self,
        target: BanTarget,
        report_key: tuple[int, int],
        saved: StoredReport,
        subject_id: int,
        *,
        can_act: Callable[[], Awaitable[bool]] | None,
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
        return (
            await self._ban(target.chat_id, target.user_id, report_key, subject_id, can_act)
            if target.user_id is not None
            else None
        )

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
            return replace(response, status=503, body=response.body + "; report cleanup pending")
        if outcome is DeleteOutcome.PERMANENT_FAILURE:
            return replace(response, body=response.body + "; report cleanup rejected")
        return replace(response, body=response.body + "; report removed")

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
                return replace(response, body=response.body + "; history cleanup skipped")
            result = await call_method(
                self.fetcher, self.token, "deleteMessages", {"chat_id": chat_id, "message_ids": batch}
            )
            if result is not True:
                return replace(response, status=503, body=response.body + "; history cleanup pending retry")
        except TelegramError as error:
            return replace(
                response,
                status=503 if error.retryable else 200,
                body=response.body
                + ("; history cleanup pending retry" if error.retryable else "; history cleanup rejected"),
                target_removed=response.target_removed if error.retryable else False,
            )
        await self.store.forget_messages(self.bot_id, chat_id, batch)
        if len(message_ids) > DELETE_BATCH_SIZE:
            return replace(response, status=503, body=response.body + "; history cleanup pending retry")
        return replace(response, body=response.body + "; recent history cleared")

    async def _may_ban(
        self,
        identifier: int | None,
        update_id: int,
        can_act: Callable[[], Awaitable[bool]] | None,
    ) -> bool:
        if can_act is not None and not await can_act():
            return False
        return identifier is None or (await self.store.newer_allow(self.bot_id, identifier, update_id)) is None

    async def _compensate_ban(self, chat_id: int, identifier: int, update_id: int) -> AppResponse | None:
        """Undo an older late ban only when no newer confirmed ban has superseded the allow."""
        allow_id = await self.store.newer_allow(self.bot_id, identifier, update_id, chat_id=chat_id)
        if allow_id is None:
            return None
        if await self.store.later_confirmed_ban(self.bot_id, identifier, chat_id, allow_id):
            return AppResponse(200, "late ban needs membership confirmation", needs_confirmation=True)
        try:
            result = await call_method(
                self.fetcher,
                self.token,
                "unbanChatMember",
                {"chat_id": chat_id, "user_id": identifier, "only_if_banned": True},
            )
        except TelegramError:
            return AppResponse(200, "late ban needs membership confirmation", needs_confirmation=True)
        if result is not True:
            return AppResponse(200, "late ban needs membership confirmation", needs_confirmation=True)
        return AppResponse(200, "older ban cancelled by allow")

    async def _ban(
        self,
        chat_id: int,
        identifier: int,
        report_key: tuple[int, int],
        subject_id: int,
        can_act: Callable[[], Awaitable[bool]] | None,
    ) -> AppResponse | None:
        try:
            status = await self.member_status(chat_id, identifier)
            if status in ADMIN_STATUSES:
                return None
            if not await self._may_ban(identifier, report_key[1], can_act):
                return AppResponse(200, "moderation cancelled")
            if status != "kicked":
                response = await self._send_ban(chat_id, identifier, report_key, subject_id, can_act)
                if response is not None:
                    return response
            await self.store.remember_moderation(*report_key, BANNED, subject_id)
        except TelegramError as error:
            if not error.retryable:
                await self.store.remember_moderation(*report_key, BAN_FAILED, subject_id)
            return AppResponse(503 if error.retryable else 200, BAN_FAILED)
        return AppResponse(200, BANNED, sender_banned=True)

    async def _send_ban(
        self,
        chat_id: int,
        identifier: int,
        report_key: tuple[int, int],
        subject_id: int,
        can_act: Callable[[], Awaitable[bool]] | None,
    ) -> AppResponse | None:
        if not await self.store.claim_ban(*report_key, subject_id):
            return AppResponse(503, "ban already attempted; retry pending")
        permitted = False
        try:
            permitted = await self._may_ban(identifier, report_key[1], can_act)
        finally:
            if not permitted:
                await self.store.release_ban(*report_key, subject_id)
        if not permitted:
            return AppResponse(200, "moderation cancelled")
        try:
            result = await call_method(
                self.fetcher,
                self.token,
                "banChatMember",
                {"chat_id": chat_id, "user_id": identifier, "until_date": 0},
            )
        except TelegramError as error:
            if error.rejected:
                await self.store.release_ban(*report_key, subject_id)
            raise
        if result is not True:
            return AppResponse(503, BAN_FAILED)
        return await self._compensate_ban(chat_id, identifier, report_key[1])

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
