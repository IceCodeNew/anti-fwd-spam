"""Preserve report JSON and searchable message classifications in D1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

RETENTION_SECONDS = 3 * 24 * 60 * 60
CONTENT_FIELDS = frozenset(
    [
        "text",
        "rich_message",
        "animation",
        "audio",
        "document",
        "live_photo",
        "paid_media",
        "photo",
        "sticker",
        "story",
        "video",
        "video_note",
        "voice",
        "checklist",
        "contact",
        "dice",
        "game",
        "poll",
        "venue",
        "location",
        "new_chat_members",
        "left_chat_member",
        "chat_owner_left",
        "chat_owner_changed",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "message_auto_delete_timer_changed",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "pinned_message",
        "invoice",
        "successful_payment",
        "refunded_payment",
        "users_shared",
        "chat_shared",
        "gift",
        "unique_gift",
        "gift_upgrade_sent",
        "connected_website",
        "write_access_allowed",
        "passport_data",
        "proximity_alert_triggered",
        "boost_added",
        "chat_background_set",
        "checklist_tasks_done",
        "checklist_tasks_added",
        "community_chat_added",
        "community_chat_joined",
        "community_chat_removed",
        "direct_message_price_changed",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_closed",
        "forum_topic_reopened",
        "general_forum_topic_hidden",
        "general_forum_topic_unhidden",
        "giveaway_created",
        "giveaway",
        "giveaway_winners",
        "giveaway_completed",
        "managed_bot_created",
        "paid_message_price_changed",
        "poll_option_added",
        "poll_option_deleted",
        "suggested_post_approved",
        "suggested_post_approval_failed",
        "suggested_post_declined",
        "suggested_post_paid",
        "suggested_post_refunded",
        "video_chat_scheduled",
        "video_chat_started",
        "video_chat_ended",
        "video_chat_participants_invited",
        "web_app_data",
    ],
)
MEDIA_FIELDS = frozenset(
    [
        "animation",
        "audio",
        "document",
        "live_photo",
        "paid_media",
        "photo",
        "sticker",
        "story",
        "video",
        "video_note",
        "voice",
    ],
)


def classify_message(message: dict[str, object]) -> dict[str, object]:
    """Describe simultaneous content fields without discarding unknown evidence."""
    return {
        "version": 1,
        "content_types": sorted(CONTENT_FIELDS.intersection(message)),
        "present_fields": sorted(message),
        "via_bot_present": "via_bot" in message,
        "media_fields": sorted(MEDIA_FIELDS.intersection(message)),
    }


class EvidenceError(Exception):
    """Evidence was not durably saved; moderation must not proceed."""


@dataclass(frozen=True, slots=True)
class StoredReport:
    """Track target moderation independently of final report cleanup."""

    status: int | None
    body: str
    moderation_result: str | None
    ban_claimed: bool


class ReportStore:
    """Use the Worker D1 binding without exposing report contents in logs."""

    def __init__(self, database: Any) -> None:  # noqa: ANN401
        """Bind the request's D1 database."""
        self.database = database

    async def save(
        self,
        bot_id: int,
        update_id: int,
        raw_json: str,
        target: dict[str, object],
        now: int,
    ) -> StoredReport:
        """Insert immutable evidence once and read durable processing progress."""
        try:
            await (
                self.database.prepare(
                    "INSERT INTO reports (bot_id, update_id, received_at, expires_at, raw_update, classification) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(bot_id, update_id) DO NOTHING",
                )
                .bind(
                    bot_id,
                    update_id,
                    now,
                    now + RETENTION_SECONDS,
                    raw_json,
                    json.dumps(classify_message(target), ensure_ascii=False),
                )
                .run()
            )
            row = (
                await self.database.prepare(
                    "SELECT response_status, response_body, moderation_result, ban_claimed "
                    "FROM reports WHERE bot_id = ? AND update_id = ?",
                )
                .bind(bot_id, update_id)
                .first()
            )
            return StoredReport(
                int(row.response_status) if row.response_status is not None else None,
                str(row.response_body or ""),
                str(row.moderation_result) if row.moderation_result is not None else None,
                bool(row.ban_claimed),
            )
        except Exception as error:
            raise EvidenceError from error

    async def claim_ban(self, bot_id: int, update_id: int) -> bool:
        """Allow only one delivery to issue a destructive Telegram request."""
        try:
            row = await (
                self.database.prepare(
                    "UPDATE reports SET ban_claimed = 1 WHERE bot_id = ? AND update_id = ? "
                    "AND ban_claimed = 0 AND moderation_result IS NULL AND response_status IS NULL RETURNING bot_id",
                )
                .bind(bot_id, update_id)
                .first()
            )
        except Exception as error:
            raise EvidenceError from error
        return row is not None

    async def release_ban(self, bot_id: int, update_id: int) -> None:
        """Permit retries only when Telegram explicitly rejected the ban."""
        try:
            await (
                self.database.prepare("UPDATE reports SET ban_claimed = 0 WHERE bot_id = ? AND update_id = ?")
                .bind(bot_id, update_id)
                .run()
            )
        except Exception as error:
            raise EvidenceError from error

    async def remember_moderation(self, bot_id: int, update_id: int, body: str) -> None:
        """Checkpoint target removal before attempting report-message cleanup."""
        try:
            await (
                self.database.prepare(
                    "UPDATE reports SET moderation_result = ? "
                    "WHERE bot_id = ? AND update_id = ? AND moderation_result IS NULL",
                )
                .bind(body, bot_id, update_id)
                .run()
            )
        except Exception as error:
            raise EvidenceError from error

    async def finish(self, bot_id: int, update_id: int, status: int, body: str) -> None:
        """Record an outcome without overwriting a concurrent completion."""
        try:
            await (
                self.database.prepare(
                    "UPDATE reports SET response_status = ?, response_body = ? "
                    "WHERE bot_id = ? AND update_id = ? AND response_status IS NULL",
                )
                .bind(status if status == HTTPStatus.OK else None, body, bot_id, update_id)
                .run()
            )
        except Exception as error:
            raise EvidenceError from error

    async def expire(self, now: int) -> None:
        """Delete expired evidence in bounded batches; the expiry column is indexed."""
        await (
            self.database.prepare(
                "DELETE FROM reports WHERE rowid IN "
                "(SELECT rowid FROM reports WHERE expires_at <= ? ORDER BY expires_at LIMIT 1000)",
            )
            .bind(now)
            .run()
        )
