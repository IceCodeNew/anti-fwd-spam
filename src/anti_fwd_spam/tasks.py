"""Persist bounded inference and moderation retries in the existing D1 database."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from .actions import Actions
from .constants import MAX_PROFILE_TEXT, SPAM_THRESHOLD
from .evidence import MESSAGE_WINDOW_SECONDS, RETENTION_SECONDS, storage_errors
from .model import (
    ModelRetryError,
    extract_profile_handles,
    public_profile,
    spam_probability,
    user_profile,
)
from .policy import matches_spam_text, parse_update

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
    subject_id: int = 0


async def _fetch_account_profiles(
    state: dict[str, object], identifiers: object, sender_id: int | None, fetcher: Fetch, token: str
) -> dict[str, str | None]:
    ids = identifiers if isinstance(identifiers, dict) else {}
    context = state.get("context")
    contact = state.get("contact")
    pending: list[tuple[str, int]] = []
    if sender_id is not None:
        pending.append(("sender", sender_id))
    if isinstance(context, dict) and type(ids.get("reply")) is int:
        pending.append(("reply", ids["reply"]))
    if isinstance(contact, dict) and type(ids.get("contact")) is int:
        pending.append(("contact", ids["contact"]))
    profiles = await asyncio.gather(*(user_profile(fetcher, token, identifier) for _, identifier in pending))
    bios: dict[str, str | None] = {}
    for (role, _), (nickname, biography) in zip(pending, profiles, strict=True):
        bios[role] = biography
        if role == "sender":
            state["bio"] = biography
        elif role == "reply" and isinstance(context, dict) and biography is not None:
            context["reply_bio"] = biography
        elif role == "contact" and isinstance(contact, dict):
            if nickname is not None:
                contact["nickname"] = nickname
            if biography is not None:
                contact["bio"] = biography
    return bios


async def _enrich_public_targets(
    state: dict[str, object], bios: dict[str, str | None], fetcher: Fetch, token: str
) -> None:
    candidates = [(role, handle) for role, bio in bios.items() for handle in extract_profile_handles(bio)]
    unique: list[tuple[str, str]] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    profiles = await asyncio.gather(*(public_profile(fetcher, token, handle) for _, handle in unique[:3]))
    destinations: dict[str, list[dict[str, str]]] = {}
    for (role, _), profile in zip(unique[:3], profiles, strict=True):
        if profile is not None:
            destinations.setdefault(role, []).append(profile)
    if destinations:
        state["profile_targets"] = destinations


async def enrich_profiles(
    state: dict[str, object], identifiers: object, sender_id: int | None, fetcher: Fetch, token: str
) -> None:
    """Fetch independent profiles and public bio targets concurrently within the task lease."""
    state["bio"] = None
    bios = await _fetch_account_profiles(state, identifiers, sender_id, fetcher, token)
    await _enrich_public_targets(state, bios, fetcher, token)


def deferred_regex_match(state: dict[str, object]) -> bool:
    """Match available profile fields only for messages without a reference."""
    if isinstance(state.get("context"), dict):
        return False
    contact = state.get("contact")
    values = [state.get("bio")]
    if isinstance(contact, dict):
        values.extend((contact.get("nickname"), contact.get("bio")))
    return any(matches_spam_text(value) for value in values)


class ModelTasks:
    """Share scheduling and retention between classification and delete-and-mute actions."""

    def __init__(self, store: ReportStore, bot_id: int) -> None:
        """Use this bot's D1 task namespace."""
        self.store = store
        self.database = store.database
        self.bot_id = bot_id

    async def enqueue(  # noqa: PLR0913 - Preserve the ordinary message API while adding an independent subject.
        self,
        chat_id: int,
        message_id: int,
        sent_at: int,
        state: dict[str, object] | None,
        now: int,
        *,
        subject_id: int = 0,
    ) -> bool:
        """Deduplicate deliveries; an edit also leaves a tombstone before any original delivery."""
        phase = "done" if state is None else "classify"
        with storage_errors():
            row = (
                await self.database.prepare(
                    "INSERT INTO model_tasks "
                    "(bot_id, chat_id, message_id, subject_id, phase, input_json, "
                    "due_at, stop_at, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(bot_id, chat_id, message_id, subject_id) DO UPDATE "
                    "SET phase = 'done', input_json = NULL, generation = generation + 1 "
                    "WHERE excluded.phase = 'done' RETURNING message_id",
                )
                .bind(
                    self.bot_id,
                    chat_id,
                    message_id,
                    subject_id,
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

    async def cancel_edit(
        self, target: tuple[int, int], sent_at: int, now: int, revision: int, *, referenced: bool
    ) -> None:
        """Fence older work once for each new Telegram edit."""
        with storage_errors():
            await (
                self.database.prepare(
                    "INSERT INTO model_tasks "
                    "(bot_id, chat_id, message_id, subject_id, phase, input_json, due_at, stop_at, "
                    "created_at, expires_at, revision, edit_pending) "
                    "VALUES (?, ?, ?, 0, 'done', NULL, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(bot_id, chat_id, message_id, subject_id) DO UPDATE SET "
                    "phase = 'done', input_json = NULL, attempts = 0, generation = generation + 1, "
                    "due_at = excluded.due_at, lease_until = 0, stop_at = excluded.stop_at, "
                    "expires_at = excluded.expires_at, revision = excluded.revision, "
                    "edit_pending = excluded.edit_pending WHERE excluded.revision > model_tasks.revision"
                )
                .bind(
                    self.bot_id,
                    *target,
                    now,
                    sent_at + MESSAGE_WINDOW_SECONDS,
                    now,
                    now + RETENTION_SECONDS,
                    revision,
                    referenced,
                )
                .run()
            )

    async def enqueue_edit(
        self, target: tuple[int, int], sent_at: int, state: dict[str, object], now: int, revision: int
    ) -> bool:
        """Classify a referenced edit only once after its older work was fenced."""
        with storage_errors():
            row = (
                await self.database.prepare(
                    "UPDATE model_tasks SET phase = 'classify', input_json = ?, attempts = 0, "
                    "due_at = ?, lease_until = 0, stop_at = ?, edit_pending = 0 "
                    "WHERE bot_id = ? AND chat_id = ? AND message_id = ? AND subject_id = 0 AND revision = ? "
                    "AND phase = 'done' AND edit_pending = 1 RETURNING message_id"
                )
                .bind(
                    json.dumps(state, ensure_ascii=False),
                    now,
                    sent_at + MESSAGE_WINDOW_SECONDS,
                    self.bot_id,
                    *target,
                    revision,
                )
                .first()
            )
        return row is not None

    async def claim(
        self,
        now: int,
        target: tuple[int, int] | None = None,
        *,
        inference: bool = True,
        subject_id: int = 0,
    ) -> Task | None:
        """Atomically claim one due stage and charge an attempt before the external request.

        Without inference, claim only deletions and unstarted biography checks; model retries stay paused.
        """
        with storage_errors():
            row = (
                await self.database.prepare(
                    "UPDATE model_tasks SET attempts = attempts + 1, generation = generation + 1, lease_until = ?, "
                    "due_at = ? + CASE attempts WHEN 0 THEN ? WHEN 1 THEN ? ELSE ? END "
                    "WHERE rowid = (SELECT rowid FROM model_tasks WHERE bot_id = ? AND phase != 'done' "
                    "AND due_at <= ? AND lease_until <= ? AND stop_at > ? AND expires_at > ? "
                    "AND attempts < ? AND (? IS NULL OR (chat_id = ? AND message_id = ? AND subject_id = ?)) "
                    "AND (? OR phase = 'delete' OR attempts = 0) "
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
                    subject_id,
                    inference,
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
                int(row.subject_id),
            )

    async def finish(
        self, task: Task, phase: str, now: int, *, retry: bool = False, action: dict[str, object] | None = None
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
                    "WHERE bot_id = ? AND chat_id = ? AND message_id = ? AND subject_id = ? AND generation = ? "
                    "AND phase = ? AND lease_until > ? AND stop_at > ? AND expires_at > ? RETURNING message_id",
                )
                .bind(
                    phase,
                    retry,
                    json.dumps(action) if phase == "delete" and action is not None else None,
                    retry,
                    due,
                    self.bot_id,
                    task.chat_id,
                    task.message_id,
                    task.subject_id,
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
        """Persist a profile or message decision before its independently leased moderation action."""
        payload = json.loads(task.input_json or "{}")
        snapshot = payload.get("target")
        target = snapshot if isinstance(snapshot, dict) else None
        joining = payload.get("joining") is True
        # Tasks saved by the previous release have no mute flag and keep the mute.
        mute = payload.get("mute") is not False
        action: dict[str, object] = {"target": target, "update_id": payload.get("update_id")}
        if joining:
            action.update({"joining": True, "update_json": payload["update_json"]})
        else:
            action["mute"] = mute

        async def can_act() -> bool:
            if not await self._owns(task, max(now, int(time.time()))):
                return False
            identifier = target["from"]["id"] if target is not None else None
            update_id = payload.get("update_id")
            return (
                identifier is None
                or (
                    await self.store.newer_allow(self.bot_id, identifier, update_id if type(update_id) is int else None)
                )
                is None
            )

        if task.phase == "classify":
            if not await can_act():
                await self.finish(task, "done", max(now, int(time.time())))
                return
            state = payload["state"]
            spam = await self._profile_match(payload, fetcher, token, can_act=can_act)
            if spam is False and models and await can_act():
                model = models[(task.attempts - 1) % len(models)]
                try:
                    probability = (
                        await spam_probability(fetcher, model, state, joining=True)
                        if joining
                        else await spam_probability(fetcher, model, state)
                    )
                except ModelRetryError as error:
                    retry = (error.retryable or task.attempts < len(models)) and await can_act()
                    await self.finish(task, "classify" if retry else "done", max(now, int(time.time())), retry=retry)
                    return
                spam = probability is not None and probability >= SPAM_THRESHOLD
            now = max(now, int(time.time()))
            phase = "delete" if spam and await can_act() else "done"
            if not await self.finish(task, phase, now, action=action) or phase == "done":
                return
            deletion = await self.claim(now, (task.chat_id, task.message_id), subject_id=task.subject_id)
            if deletion is None:
                return
            task = deletion
        retry = await self._moderate(payload, fetcher, token, can_act=can_act)
        await self.finish(task, "delete" if retry else "done", max(now, int(time.time())), retry=retry)

    async def _moderate(
        self, payload: dict[str, object], fetcher: Fetch, token: str, *, can_act: Callable[[], Awaitable[bool]]
    ) -> bool:
        target = payload.get("target")
        if not isinstance(target, dict):
            return False
        actions = Actions(fetcher, token, self.store)
        if payload.get("joining") is True:
            raw_json = payload.get("update_json")
            if not isinstance(raw_json, str):
                return False
            update = parse_update(raw_json)
            if update is None or not await can_act():
                return False
            response = await actions.ban_joiner(target, update, can_act=can_act)
        else:
            response = await actions.delete_and_mute(target, can_act=can_act, mute=payload.get("mute") is not False)
        return response.status == HTTPStatus.SERVICE_UNAVAILABLE

    async def _profile_match(
        self, payload: dict[str, object], fetcher: Fetch, token: str, *, can_act: Callable[[], Awaitable[bool]]
    ) -> bool | None:
        state = payload["state"]
        if not isinstance(state, dict):
            return False
        target = payload.get("target")
        sender = target["from"] if isinstance(target, dict) else None
        candidate = sender.get("id") if isinstance(sender, dict) else None
        identifier = candidate if type(candidate) is int else None
        if payload.get("joining") is True and isinstance(sender, dict) and identifier is not None:
            if await self.store.is_blacklisted(self.bot_id, identifier):
                return True
            if sender.get("is_bot") is True:
                return True if await self.store.matching_sources(frozenset({identifier})) else None
            if matches_spam_text(state.get("nickname")):
                return True
        if not await can_act():
            return False
        await enrich_profiles(state, payload.get("profile_ids"), identifier, fetcher, token)
        biography = state.get("bio")
        if payload.get("joining") is True and isinstance(biography, str):
            state["bio"] = biography[:MAX_PROFILE_TEXT]
        return deferred_regex_match(state)

    async def _owns(self, task: Task, now: int) -> bool:
        with storage_errors():
            row = await (
                self.database.prepare(
                    "SELECT 1 FROM model_tasks WHERE bot_id = ? AND chat_id = ? AND message_id = ? AND subject_id = ? "
                    "AND generation = ? AND phase = ? AND lease_until > ? AND stop_at > ? AND expires_at > ?"
                )
                .bind(
                    self.bot_id,
                    task.chat_id,
                    task.message_id,
                    task.subject_id,
                    task.generation,
                    task.phase,
                    now,
                    now,
                    now,
                )
                .first()
            )
        return row is not None
