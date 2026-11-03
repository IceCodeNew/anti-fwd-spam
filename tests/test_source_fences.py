from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from anti_fwd_spam.evidence import ALLOW_REQUESTED, ALLOWED, RETENTION_SECONDS, EvidenceError, ReportStore
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
            result = {"id": parameters["chat_id"], "type": "private", "first_name": "Bot"}
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
        self.assertTrue(await self.source_exists())
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
        """user: Given registration then a 429, When the update replays, Then it cannot ban or clean history."""
        pending = await self.pending_delete()
        await self.dispatch({"update_id": 10, "message": message()})
        await self.assert_no_escalation()
        with patch("time.time", return_value=pending.due_at):
            task = await self.tasks.claim(pending.due_at)
            assert task is not None
            await self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, pending.due_at)
        self.assertNotIn(81, self.telegram.messages)
        await self.assert_no_escalation()

    async def test_no_replay_keeps_content_action_and_new_event_uses_source(self) -> None:
        """user: Given no redelivery, When content is spam, Then only a new event can use source routing."""
        await self.dispatch({"update_id": 10, "message": message()})
        self.assertTrue(await self.source_exists())
        self.assertNotIn(81, self.telegram.messages)
        await self.assert_no_escalation()
        await self.dispatch({"update_id": 11, "message": message(82)})
        self.assertIn("banChatMember", [name for name, _ in self.telegram.calls])
        self.assertNotIn(80, self.telegram.messages)

    async def test_incomplete_allow_cannot_remove_newer_source_decision(self) -> None:
        """user: Given an interrupted allow, When newer bot spam registers, Then the old allow is superseded."""
        self.telegram.faults.add("getChatMember")
        self.assertEqual((await self.dispatch(allow())).status, 503)
        row = await self.database.prepare("SELECT moderation_result FROM reports WHERE update_id = 10").first()
        assert row is not None
        self.assertEqual(row.moderation_result, ALLOW_REQUESTED)
        self.telegram.faults.clear()
        await self.dispatch({"update_id": 11, "message": message()})
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
        """user: Given a completed allow, When newer spam registers and allow replays, Then the source stays."""
        self.assertEqual((await self.dispatch(allow())).status, 200)
        await self.dispatch({"update_id": 11, "message": message()})
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

    async def test_edit_during_allow_read_fences_source_insert(self) -> None:
        """user: Given an owned deletion pauses in can_act, When a benign edit completes, Then no source leaks."""
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

    async def test_edit_before_old_task_runs_prevents_registration(self) -> None:
        """user: Given cancellation precedes stale task execution, When it runs, Then registration stays absent."""
        task = await self.deletion_task()
        await self.benign_edit()
        await self.tasks.run(task, self.telegram, CONFIG.bot_token, MODELS, NOW)
        self.assertFalse(await self.source_exists())
        self.assertIn(81, self.telegram.messages)
        await self.assert_no_escalation()

    async def test_allow_sql_fences_survive_a_late_source_registration(self) -> None:
        """user: Given a newer source, When old recovery claims or completes directly, Then neither SQL write wins."""
        await self.store.save((BOT_ID, 10), json.dumps(allow()), NOW, 0)
        await self.store.pin_command_source(BOT_ID, 10, 22)
        await self.store.request_allow(BOT_ID, 10)
        await self.store.add_source(22, bot_id=BOT_ID, update_id=11)
        self.assertFalse(await self.store.claim_access_recovery(BOT_ID, 10))
        await self.database.prepare("UPDATE reports SET moderation_result = 'unbanned' WHERE update_id = 10").run()
        self.assertFalse(await self.store.complete_allow(BOT_ID, 22, 10))
        self.assertTrue(await self.source_exists())

    async def test_source_and_ledger_roll_back_together(self) -> None:
        """user: Given a failed source write, When registration retries, Then no orphan ledger changes routing."""
        self.database.connection.executescript(
            "CREATE TRIGGER reject_source BEFORE INSERT ON blacklisted_sources "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
        )
        with self.assertRaises(EvidenceError):
            await self.store.add_source(22, bot_id=BOT_ID, update_id=10)
        self.assertEqual(await self.store.introduced_sources(BOT_ID, 10), ())
        self.assertFalse(await self.source_exists())
        self.database.connection.execute("DROP TRIGGER reject_source")
        await self.store.add_source(22, bot_id=BOT_ID, update_id=10)
        await self.store.add_source(22, bot_id=BOT_ID, update_id=10)
        self.assertEqual(await self.store.introduced_sources(BOT_ID, 10), (22,))
        self.assertTrue(await self.source_exists())

    async def test_expired_or_reclaimed_lease_cannot_register(self) -> None:
        """user: Given expired or reclaimed ownership, When source insertion runs, Then it creates no decision."""
        task = await self.deletion_task()
        with patch("time.time", return_value=NOW + 30):
            await self.store.add_source(22, bot_id=BOT_ID, update_id=10, task=task)
        self.assertFalse(await self.source_exists())
        await self.database.prepare("UPDATE model_tasks SET generation = generation + 1").run()
        await self.store.add_source(22, bot_id=BOT_ID, update_id=10, task=task)
        self.assertFalse(await self.source_exists())
        self.assertEqual(await self.store.introduced_sources(BOT_ID, 10), ())

    async def test_ledger_expiry_keeps_the_long_lived_source(self) -> None:
        """user: Given an aged decision, When evidence expires, Then its replay fence expires but its source remains."""
        await self.store.add_source(22, bot_id=BOT_ID, update_id=10)
        await self.store.expire(NOW + RETENTION_SECONDS)
        self.assertEqual(await self.store.introduced_sources(BOT_ID, 10), ())
        self.assertTrue(await self.source_exists())
