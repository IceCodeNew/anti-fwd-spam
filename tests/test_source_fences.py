from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from anti_fwd_spam.evidence import ALLOW_REQUESTED, ALLOWED, RETENTION_SECONDS, ReportStore
from anti_fwd_spam.model import ModelConfig
from anti_fwd_spam.moderation import Moderator
from anti_fwd_spam.policy import Config
from anti_fwd_spam.tasks import ModelTasks, Task
from tests.test_model import StreamingResponse

if TYPE_CHECKING:
    from anti_fwd_spam.actions import AppResponse

NOW = 1_800_000_000
BOT_ID = 123
CONFIG = Config("123:token", "secret", frozenset({99}))
MODELS = (ModelConfig("https://model.invalid/decisions", "test", "test-key"),)


class Statement:
    def __init__(self, database: Database, sql: str) -> None:
        self.database, self.sql = database, sql
        self.parameters = ()

    def bind(self, *parameters: object) -> Statement:
        self.parameters = parameters
        return self

    async def first(self) -> SimpleNamespace | None:
        row = self.database.connection.execute(self.sql, self.parameters).fetchone()
        result = SimpleNamespace(**dict(row)) if row is not None else None
        if self.database.pause_allow and self.sql.startswith("SELECT update_id FROM reports"):
            self.database.pause_allow = False
            self.database.started.set()
            await self.database.release.wait()
        return result

    async def run(self) -> None:
        self.database.connection.execute(self.sql, self.parameters).fetchall()

    async def all(self) -> SimpleNamespace:
        rows = self.database.connection.execute(self.sql, self.parameters).fetchall()
        return SimpleNamespace(results=[SimpleNamespace(**dict(row)) for row in rows])


class Database:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        for path in sorted(migrations.glob("*.sql")):
            self.connection.executescript(path.read_text())
        self.pause_allow = False
        self.started, self.release = asyncio.Event(), asyncio.Event()

    def prepare(self, sql: str) -> Statement:
        return Statement(self, sql)

    async def batch(self, statements: list[Statement]) -> None:
        self.connection.execute("BEGIN")
        try:
            for statement in statements:
                await statement.run()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise


class Response(StreamingResponse):
    def __init__(self, payload: object, status: int = 200) -> None:
        async def read_body() -> bytes:
            return json.dumps(payload).encode()

        super().__init__(read_body, asyncio.Event())
        self.status = status


class Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.faults: set[str] = set()
        self.probability = 1
        self.messages = {80, 81}
        self.status = "member"

    async def __call__(self, url: str, *, method: str, headers: dict[str, str], body: str) -> Response:
        assert method == "POST"
        assert headers["content-type"] == "application/json"
        name = url.rsplit("/", 1)[1]
        parameters = json.loads(body)
        self.calls.append((name, parameters))
        if name in self.faults:
            return Response({"ok": False, "error_code": 429}, 429)
        if name == "decisions":
            return Response({"answers": [{"type": "predicate", "name": "spam", "probability": self.probability}]})
        result: object = True
        if name == "getChatMember":
            result = {"status": self.status, "user": {"id": parameters["user_id"]}}
        elif name == "getChat":
            result = {"id": 22, "type": "private", "first_name": "Bot", "username": "example_bot"}
        elif name == "deleteMessage":
            self.messages.discard(parameters["message_id"])
        elif name == "deleteMessages":
            self.messages.difference_update(parameters["message_ids"])
        elif name == "banChatMember":
            self.status = "kicked"
        elif name == "unbanChatMember":
            self.status = "member"
        return Response({"ok": True, "result": result})


def message(message_id: int = 81) -> dict[str, object]:
    return {
        "message_id": message_id,
        "date": NOW,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 22, "is_bot": True, "first_name": "Bot"},
        "text": "An unmatched offer for the model",
    }


