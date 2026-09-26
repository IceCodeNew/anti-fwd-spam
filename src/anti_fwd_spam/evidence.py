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
MESSAGE_WINDOW_SECONDS = 48 * 60 * 60
DELETE_BATCH_SIZE = 100
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

    async def add_source(self, source_id: int) -> None:
        """Persist a source once, including concurrent or redelivered commands."""
        with storage_errors():
            await (
                self.database.prepare("INSERT INTO blacklisted_sources (source_id) VALUES (?) ON CONFLICT DO NOTHING")
                .bind(source_id)
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

    async def blacklist_user(self, bot_id: int, user_id: int, now: int) -> None:
        """Retain confirmed banned accounts independently of expiring evidence."""
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO blacklisted_users (bot_id, user_id, added_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(bot_id, user_id) DO NOTHING",
                )
                .bind(bot_id, user_id, now)
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

    async def claim_mute(self, bot_id: int, chat_id: int, message_id: int, now: int) -> bool:
        """Claim a message once, including edits delivered under a different update ID."""
        with storage_errors():
            row = await (
                self.database.prepare(
                    "INSERT INTO automatic_mutes (bot_id, chat_id, message_id, expires_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(bot_id, chat_id, message_id) DO NOTHING RETURNING bot_id",
                )
                .bind(bot_id, chat_id, message_id, now + RETENTION_SECONDS)
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

    async def remember_message(
        self,
        bot_id: int,
        chat_id: int,
        message_id: int,
        sender_id: int,
        sent_at: int,
    ) -> None:
        """Index identifiers only; edits and redelivery must not refresh message age."""
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO recent_messages (bot_id, chat_id, message_id, sender_id, sent_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(bot_id, chat_id, message_id) DO NOTHING",
                )
                .bind(bot_id, chat_id, message_id, sender_id, sent_at)
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
                    "AND message_id < ? AND sent_at > ? ORDER BY message_id LIMIT ?",
                )
                .bind(
                    bot_id, chat_id, sender_id, before_message_id, now - MESSAGE_WINDOW_SECONDS, DELETE_BATCH_SIZE + 1
                )
                .all()
            )
            return [int(row.message_id) for row in result.results]

    async def forget_messages(self, bot_id: int, chat_id: int, message_ids: list[int]) -> None:
        """Remove only the batch Telegram confirmed, leaving failed batches retryable."""
        with storage_errors():
            await (
                self.database.prepare(
                    "DELETE FROM recent_messages WHERE bot_id = ? AND chat_id = ? "
                    "AND message_id IN (SELECT value FROM json_each(?))",
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
                    "AND (moderation_result IS NULL OR moderation_result IN ('banned', 'ban failed'))",
                )
                .bind(body, bot_id, update_id, subject_id)
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
        await (
            self.database.prepare(
                "DELETE FROM recent_messages WHERE rowid IN "
                "(SELECT rowid FROM recent_messages WHERE sent_at <= ? ORDER BY sent_at LIMIT 1000)",
            )
            .bind(now - MESSAGE_WINDOW_SECONDS)
            .run()
        )
        await (
            self.database.prepare(
                "DELETE FROM reports WHERE rowid IN "
                "(SELECT rowid FROM reports WHERE expires_at <= ? ORDER BY expires_at LIMIT 1000)",
            )
            .bind(now)
            .run()
        )
