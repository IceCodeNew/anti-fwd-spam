"""Persist bounded inference and moderation retries in the existing D1 database."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from .actions import Actions
from .evidence import MESSAGE_WINDOW_SECONDS, RETENTION_SECONDS, storage_errors
from .model import SPAM_THRESHOLD, ModelRetryError, spam_probability

if TYPE_CHECKING:
    from .evidence import ReportStore
    from .model import ModelConfig
    from .telegram import Fetch

RETRY_DELAYS = (60, 120, 300)
LEASE_SECONDS = 30


@dataclass(frozen=True)
class Task:
    """One leased stage; its generation fences writes after edits or reclamation."""

    chat_id: int
    message_id: int
    phase: str
    input_json: str | None
    attempts: int
    generation: int


class ModelTasks:
    """Share scheduling and retention between classification and delete-and-mute actions."""

    def __init__(self, store: ReportStore, bot_id: int) -> None:
        """Use this bot's D1 task namespace."""
        self.store = store
        self.database = store.database
        self.bot_id = bot_id

    async def enqueue(
        self,
        chat_id: int,
        message_id: int,
        sent_at: int,
        state: dict[str, object] | None,
        now: int,
    ) -> bool:
        """Deduplicate deliveries; an edit also leaves a tombstone before any original delivery."""
        phase = "done" if state is None else "classify"
        with storage_errors():
            row = (
                await self.database.prepare(
                    "INSERT INTO model_tasks "
                    "(bot_id, chat_id, message_id, phase, input_json, due_at, stop_at, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(bot_id, chat_id, message_id) DO UPDATE "
                    "SET phase = 'done', input_json = NULL, generation = generation + 1 "
                    "WHERE excluded.phase = 'done' RETURNING message_id",
                )
                .bind(
                    self.bot_id,
                    chat_id,
                    message_id,
                    phase,
                    json.dumps(state, ensure_ascii=False) if state is not None else None,
                    now,
                    sent_at + MESSAGE_WINDOW_SECONDS,
                    now,
                    now + RETENTION_SECONDS,
                )
                .first()
            )
        return row is not None

    async def claim(self, now: int, target: tuple[int, int] | None = None) -> Task | None:
        """Atomically claim one due stage and charge an attempt before the external request."""
        with storage_errors():
            row = (
                await self.database.prepare(
                    "UPDATE model_tasks SET attempts = attempts + 1, generation = generation + 1, lease_until = ?, "
                    "due_at = ? + CASE attempts WHEN 0 THEN ? WHEN 1 THEN ? ELSE ? END "
                    "WHERE rowid = (SELECT rowid FROM model_tasks WHERE bot_id = ? AND phase != 'done' "
                    "AND due_at <= ? AND lease_until <= ? AND stop_at > ? AND expires_at > ? "
                    "AND attempts < ? AND (? IS NULL OR (chat_id = ? AND message_id = ?)) "
                    "ORDER BY due_at, rowid LIMIT 1) RETURNING *",
                )
                .bind(
                    now + LEASE_SECONDS,
                    now,
                    *RETRY_DELAYS,
                    self.bot_id,
                    now,
                    now,
                    now,
                    now,
                    len(RETRY_DELAYS) + 1,
                    target[0] if target else None,
                    target[0] if target else None,
                    target[1] if target else None,
                )
                .first()
            )
            if row is None:
                return None
            return Task(
                int(row.chat_id),
                int(row.message_id),
                str(row.phase),
                str(row.input_json) if row.input_json is not None else None,
                int(row.attempts),
                int(row.generation),
            )

    async def finish(
        self, task: Task, phase: str, now: int, *, retry: bool = False, target: dict[str, object] | None = None
    ) -> bool:
        """Commit an owned result, clearing content once classification ends."""
        if retry and task.attempts > len(RETRY_DELAYS):
            phase, retry = "done", False
        due = now + RETRY_DELAYS[task.attempts - 1] if retry else now
        with storage_errors():
            row = (
                await self.database.prepare(
                    "UPDATE model_tasks SET phase = ?, input_json = CASE WHEN ? THEN input_json ELSE ? END, "
                    "attempts = CASE WHEN ? THEN attempts ELSE 0 END, due_at = ?, lease_until = 0 "
                    "WHERE bot_id = ? AND chat_id = ? AND message_id = ? AND generation = ? "
                    "AND phase = ? AND lease_until > ? AND stop_at > ? AND expires_at > ? RETURNING message_id",
                )
                .bind(
                    phase,
                    retry,
                    json.dumps({"target": target}) if phase == "delete" and target is not None else None,
                    retry,
                    due,
                    self.bot_id,
                    task.chat_id,
                    task.message_id,
                    task.generation,
                    task.phase,
                    now,
                    now,
                    now,
                )
                .first()
            )
        return row is not None

    async def expire(self, now: int) -> None:
        """Clear aged or exhausted inputs and remove three-day metadata in bounded batches."""
        await (
            self.database.prepare(
                "DELETE FROM model_tasks WHERE rowid IN "
                "(SELECT rowid FROM model_tasks WHERE expires_at <= ? ORDER BY expires_at LIMIT 1000)",
            )
            .bind(now)
            .run()
        )
        await (
            self.database.prepare(
                "UPDATE model_tasks SET phase = 'done', input_json = NULL, generation = generation + 1 "
                "WHERE rowid IN (SELECT rowid FROM model_tasks WHERE phase != 'done' "
                "AND (stop_at <= ? OR (attempts >= ? AND lease_until <= ?)) LIMIT 1000)",
            )
            .bind(now, len(RETRY_DELAYS) + 1, now)
            .run()
        )

    async def run(self, task: Task, fetcher: Fetch, token: str, models: tuple[ModelConfig, ...], now: int) -> None:
        """Persist inference before moderation, so retries cannot reevaluate a saved score."""
        if task.phase == "classify":
            model = models[(task.attempts - 1) % len(models)]
            payload = json.loads(task.input_json or "{}")
            try:
                probability = await spam_probability(fetcher, model, payload.get("state", payload))
            except ModelRetryError as error:
                retry = error.retryable or task.attempts < len(models)
                await self.finish(task, "classify" if retry else "done", max(now, int(time.time())), retry=retry)
                return
            now = max(now, int(time.time()))
            phase = "delete" if probability is not None and probability >= SPAM_THRESHOLD else "done"
            snapshot = payload.get("target")
            target = snapshot if isinstance(snapshot, dict) else None
            if not await self.finish(task, phase, now, target=target) or phase == "done":
                return
            deletion = await self.claim(now, (task.chat_id, task.message_id))
            if deletion is None:
                return
            task = deletion
        target = await self._target(task)
        retry = False
        if target is not None:

            async def can_act() -> bool:
                return await self._owns(task, max(now, int(time.time())))

            response = await Actions(fetcher, token, self.store).delete_and_mute(target, can_act=can_act)
            retry = response.status == HTTPStatus.SERVICE_UNAVAILABLE
        await self.finish(task, "delete" if retry else "done", max(now, int(time.time())), retry=retry)

    async def _owns(self, task: Task, now: int) -> bool:
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT 1 FROM model_tasks WHERE bot_id = ? AND chat_id = ? AND message_id = ? "
                    "AND generation = ? AND phase = 'delete' AND lease_until > ? AND stop_at > ? AND expires_at > ?"
                )
                .bind(self.bot_id, task.chat_id, task.message_id, task.generation, now, now, now)
                .first()
            )
        return row is not None

    async def _target(self, task: Task) -> dict[str, object] | None:
        payload = json.loads(task.input_json or "{}")
        target = payload.get("target")
        if isinstance(target, dict):
            return target
        # Pending tasks created before sender snapshots can recover identity from the index.
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT sender_id FROM recent_messages WHERE bot_id = ? AND chat_id = ? AND message_id = ?"
                )
                .bind(self.bot_id, task.chat_id, task.message_id)
                .first()
            )
        if row is None:
            return None
        return {
            "chat": {"id": task.chat_id, "type": "supergroup"},
            "message_id": task.message_id,
            "from": {"id": int(row.sender_id), "is_bot": False},
        }