def allow() -> dict[str, object]:
    return {
        "update_id": 10,
        "message": {
            **message(90),
            "from": {"id": 99, "is_bot": False, "first_name": "Reporter"},
            "text": "/allow 22",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }


def source_command(*, chat_id: int = -100, named: bool = False) -> dict[str, object]:
    update = {
        "update_id": 11,
        "message": {
            **message(91),
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": 99, "is_bot": False, "first_name": "Reporter"},
            "text": "/bs",
            "entities": [{"type": "bot_command", "offset": 0, "length": 3}],
            "reply_to_message": {**message(), "chat": {"id": chat_id, "type": "supergroup"}},
        },
    }

    if named:
        update["message"]["text"] = "/bs @example_bot"
        del update["message"]["reply_to_message"]
    return update


class SourceFenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.store = ReportStore(self.database)
        self.telegram = Telegram()
        self.tasks = ModelTasks(self.store, BOT_ID)
        await self.store.remember_message(BOT_ID, -100, 80, 22, NOW)

    async def dispatch(self, update: dict[str, object]) -> AppResponse:
        moderator = Moderator(CONFIG, self.telegram, self.store, "test_bot", MODELS)
        response = await moderator.process("application/json", json.dumps(update).encode())
        if moderator.background is not None:
            await moderator.background
        return response

    async def source_exists(self) -> bool:
        return bool(await self.store.matching_sources(frozenset({22})))

    async def pending_delete(self) -> SimpleNamespace:
        self.telegram.faults.add("deleteMessage")
        await self.dispatch({"update_id": 10, "message": message()})
        self.assertFalse(await self.source_exists())
        row = await self.database.prepare("SELECT * FROM model_tasks").first()
        assert row is not None
        self.assertEqual(row.phase, "delete")
        self.telegram.faults.clear()
        return row

    async def assert_no_escalation(self) -> None:
        names = [name for name, _ in self.telegram.calls]
        self.assertNotIn("banChatMember", names)
        self.assertNotIn("restrictChatMember", names)
        self.assertNotIn("deleteMessages", names)
        row = await self.database.prepare("SELECT history_removed FROM recent_messages WHERE message_id = 80").first()
        assert row is not None
        self.assertEqual(row.history_removed, 0)
        self.assertIn(80, self.telegram.messages)

    async def test_replayed_content_decision_only_retries_deletion(self) -> None:
        """user: Given a spam decision then a 429, When the update replays, Then it cannot ban or clean history."""
        pending = await self.pending_delete()
        await self.dispatch({"update_id": 10, "message": message()})
        await self.assert_no_escalation()
        with patch("time.time", return_value=pending.due_at):
            task = await self.tasks.claim(pending.due_at)
            assert task is not None
            await self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, pending.due_at)
        self.assertNotIn(81, self.telegram.messages)
        await self.assert_no_escalation()

    async def test_new_content_event_does_not_escalate(self) -> None:
        """user: Given bot spam, When a new benign message arrives, Then history and membership stay intact."""
        await self.dispatch({"update_id": 10, "message": message()})
        self.assertFalse(await self.source_exists())
        self.assertNotIn(81, self.telegram.messages)
        await self.assert_no_escalation()
        self.telegram.probability = 0
        await self.dispatch({"update_id": 11, "message": message(82)})
        await self.assert_no_escalation()

    async def test_incomplete_allow_cannot_remove_newer_source_decision(self) -> None:
        """user: Given an interrupted allow, When newer authorized bs registers, Then the old allow is superseded."""
        self.telegram.faults.add("getChatMember")
        self.assertEqual((await self.dispatch(allow())).status, 503)
        row = await self.database.prepare("SELECT moderation_result FROM reports WHERE update_id = 10").first()
        assert row is not None
        self.assertEqual(row.moderation_result, ALLOW_REQUESTED)
        self.telegram.faults.clear()
        await self.dispatch(source_command())
        self.assertTrue(await self.source_exists())
        calls = len(self.telegram.calls)
        response = await self.dispatch(allow())
        self.assertTrue(await self.source_exists())
        self.assertTrue(response.needs_confirmation)
        names = [name for name, _ in self.telegram.calls[calls:]]
        self.assertNotIn("unbanChatMember", names)
        self.assertNotIn("restrictChatMember", names)
        row = await self.database.prepare("SELECT moderation_result FROM reports WHERE update_id = 10").first()
        assert row is not None
        self.assertNotEqual(row.moderation_result, ALLOWED)

    async def test_completed_allow_replay_preserves_newer_registration(self) -> None:
        """user: Given a completed allow, When newer bs registers and allow replays, Then the source stays."""
        self.assertEqual((await self.dispatch(allow())).status, 200)
        await self.dispatch(source_command())
        self.assertTrue(await self.source_exists())
        calls = len(self.telegram.calls)
        self.assertEqual((await self.dispatch(allow())).status, 200)
        self.assertTrue(await self.source_exists())
        self.assertNotIn("getChatMember", [name for name, _ in self.telegram.calls[calls:]])

    async def deletion_task(self) -> Task:
        payload = {"target": message(), "update_id": 10, "mute": True}
        await self.tasks.enqueue(-100, 81, NOW, payload, NOW)
        task = await self.tasks.claim(NOW)
        assert task is not None
        self.assertTrue(await self.tasks.finish(task, "delete", NOW, action=payload))
        deletion = await self.tasks.claim(NOW)
        assert deletion is not None
        return deletion

    async def benign_edit(self) -> None:
        self.telegram.probability = 0
        edited = {**message(), "text": "Ordinary conversation", "edit_date": NOW}
        await self.dispatch({"update_id": 11, "edited_message": edited})
        row = await self.database.prepare("SELECT phase, revision FROM model_tasks").first()
        assert row is not None
        self.assertEqual((row.phase, row.revision), ("done", 11))

    async def test_edit_during_allow_read_cancels_deletion(self) -> None:
        """user: Given deletion pauses in can_act, When a benign edit completes, Then no stale deletion occurs."""
        task = await self.deletion_task()
        self.database.pause_allow = True
        running = asyncio.create_task(self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, NOW))
        try:
            await asyncio.wait_for(self.database.started.wait(), 2)
            await self.benign_edit()
        finally:
            self.database.release.set()
            await running
        self.assertFalse(await self.source_exists())
        self.assertIn(81, self.telegram.messages)
        await self.assert_no_escalation()

    async def test_edit_before_old_task_runs_prevents_deletion(self) -> None:
        """user: Given cancellation precedes stale task execution, When it runs, Then deletion is cancelled."""
        task = await self.deletion_task()
        await self.benign_edit()
        await self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, NOW)
        self.assertFalse(await self.source_exists())
        self.assertIn(81, self.telegram.messages)
        await self.assert_no_escalation()

    async def test_allow_sql_fences_preserve_newer_bs_in_another_group(self) -> None:
        """user: Given a newer cross-group bs, When old recovery claims or completes, Then neither write wins."""
        await self.store.save((BOT_ID, 10), json.dumps(allow()), NOW, 0)
        await self.store.pin_command_source(BOT_ID, 10, 22)
        await self.store.request_allow(BOT_ID, 10)
        # A repeated registration must protect an already-listed source too.
        await self.store.add_source(22, bot_id=BOT_ID, update_id=9)
        await self.dispatch(source_command(chat_id=-200, named=True))
        self.assertFalse(await self.store.claim_access_recovery(BOT_ID, 10))
        await self.database.prepare("UPDATE reports SET moderation_result = 'unbanned' WHERE update_id = 10").run()
        self.assertFalse(await self.store.complete_allow(BOT_ID, 22, 10))
        self.assertTrue(await self.source_exists())

    async def test_bs_after_recovery_claim_prevents_external_restoration(self) -> None:
        """user: Given recovery is claimed, When a newer named bs completes, Then old recovery does not unban."""
        claim = self.store.claim_access_recovery

        async def register_after_claim(bot_id: int, update_id: int) -> bool:
            claimed = await claim(bot_id, update_id)
            self.assertTrue(claimed)
            self.assertEqual((await self.dispatch(source_command(chat_id=-200, named=True))).status, 200)
            return claimed

        self.telegram.status = "kicked"
        with patch.object(self.store, "claim_access_recovery", side_effect=register_after_claim):
            response = await self.dispatch(allow())
        self.assertTrue(response.needs_confirmation)
        self.assertTrue(await self.source_exists())
        self.assertNotIn("unbanChatMember", [name for name, _ in self.telegram.calls])
        self.assertEqual(self.telegram.status, "kicked")

    async def test_newer_other_group_allow_is_not_a_source_command(self) -> None:
        """user: Given another group's newer recovery, When old recovery runs, Then it is not a bs fence."""
        newer = allow()
        newer["update_id"] = 11
        newer["message"]["chat"]["id"] = -200
        self.assertEqual((await self.dispatch(newer)).status, 200)
        response = await self.dispatch(allow())
        self.assertFalse(response.needs_confirmation)
        self.assertEqual(response.body, ALLOWED)

    async def test_expired_or_reclaimed_lease_cannot_delete(self) -> None:
        """user: Given expired or reclaimed ownership, When old deletion runs, Then it leaves the message."""
        task = await self.deletion_task()
        with patch("time.time", return_value=NOW + 30):
            await self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, NOW + 30)
        self.assertIn(81, self.telegram.messages)
        await self.database.prepare("UPDATE model_tasks SET generation = generation + 1").run()
        await self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, NOW)
        self.assertIn(81, self.telegram.messages)
        self.assertFalse(await self.source_exists())
        await self.assert_no_escalation()

    async def test_report_expiry_keeps_explicit_source(self) -> None:
        """user: Given an aged explicit registration, When evidence expires, Then the source remains."""
        await self.dispatch(source_command())
        await self.store.expire(NOW + RETENTION_SECONDS)
        self.assertTrue(await self.source_exists())
