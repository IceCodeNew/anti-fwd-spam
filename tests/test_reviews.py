from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from anti_fwd_spam.evidence import RETENTION_SECONDS, PendingReview, ReportStore
from anti_fwd_spam.moderation import Moderator
from anti_fwd_spam.policy import Config, ConfigError

if TYPE_CHECKING:
    import builtins

    from anti_fwd_spam.actions import AppResponse

NOW = 1_800_000_000
BOT_ID = 123
CONFIG = Config("123:token", "secret", frozenset({99}), "review-secret")
TARGET_TEXT = "Easy income opportunity, contact me to join today"


class Statement:
    def __init__(self, database: Database, sql: str) -> None:
        self.database, self.sql = database, sql
        self.parameters = ()

    def bind(self, *parameters: object) -> Statement:
        self.parameters = parameters
        return self

    async def first(self) -> SimpleNamespace | None:
        row = self.database.connection.execute(self.sql, self.parameters).fetchone()
        return SimpleNamespace(**dict(row)) if row is not None else None

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


class Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(payload).encode()

    async def bytes(self) -> builtins.bytes:
        return self.body


class Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.faults: set[str] = set()
        self.messages = {80, 81, 95}
        self.status = "member"

    async def __call__(self, url: str, *, method: str, headers: dict[str, str], body: str) -> Response:
        assert method == "POST"
        assert headers["content-type"] == "application/json"
        name = url.rsplit("/", 1)[1]
        parameters = json.loads(body)
        self.calls.append((name, parameters))
        if name in self.faults:
            return Response({"ok": False, "error_code": 429}, 429)
        if name == "getChatMember":
            return Response({"ok": True, "result": {"status": self.status, "user": {"id": parameters["user_id"]}}})
        if name == "deleteMessage" and parameters["message_id"] not in self.messages:
            return Response(
                {"ok": False, "error_code": 400, "description": "Bad Request: message to delete not found"}, 400
            )
        if name == "deleteMessage":
            self.messages.discard(parameters["message_id"])
        return Response({"ok": True, "result": True})

    def calls_named(self, name: str) -> list[dict[str, object]]:
        return [parameters for called, parameters in self.calls if called == name]


def target_message() -> dict[str, object]:
    return {
        "message_id": 81,
        "date": NOW,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 33, "is_bot": False, "first_name": "Spammer"},
        "text": TARGET_TEXT,
    }


def report_update(update_id: int = 500, reporter: int = 42) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 95,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": reporter, "is_bot": False, "first_name": "Member"},
            "text": "@test_bot spam",
            "entities": [{"type": "mention", "offset": 0, "length": 9}],
            "reply_to_message": target_message(),
        },
    }


def source_command_update(
    update_id: int = 501, reporter: int = 42, target: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 96,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": reporter, "is_bot": False, "first_name": "Member"},
            "text": "/bs",
            "entities": [{"type": "bot_command", "offset": 0, "length": 3}],
            "reply_to_message": target if target is not None else target_message(),
        },
    }


class ReviewQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.store = ReportStore(self.database)
        self.telegram = Telegram()
        self.moderator = Moderator(CONFIG, self.telegram, self.store, "test_bot", ())
        await self.store.remember_message(BOT_ID, -100, 80, 33, NOW)
        await self.store.remember_message(BOT_ID, -100, 81, 33, NOW)

    async def dispatch(self, update: dict[str, object]) -> AppResponse:
        response = await self.moderator.process("application/json", json.dumps(update).encode())
        if self.moderator.background is not None:
            await self.moderator.background
        return response

    async def queued(self) -> list[SimpleNamespace]:
        rows = await self.database.prepare("SELECT * FROM pending_reviews").all()
        return rows.results

    async def test_unlisted_report_is_queued_with_target_payload(self) -> None:
        """user: Given an unlisted member's reply report, When it is recorded, Then a review carries the target."""
        response = await self.dispatch(report_update())
        self.assertEqual(response.status, 200)
        rows = await self.queued()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row.chat_id, row.target_message_id, row.target_sender_id, row.reporter_id), (-100, 81, 33, 42)
        )
        payload = json.loads(row.payload_json)
        self.assertEqual(payload["target"]["state"]["message"]["text"], TARGET_TEXT)
        self.assertEqual(payload["target"]["state"]["nickname"], "Spammer")
        pending = await self.moderator.list_pending_reviews(10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["update_id"], 500)

    async def test_unlisted_bare_source_command_is_queued(self) -> None:
        """user: Given an unlisted bare /bs reply, When it is recorded, Then the same review queue receives it."""
        await self.dispatch(source_command_update())
        rows = await self.queued()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].update_id, 501)

    async def test_redelivery_does_not_duplicate_the_review(self) -> None:
        """user: Given a redelivered unlisted report, When recorded twice, Then one review stays queued."""
        await self.dispatch(report_update())
        await self.dispatch(report_update())
        self.assertEqual(len(await self.queued()), 1)

    async def test_listed_report_is_sampled_not_queued(self) -> None:
        """user: Given a listed reporter's report, When it moderates directly, Then it is kept as a sample."""
        await self.dispatch(report_update(reporter=99))
        self.assertEqual(await self.moderator.list_pending_reviews(10), [])
        samples = await self.moderator.list_review_samples(10)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["update_id"], 500)
        self.assertEqual(samples[0]["outcome"], "banned")
        row = (await self.queued())[0]
        self.assertEqual((row.verdict, row.outcome), ("spam", "banned"))
        self.assertIsNotNone(row.resolved_at)
        self.assertNotEqual(row.payload_json, "")
        payload = json.loads(row.payload_json)
        self.assertEqual(payload["target"]["state"]["message"]["text"], TARGET_TEXT)
        self.assertEqual(await self.store.review_samples(BOT_ID, NOW + RETENTION_SECONDS + 1, 10), [])

    async def test_listed_report_redelivery_does_not_duplicate_the_sample(self) -> None:
        """user: Given a redelivered listed report, When recorded twice, Then one sample stays stored."""
        await self.dispatch(report_update(reporter=99))
        await self.dispatch(report_update(reporter=99))
        self.assertEqual(len(await self.moderator.list_review_samples(10)), 1)

    async def test_listed_source_command_is_sampled_with_its_source(self) -> None:
        """user: Given a listed /bs on a bot message, When it moderates, Then the sample carries the source."""
        bot_target: dict[str, object] = {
            "message_id": 81,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 22, "is_bot": True, "first_name": "Source Bot"},
            "text": TARGET_TEXT,
        }
        await self.dispatch(source_command_update(reporter=99, target=bot_target))
        samples = await self.moderator.list_review_samples(10)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["outcome"], "banned")
        row = (await self.queued())[0]
        self.assertEqual(json.loads(row.payload_json)["source_id"], 22)

    async def test_resolved_unlisted_review_is_not_a_sample(self) -> None:
        """user: Given a resolved unlisted review, When samples are listed, Then its cleared payload stays out."""
        await self.dispatch(report_update())
        await self.moderator.resolve_review(500, "not_spam")
        self.assertEqual(await self.moderator.list_review_samples(10), [])

    async def test_resolve_not_spam_keeps_the_message(self) -> None:
        """user: Given a queued report, When the verdict is not spam, Then the message stays and the review closes."""
        await self.dispatch(report_update())
        response = await self.moderator.resolve_review(500, "not_spam")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram.calls_named("deleteMessage"), [])
        self.assertIn(81, self.telegram.messages)
        self.assertEqual(await self.moderator.list_pending_reviews(10), [])
        row = (await self.queued())[0]
        self.assertEqual((row.verdict, row.outcome), ("not_spam", "kept"))
        self.assertEqual(row.payload_json, "")

    async def test_resolve_spam_replays_the_listed_report_action(self) -> None:
        """user: Given a queued report, When the verdict is spam, Then the sender is banned like a listed report."""
        await self.dispatch(report_update())
        response = await self.moderator.resolve_review(500, "spam")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.telegram.calls_named("banChatMember"), [{"chat_id": -100, "user_id": 33, "until_date": 0}]
        )
        deleted = {call["message_id"] for call in self.telegram.calls_named("deleteMessage")}
        self.assertIn(81, deleted)
        self.assertIn(95, deleted)
        self.assertEqual(self.telegram.calls_named("deleteMessages"), [{"chat_id": -100, "message_ids": [80, 81]}])
        self.assertTrue(await self.store.is_blacklisted(BOT_ID, 33))
        row = (await self.queued())[0]
        self.assertEqual((row.verdict, row.outcome), ("spam", "banned"))
        again = await self.moderator.resolve_review(500, "spam")
        self.assertIn("already resolved", again.body)
        self.assertEqual(len(self.telegram.calls_named("banChatMember")), 1)

    async def test_resolve_spam_on_an_administrator_deletes_the_target_without_a_ban(self) -> None:
        """user: Given a report on an administrator, When the verdict is spam, Then only the target is deleted."""
        self.telegram.status = "administrator"
        await self.dispatch(report_update())
        response = await self.moderator.resolve_review(500, "spam")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])
        self.assertIn(81, {call["message_id"] for call in self.telegram.calls_named("deleteMessage")})
        self.assertFalse(await self.store.is_blacklisted(BOT_ID, 33))
        row = (await self.queued())[0]
        self.assertEqual(row.outcome, "deleted")

    async def test_resolve_spam_bans_the_sender_when_the_target_is_already_absent(self) -> None:
        """user: Given the target vanished first, When the verdict is spam, Then the sender is still banned."""
        self.telegram.messages.discard(81)
        await self.dispatch(report_update())
        response = await self.moderator.resolve_review(500, "spam")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.telegram.calls_named("banChatMember"), [{"chat_id": -100, "user_id": 33, "until_date": 0}]
        )
        row = (await self.queued())[0]
        self.assertEqual(row.outcome, "banned")

    async def test_resolve_spam_retries_after_a_temporary_failure(self) -> None:
        """user: Given a temporary delete failure, When resolving, Then the review stays pending for a retry."""
        await self.dispatch(report_update())
        self.telegram.faults.add("deleteMessage")
        response = await self.moderator.resolve_review(500, "spam")
        self.assertEqual(response.status, 503)
        self.assertEqual(len(await self.moderator.list_pending_reviews(10)), 1)
        self.telegram.faults.clear()
        response = await self.moderator.resolve_review(500, "spam")
        self.assertEqual(response.status, 200)
        self.assertNotIn(81, self.telegram.messages)
        self.assertEqual(len(self.telegram.calls_named("banChatMember")), 1)
        self.assertTrue(await self.store.is_blacklisted(BOT_ID, 33))

    async def test_resolve_spam_is_superseded_by_a_newer_allow(self) -> None:
        """user: Given a newer /allow for the sender, When the verdict is spam, Then the deletion is fenced off."""
        await self.dispatch(report_update())
        await self.store.save((BOT_ID, 999), json.dumps(report_update(999)), NOW, 0)
        await self.store.pin_command_source(BOT_ID, 999, 33)
        await self.store.request_allow(BOT_ID, 999)
        response = await self.moderator.resolve_review(500, "spam")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram.calls_named("deleteMessage"), [])
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])
        row = (await self.queued())[0]
        self.assertEqual(row.outcome, "superseded_by_allow")

    async def test_resolve_spam_registers_a_bare_bs_source(self) -> None:
        """user: Given an unlisted bare /bs on a bot message, When spam, Then its source is registered and banned."""
        bot_target: dict[str, object] = {
            "message_id": 81,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 22, "is_bot": True, "first_name": "Source Bot"},
            "text": TARGET_TEXT,
        }
        await self.dispatch(source_command_update(target=bot_target))
        rows = await self.queued()
        self.assertEqual(json.loads(rows[0].payload_json)["source_id"], 22)
        response = await self.moderator.resolve_review(501, "spam")
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.store.matching_sources(frozenset({22})), (22,))
        self.assertIn({"chat_id": -100, "user_id": 22, "until_date": 0}, self.telegram.calls_named("banChatMember"))
        row = (await self.queued())[0]
        self.assertEqual(row.outcome, "banned")

    async def test_resolve_spam_registers_a_bare_bs_sticker_set(self) -> None:
        """user: Given an unlisted bare /bs on a sticker, When spam, Then its set is registered like a listed /bs."""
        sticker_target: dict[str, object] = {
            "message_id": 81,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 33, "is_bot": False, "first_name": "Spammer"},
            "sticker": {"set_name": "Spam_Set", "file_unique_id": "stk1", "emoji": "x"},
        }
        await self.dispatch(source_command_update(target=sticker_target))
        response = await self.moderator.resolve_review(501, "spam")
        self.assertEqual(response.status, 200)
        self.assertTrue(await self.store.is_sticker_blacklisted(BOT_ID, "spam_set", whole_set=True))
        self.assertIn({"chat_id": -100, "user_id": 33, "until_date": 0}, self.telegram.calls_named("banChatMember"))

    async def test_resolve_rejects_unknown_reviews_and_verdicts(self) -> None:
        """user: Given an unknown review or verdict, When resolving, Then the request is rejected without action."""
        unknown = await self.moderator.resolve_review(424242, "spam")
        self.assertEqual(unknown.status, 404)
        self.assertEqual(unknown.body, "review not found")
        await self.dispatch(report_update())
        self.assertEqual((await self.moderator.resolve_review(500, "maybe")).status, 400)
        self.assertEqual(len(await self.moderator.list_pending_reviews(10)), 1)

    async def test_expired_reviews_leave_the_queue(self) -> None:
        """user: Given a review past its retention, When expiry runs, Then it no longer awaits a verdict."""
        await self.store.enqueue_review(
            BOT_ID,
            PendingReview(
                update_id=700,
                chat_id=-100,
                target_message_id=81,
                target_sender_id=33,
                reporter_id=42,
                payload_json="{}",
                created_at=NOW - RETENTION_SECONDS - 1,
            ),
        )
        self.assertEqual(await self.store.pending_reviews(BOT_ID, NOW, 10), [])
        await self.store.expire(NOW)
        self.assertEqual(await self.queued(), [])


class ReviewConfigurationTests(unittest.TestCase):
    def test_review_secret_is_optional(self) -> None:
        """user: Given no review secret, When configured, Then the review endpoints stay disabled."""
        config = Config.from_values(bot_token="123:token", webhook_secret="secret")
        self.assertIsNone(config.review_secret)
        configured = Config.from_values(bot_token="123:token", webhook_secret="secret", review_secret="review-1")
        self.assertEqual(configured.review_secret, "review-1")

    def test_review_secret_rejects_unsupported_values(self) -> None:
        """user: Given a malformed review secret, When configured, Then startup fails explicitly."""
        for value in ("bad secret", "sécret", "x" * 257, 42):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_values(bot_token="123:token", webhook_secret="secret", review_secret=value)


if __name__ == "__main__":
    unittest.main()
