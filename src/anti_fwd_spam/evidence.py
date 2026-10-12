"""Preserve report JSON, moderation progress and message identifiers in D1."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

RETENTION_SECONDS = 3 * 24 * 60 * 60
# This deletion window also delays activity credit against farming; retention changes require behavior review.
MESSAGE_WINDOW_SECONDS = 48 * 60 * 60
DELETE_BATCH_SIZE = 100
# Ban progress saved before the report finishes; a target-removal result replaces it.
BANNED = "banned"
BAN_FAILED = "ban failed"
ALLOW_REQUESTED = "allow requested"
ACCESS_RECOVERING = "access recovering"
UNBANNED = "unbanned"
ALLOWED = "allowed"
ALLOW_MARKERS = (ALLOW_REQUESTED, ACCESS_RECOVERING, UNBANNED, ALLOWED)


class EvidenceError(Exception):
    """Evidence was not durably saved; moderation must not proceed."""


@contextmanager
def storage_errors() -> Iterator[None]:
    """Raise EvidenceError for all D1 failures. This also applies to JavaScript exceptions."""
    try:
        yield
    except Exception as error:
        raise EvidenceError from error


@dataclass(frozen=True, slots=True)
class StoredReport:
    """Track target moderation independently of final report cleanup."""

    status: int | None
    body: str
    moderation_result: str | None
    ban_claimed: bool


class ReportStore:
    """Use the Worker D1 binding without exposing report contents in logs."""

    def __init__(self, database: Any) -> None:  # noqa: ANN401 - D1 is an untyped JavaScript proxy.
        """Bind the request's D1 database."""
        self.database = database

    async def command_source(self, bot_id: int, update_id: int) -> int | None:
        """Keep retries bound to the originally resolved account after username changes."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT command_source_id FROM reports WHERE bot_id = ? AND update_id = ? AND subject_id = 0",
                )
                .bind(bot_id, update_id)
                .first()
            )
            return int(row.command_source_id) if row is not None and row.command_source_id is not None else None

    async def pin_command_source(self, bot_id: int, update_id: int, source_id: int) -> None:
        """Save the first resolution before any source or membership changes."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET command_source_id = ? WHERE bot_id = ? AND update_id = ? "
                    "AND subject_id = 0 AND command_source_id IS NULL",
                )
                .bind(source_id, bot_id, update_id)
                .run()
            )

    async def request_allow(self, bot_id: int, update_id: int) -> None:
        """Publish an account fence without resetting confirmed unban or completion progress."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET moderation_result = ? WHERE bot_id = ? AND update_id = ? "
                    "AND subject_id = 0 AND command_source_id IS NOT NULL AND moderation_result IS NULL",
                )
                .bind(ALLOW_REQUESTED, bot_id, update_id)
                .run()
            )

    async def claim_access_recovery(self, bot_id: int, update_id: int) -> bool:
        """Claim one permission-changing request before calling Telegram; uncertain success is not retried."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "UPDATE reports SET moderation_result = ? WHERE bot_id = ? AND update_id = ? AND subject_id = 0 "
                    "AND moderation_result = ? AND response_status IS NULL AND NOT EXISTS "
                    "(SELECT 1 FROM automatic_mutes AS m WHERE m.bot_id = reports.bot_id "
                    "AND m.chat_id = json_extract(reports.raw_update, '$.message.chat.id') "
                    "AND m.user_id = reports.command_source_id AND m.update_id > reports.update_id) "
                    "AND NOT EXISTS (SELECT 1 FROM reports AS newer WHERE newer.bot_id = reports.bot_id "
                    "AND newer.subject_id = 0 AND newer.command_source_id = reports.command_source_id "
                    "AND newer.update_id > reports.update_id "
                    "AND json_extract(newer.raw_update, '$.message.chat.id') = "
                    "json_extract(reports.raw_update, '$.message.chat.id') "
                    "AND newer.moderation_result IN (?, ?, ?, ?)) RETURNING bot_id"
                )
                .bind(ACCESS_RECOVERING, bot_id, update_id, ALLOW_REQUESTED, *ALLOW_MARKERS)
                .first()
            )
        return row is not None

    async def release_access_recovery(self, bot_id: int, update_id: int) -> None:
        """Retry only an explicitly rejected recovery, never a request with an unknown outcome."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET moderation_result = ? WHERE bot_id = ? AND update_id = ? "
                    "AND subject_id = 0 AND moderation_result = ? AND response_status IS NULL"
                )
                .bind(ALLOW_REQUESTED, bot_id, update_id, ACCESS_RECOVERING)
                .run()
            )

    async def later_mute(self, bot_id: int, identifier: int, chat_id: int, update_id: int) -> bool:
        """Preserve a newer issued mute, including one waiting for Telegram confirmation."""
        with storage_errors():
            row = (
                await self.database.prepare(
                    "SELECT 1 FROM automatic_mutes WHERE bot_id = ? AND chat_id = ? AND user_id = ? "
                    "AND update_id > ? LIMIT 1"
                )
                .bind(bot_id, chat_id, identifier, update_id)
                .first()
            )
        return row is not None

    async def confirm_unban(self, bot_id: int, update_id: int) -> None:
        """Checkpoint Telegram success before attempting any atomic blacklist removal."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET moderation_result = ? WHERE bot_id = ? AND update_id = ? "
                    "AND subject_id = 0 AND moderation_result = ?",
                )
                .bind(UNBANNED, bot_id, update_id, ACCESS_RECOVERING)
                .run()
            )

    async def newer_allow(
        self, bot_id: int, identifier: int, event_update_id: int | None, *, chat_id: int | None = None
    ) -> int | None:
        """Read the newest retained command fence for an older or legacy event."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT update_id FROM reports WHERE bot_id = ? AND subject_id = 0 "
                    "AND command_source_id = ? AND moderation_result IN (?, ?, ?, ?) "
                    "AND (? IS NULL OR update_id > ?) "
                    "AND (? IS NULL OR json_extract(raw_update, '$.message.chat.id') = ?) "
                    "ORDER BY update_id DESC LIMIT 1",
                )
                .bind(bot_id, identifier, *ALLOW_MARKERS, event_update_id, event_update_id, chat_id, chat_id)
                .first()
            )
        return int(row.update_id) if row is not None else None

    async def later_confirmed_ban(self, bot_id: int, identifier: int, chat_id: int, update_id: int) -> bool:
        """Preserve later confirmed bans when compensating an older in-flight request."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT 1 FROM reports WHERE bot_id = ? AND update_id > ? "
                    "AND COALESCE(json_extract(raw_update, '$.message.chat.id'), "
                    "json_extract(raw_update, '$.edited_message.chat.id')) = ? "
                    "AND (subject_id = ? OR (subject_id = 0 AND COALESCE("
                    "json_extract(raw_update, '$.message.reply_to_message.from.id'), "
                    "json_extract(raw_update, '$.edited_message.reply_to_message.from.id'), "
                    "json_extract(raw_update, '$.message.from.id'), "
                    "json_extract(raw_update, '$.edited_message.from.id')) = ?)) "
                    "AND (ban_claimed = 1 OR moderation_result = ? OR moderation_result LIKE '%; banned') LIMIT 1",
                )
                .bind(bot_id, update_id, chat_id, identifier, identifier, BANNED)
                .first()
            )
        return row is not None

    async def complete_allow(self, bot_id: int, identifier: int, update_id: int) -> bool:
        """Atomically remove rules only while this confirmed unban is still the latest account decision."""
        key = (bot_id, update_id)
        with storage_errors():
            await self.database.batch(
                [
                    self.database.prepare(
                        "UPDATE reports SET moderation_result = ? WHERE bot_id = ? AND update_id = ? "
                        "AND subject_id = 0 AND moderation_result = ? AND response_status IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM reports AS n WHERE n.bot_id = ? AND n.update_id > ? AND "
                        "((n.command_source_id = ? AND n.moderation_result IS NULL) OR "
                        "((n.subject_id = ? OR (n.subject_id = 0 AND "
                        "COALESCE(json_extract(n.raw_update, '$.message.reply_to_message.from.id'), "
                        "json_extract(n.raw_update, '$.edited_message.reply_to_message.from.id')) = ?)) "
                        "AND (n.ban_claimed = 1 OR n.moderation_result = ? OR n.moderation_result LIKE '%; banned')))) "
                        "AND NOT EXISTS (SELECT 1 FROM automatic_mutes AS m WHERE m.bot_id = reports.bot_id "
                        "AND m.chat_id = json_extract(reports.raw_update, '$.message.chat.id') "
                        "AND m.user_id = reports.command_source_id AND m.update_id > reports.update_id) "
                        "AND NOT EXISTS (SELECT 1 FROM reports AS newer WHERE newer.bot_id = reports.bot_id "
                        "AND newer.subject_id = 0 AND newer.command_source_id = reports.command_source_id "
                        "AND newer.update_id > reports.update_id "
                        "AND json_extract(newer.raw_update, '$.message.chat.id') = "
                        "json_extract(reports.raw_update, '$.message.chat.id') "
                        "AND newer.moderation_result IN (?, ?, ?, ?))"
                    ).bind(ALLOWED, *key, UNBANNED, *key, identifier, identifier, identifier, BANNED, *ALLOW_MARKERS),
                    self.database.prepare(
                        "DELETE FROM blacklisted_users WHERE bot_id = ? AND user_id = ? AND EXISTS "
                        "(SELECT 1 FROM reports WHERE bot_id = ? AND update_id = ? AND subject_id = 0 "
                        "AND moderation_result = ? AND response_status IS NULL)"
                    ).bind(bot_id, identifier, *key, ALLOWED),
                    self.database.prepare(
                        "DELETE FROM blacklisted_sources WHERE source_id = ? AND EXISTS "
                        "(SELECT 1 FROM reports WHERE bot_id = ? AND update_id = ? AND subject_id = 0 "
                        "AND moderation_result = ? AND response_status IS NULL)"
                    ).bind(identifier, *key, ALLOWED),
                    self.database.prepare(
                        "UPDATE model_tasks SET phase = 'done', input_json = NULL, generation = generation + 1, "
                        "lease_until = 0 WHERE bot_id = ? AND json_extract(input_json, '$.target.from.id') = ? "
                        "AND (json_extract(input_json, '$.update_id') IS NULL "
                        "OR json_extract(input_json, '$.update_id') < ?) "
                        "AND EXISTS (SELECT 1 FROM reports WHERE bot_id = ? AND update_id = ? AND subject_id = 0 "
                        "AND moderation_result = ? AND response_status IS NULL)"
                    ).bind(bot_id, identifier, update_id, *key, ALLOWED),
                    self.database.prepare(
                        "UPDATE reports SET response_status = 200, response_body = ? "
                        "WHERE bot_id = ? AND update_id = ? AND subject_id = 0 AND moderation_result = ? "
                        "AND response_status IS NULL"
                    ).bind(ALLOWED, *key, ALLOWED),
                ],
            )
            row = (
                await self.database.prepare(
                    "SELECT response_status FROM reports WHERE bot_id = ? AND update_id = ? AND subject_id = 0"
                )
                .bind(*key)
                .first()
            )
        return row is not None and row.response_status == HTTPStatus.OK

    async def add_source(self, source_id: int, *, bot_id: int, update_id: int) -> None:
        """Persist a source once unless a newer allow command fenced this registration."""
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO blacklisted_sources (source_id) SELECT ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM reports WHERE bot_id = ? AND subject_id = 0 AND command_source_id = ? "
                    "AND moderation_result IN (?, ?, ?, ?) AND update_id > ?) "
                    "ON CONFLICT DO NOTHING",
                )
                .bind(source_id, bot_id, source_id, *ALLOW_MARKERS, update_id)
                .run()
            )

    async def matching_sources(self, source_ids: frozenset[int]) -> tuple[int, ...]:
        """Look up only the message's explicit source IDs using the primary key."""
        with storage_errors():
            rows = await (
                self.database.prepare(
                    "SELECT source_id FROM blacklisted_sources "
                    "WHERE source_id IN (SELECT value FROM json_each(?)) ORDER BY source_id",
                )
                .bind(json.dumps(sorted(source_ids)))
                .all()
            )
        return tuple(int(row.source_id) for row in rows.results)

    async def add_sticker(self, bot_id: int, identity: str, now: int, *, whole_set: bool = False) -> None:
        """Retain a whole set or one sticker for this bot, independently of report expiry."""
        sql = (
            "INSERT INTO blacklisted_sticker_sets (bot_id, set_name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(bot_id, set_name) DO NOTHING"
            if whole_set
            else "INSERT INTO blacklisted_stickers (bot_id, file_unique_id, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(bot_id, file_unique_id) DO NOTHING"
        )
        with storage_errors():
            await self.database.prepare(sql).bind(bot_id, identity, now).run()

    async def is_sticker_blacklisted(self, bot_id: int, identity: str, *, whole_set: bool = False) -> bool:
        """Query one set name or case-sensitive sticker ID through its composite primary key."""
        sql = (
            "SELECT 1 FROM blacklisted_sticker_sets WHERE bot_id = ? AND set_name = ?"
            if whole_set
            else "SELECT 1 FROM blacklisted_stickers WHERE bot_id = ? AND file_unique_id = ?"
        )
        with storage_errors():
            row = await self.database.prepare(sql).bind(bot_id, identity).first()
        return row is not None

    async def blacklisted_username(self, bot_id: int, username: str) -> tuple[int, ...]:
        """Read up to two retained matches so an ambiguous name cannot fall back to public lookup."""
        with storage_errors():
            rows = await (
                self.database.prepare(
                    "SELECT user_id FROM blacklisted_users WHERE bot_id = ? AND username = ? COLLATE NOCASE LIMIT 2"
                )
                .bind(bot_id, username)
                .all()
            )
        return tuple(int(row.user_id) for row in rows.results)

    async def blacklist_user(
        self, bot_id: int, user_id: int, now: int, *, update_id: int | None = None, username: str | None = None
    ) -> None:
        """Retain confirmed banned accounts unless a newer allow fenced this event."""
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO blacklisted_users (bot_id, user_id, added_at, username) SELECT ?, ?, ?, ? "
                    "WHERE NOT EXISTS (SELECT 1 FROM reports WHERE bot_id = ? AND subject_id = 0 "
                    "AND command_source_id = ? AND moderation_result IN (?, ?, ?, ?) "
                    "AND (? IS NULL OR update_id > ?)) ON CONFLICT(bot_id, user_id) DO UPDATE "
                    "SET username = COALESCE(excluded.username, blacklisted_users.username)",
                )
                .bind(bot_id, user_id, now, username, bot_id, user_id, *ALLOW_MARKERS, update_id, update_id)
                .run()
            )

    async def is_blacklisted(self, bot_id: int, user_id: int) -> bool:
        """Match a sender against this bot's retained account blacklist."""
        with storage_errors():
            row = await (
                self.database.prepare("SELECT 1 FROM blacklisted_users WHERE bot_id = ? AND user_id = ?")
                .bind(bot_id, user_id)
                .first()
            )
        return row is not None

    async def claim_mute(self, target: tuple[int, int, int], now: int, *, user_id: int, update_id: int | None) -> bool:
        """Claim a message once, including edits delivered under a different update ID."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "INSERT INTO automatic_mutes (bot_id, chat_id, message_id, expires_at, user_id, update_id) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(bot_id, chat_id, message_id) DO NOTHING RETURNING bot_id",
                )
                .bind(*target, now + RETENTION_SECONDS, user_id, update_id)
                .first()
            )
        return row is not None

    async def release_mute(self, bot_id: int, chat_id: int, message_id: int) -> None:
        """Allow retry when no restriction was sent or Telegram explicitly rejected it temporarily."""
        with storage_errors():
            await (
                self.database.prepare("DELETE FROM automatic_mutes WHERE bot_id = ? AND chat_id = ? AND message_id = ?")
                .bind(bot_id, chat_id, message_id)
                .run()
            )

    async def is_established(self, bot_id: int, chat_id: int, user_id: int) -> bool:
        """Require ten naturally matured observations for this bot, group and member."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT 1 FROM member_activity WHERE bot_id = ? AND chat_id = ? AND user_id = ? "
                    "AND matured_count >= 10",
                )
                .bind(bot_id, chat_id, user_id)
                .first()
            )
        return row is not None

    async def remember_message(  # noqa: PLR0913 - Message identity, original age and eligibility are independent inputs.
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        sender_id: int,
        sent_at: int,
        *,
        activity_eligible: bool = False,
    ) -> None:
        """Index identifiers only; edits and redelivery cannot refresh age or upgrade activity eligibility."""
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO recent_messages (bot_id, chat_id, message_id, sender_id, sent_at, activity_eligible) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(bot_id, chat_id, message_id) DO NOTHING",
                )
                .bind(bot_id, chat_id, message_id, sender_id, sent_at, int(activity_eligible))
                .run()
            )

    async def invalidate_activity(self, bot_id: int, chat_id: int, message_id: int) -> None:
        """Disqualify a moderated message without allowing retries or edits to restore its credit."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE recent_messages SET activity_eligible = 0 "
                    "WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
                )
                .bind(bot_id, chat_id, message_id)
                .run()
            )

    async def recent_messages(
        self,
        bot_id: int,
        chat_id: int,
        sender_id: int,
        before_message_id: int,
        now: int,
    ) -> list[int]:
        """Select a recent pre-report batch plus one row to detect remaining work."""
        with storage_errors():
            result = await (
                self.database.prepare(
                    "SELECT message_id FROM recent_messages WHERE bot_id = ? AND chat_id = ? AND sender_id = ? "
                    "AND message_id < ? AND sent_at > ? AND history_removed = 0 ORDER BY message_id LIMIT ?",
                )
                .bind(
                    bot_id, chat_id, sender_id, before_message_id, now - MESSAGE_WINDOW_SECONDS, DELETE_BATCH_SIZE + 1
                )
                .all()
            )
            return [int(row.message_id) for row in result.results]

    async def forget_messages(self, bot_id: int, chat_id: int, message_ids: list[int]) -> None:
        """Mark confirmed history removals without discarding their replay-deduplication identities."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE recent_messages SET activity_eligible = 0, history_removed = 1 "
                    "WHERE bot_id = ? AND chat_id = ? AND message_id IN (SELECT value FROM json_each(?))",
                )
                .bind(bot_id, chat_id, json.dumps(message_ids))
                .run()
            )

    async def save(
        self,
        report_key: tuple[int, int],
        raw_json: str,
        now: int,
        subject_id: int,
    ) -> StoredReport:
        """Insert immutable evidence once and read durable processing progress."""
        bot_id, update_id = report_key
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO reports "
                    "(bot_id, update_id, subject_id, received_at, expires_at, raw_update) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(bot_id, update_id, subject_id) DO NOTHING",
                )
                .bind(
                    bot_id,
                    update_id,
                    subject_id,
                    now,
                    now + RETENTION_SECONDS,
                    raw_json,
                )
                .run()
            )
            row = (
                await self.database.prepare(
                    "SELECT response_status, response_body, moderation_result, ban_claimed "
                    "FROM reports WHERE bot_id = ? AND update_id = ? AND subject_id = ?",
                )
                .bind(bot_id, update_id, subject_id)
                .first()
            )
            return StoredReport(
                int(row.response_status) if row.response_status is not None else None,
                str(row.response_body or ""),
                str(row.moderation_result) if row.moderation_result is not None else None,
                bool(row.ban_claimed),
            )

    async def claim_ban(self, bot_id: int, update_id: int, subject_id: int) -> bool:
        """Allow only one delivery to issue a destructive Telegram request."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "UPDATE reports SET ban_claimed = 1 WHERE bot_id = ? AND update_id = ? "
                    "AND subject_id = ? "
                    "AND ban_claimed = 0 AND moderation_result IS NULL AND response_status IS NULL RETURNING bot_id",
                )
                .bind(bot_id, update_id, subject_id)
                .first()
            )
        return row is not None

    async def release_ban(self, bot_id: int, update_id: int, subject_id: int) -> None:
        """Permit retries only when Telegram explicitly rejected the ban."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET ban_claimed = 0 WHERE bot_id = ? AND update_id = ? AND subject_id = ?",
                )
                .bind(bot_id, update_id, subject_id)
                .run()
            )

    async def remember_moderation(self, bot_id: int, update_id: int, body: str, subject_id: int) -> None:
        """Save a ban outcome as incomplete progress, or save a confirmed target-removal result."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET moderation_result = ? "
                    "WHERE bot_id = ? AND update_id = ? AND subject_id = ? "
                    "AND (moderation_result IS NULL OR moderation_result IN (?, ?))",
                )
                .bind(body, bot_id, update_id, subject_id, BANNED, BAN_FAILED)
                .run()
            )

    async def finish(self, bot_id: int, update_id: int, status: int, body: str, subject_id: int) -> None:
        """Record an outcome without overwriting a concurrent completion."""
        with storage_errors():
            await (
                self.database.prepare(
                    "UPDATE reports SET response_status = ?, response_body = ? "
                    "WHERE bot_id = ? AND update_id = ? AND subject_id = ? AND response_status IS NULL",
                )
                .bind(status if status == HTTPStatus.OK else None, body, bot_id, update_id, subject_id)
                .run()
            )

    async def expire(self, now: int) -> None:
        """Delete expired evidence and message identifiers in bounded indexed batches."""
        await (
            self.database.prepare(
                "DELETE FROM automatic_mutes WHERE rowid IN "
                "(SELECT rowid FROM automatic_mutes WHERE expires_at <= ? ORDER BY expires_at LIMIT 1000)",
            )
            .bind(now)
            .run()
        )
        cutoff = now - MESSAGE_WINDOW_SECONDS
        # Credit and removal use the same bounded selection; either both commit or neither does.
        await self.database.batch(
            [
                self.database.prepare(
                    "WITH expired AS (SELECT bot_id, chat_id, sender_id, activity_eligible FROM recent_messages "
                    "WHERE rowid IN (SELECT rowid FROM recent_messages WHERE sent_at <= ? "
                    "ORDER BY sent_at, rowid LIMIT 1000)) "
                    "INSERT INTO member_activity (bot_id, chat_id, user_id, matured_count) "
                    "SELECT bot_id, chat_id, sender_id, MIN(10, COUNT(*)) FROM expired "
                    "WHERE activity_eligible = 1 GROUP BY bot_id, chat_id, sender_id "
                    "ON CONFLICT(bot_id, chat_id, user_id) DO UPDATE "
                    "SET matured_count = MIN(10, member_activity.matured_count + excluded.matured_count)",
                ).bind(cutoff),
                self.database.prepare(
                    "DELETE FROM recent_messages WHERE rowid IN "
                    "(SELECT rowid FROM recent_messages WHERE sent_at <= ? ORDER BY sent_at, rowid LIMIT 1000)",
                ).bind(cutoff),
            ],
        )
        await (
            self.database.prepare(
                "DELETE FROM reports WHERE rowid IN "
                "(SELECT rowid FROM reports WHERE expires_at <= ? ORDER BY expires_at LIMIT 1000)",
            )
            .bind(now)
            .run()
        )
