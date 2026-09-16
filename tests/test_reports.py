from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from anti_fwd_spam.app import AppResponse, handle_update
from anti_fwd_spam.evidence import ReportStore, classify_message
from anti_fwd_spam.moderation import Moderator, mentions_bot
from anti_fwd_spam.policy import Config

if TYPE_CHECKING:
    from anti_fwd_spam.telegram import TelegramResponse

BOT_USERNAME = "niuqu_icn_bot"
BOT_TOKEN = f"{123}:test-token"
WEBHOOK_SECRET = "test-secret"  # noqa: S105
CONFIG = Config.from_values(bot_token=BOT_TOKEN, webhook_secret=WEBHOOK_SECRET)
CHAT = {"id": -10012, "type": "supergroup"}


class SQLiteD1:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[1] / "migrations/0001_reports.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql: str) -> Statement:
        return Statement(self.connection, sql)


class Statement:
    def __init__(self, connection, sql):
        self.connection, self.sql, self.args = connection, sql, ()

    def bind(self, *args: object) -> Statement:
        self.args = args
        return self

    async def run(self) -> SimpleNamespace:
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return SimpleNamespace(meta=SimpleNamespace(changes=cursor.rowcount))

    async def first(self) -> SimpleNamespace | None:
        row = self.connection.execute(self.sql, self.args).fetchone()
        return SimpleNamespace(**dict(row)) if row is not None else None


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.statuses = {11: "administrator", 22: "member"}
        self.fail_method = None
        self.error_code = 429
        self.result_overrides: dict[str, list[object]] = {}
        self.before_call = None

    async def __call__(self, url: str, **kwargs: object) -> TelegramResponse:
        method = url.rsplit("/", 1)[-1]
        params = json.loads(str(kwargs["body"]))
        if self.before_call is not None:
            self.before_call(method, params)
        self.calls.append((method, params))
        if method == self.fail_method:
            payload, status = {"ok": False, "error_code": self.error_code}, self.error_code
        else:
            overrides = self.result_overrides.get(method, [])
            if overrides:
                result = overrides.pop(0)
            elif method == "getChatMember":
                result = {"status": self.statuses[params["user_id"]], "user": {"id": params["user_id"]}}
            else:
                result = True
            payload, status = {"ok": True, "result": result}, 200

        async def read_bytes() -> bytes:
            return json.dumps(payload).encode()

        return cast("TelegramResponse", SimpleNamespace(status=status, bytes=read_bytes))


def report_update() -> dict[str, Any]:
    target = {
        "message_id": 81,
        "chat": dict(CHAT),
        "date": 1,
        "from": {"id": 22, "is_bot": False},
        "animation": {"file_id": "a", "file_unique_id": "unique-a"},
        "document": {"file_id": "a"},
        "caption": "垃圾链接",
        "caption_entities": [{"type": "text_link", "url": "https://example.com"}],
        "effect_id": "effect-1",
        "future_field": {"nested": [False, None, {"未知": 42}]},
    }
    return {
        "update_id": 71,
        "message": {
            "message_id": 82,
            "date": 2,
            "chat": dict(CHAT),
            "from": {"id": 11, "is_bot": False},
            "reply_to_message": target,
            "text": "😀 @niuqu_icn_bot",
            "entities": [{"type": "mention", "offset": 3, "length": 14}],
        },
    }


class MentionTests(unittest.TestCase):
    def test_both_entity_kinds_match_username_not_id_or_display_name(self) -> None:
        message = report_update()["message"]
        self.assertTrue(mentions_bot(message, BOT_USERNAME.upper()))
        message["entities"] = [{"type": "text_mention", "user": {"id": 999, "username": BOT_USERNAME.upper()}}]
        self.assertTrue(mentions_bot(message, BOT_USERNAME))
        for account in ({"id": 123}, {"id": 123, "username": "other_bot"}, {"first_name": BOT_USERNAME}):
            message["entities"] = [{"type": "text_mention", "user": account}]
            self.assertFalse(mentions_bot(message, BOT_USERNAME))

    def test_exact_utf16_spans_caption_and_invalid_entities(self) -> None:
        message = report_update()["message"]
        entity = message["entities"][0]
        for replacement in ({"offset": 2}, {"length": 12}, {"length": 99}, {"offset": True}, {"type": "text_link"}):
            candidate = {**message, "entities": [{**entity, **replacement}]}
            self.assertFalse(mentions_bot(candidate, BOT_USERNAME))
        self.assertFalse(mentions_bot({"text": "@niuqu_icn_bot"}, BOT_USERNAME))
        self.assertFalse(
            mentions_bot(
                {"text": "@niuqu_icn_bot_extra", "entities": [{"type": "mention", "offset": 0, "length": 19}]},
                BOT_USERNAME,
            )
        )
        self.assertTrue(mentions_bot({"caption": message["text"], "caption_entities": [entity]}, BOT_USERNAME))


class ReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = SQLiteD1()
        self.addCleanup(self.db.connection.close)
        self.store = ReportStore(self.db)
        self.telegram = FakeTelegram()
        self.moderator = Moderator(CONFIG, self.telegram, self.store, BOT_USERNAME)

    async def dispatch(self, update: dict[str, Any]) -> AppResponse:
        return await handle_update(
            content_type="application/json",
            body=json.dumps(update, ensure_ascii=False).encode(),
            config=CONFIG,
            process_update=self.moderator.process,
        )

    async def test_admin_report_saves_full_json_then_deletes_and_mutes_only_target(self) -> None:
        update = report_update()
        raw_update = json.dumps(update, ensure_ascii=False)

        def assert_evidence_precedes_network(_method: str, _params: dict[str, object]) -> None:
            row = self.db.connection.execute("SELECT raw_update FROM reports").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["raw_update"], raw_update)

        self.telegram.before_call = assert_evidence_precedes_network
        response = await self.dispatch(update)
        self.assertEqual((response.status, response.body), (200, "deleted; muted"))
        self.assertEqual(
            [call[0] for call in self.telegram.calls],
            ["getChatMember", "deleteMessage", "getChatMember", "restrictChatMember"],
        )
        self.assertEqual(self.telegram.calls[1][1], {"chat_id": CHAT["id"], "message_id": 81})
        restriction = self.telegram.calls[-1][1]
        self.assertEqual(
            (restriction["user_id"], restriction["until_date"], restriction["use_independent_chat_permissions"]),
            (22, 0, True),
        )
        self.assertTrue(all(value is False for value in restriction["permissions"].values()))
        row = self.db.connection.execute("SELECT * FROM reports").fetchone()
        self.assertEqual(json.loads(row["raw_update"]), update)
        self.assertEqual(json.loads(row["classification"])["content_types"], ["animation", "document"])
        self.assertEqual(row["expires_at"] - row["received_at"], 259200)
        self.telegram.calls.clear()
        self.assertEqual(await self.dispatch(update), response)
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 1)

    async def test_full_raw_json_preserves_rich_message_media_and_unknown_fields(self) -> None:
        update = report_update()
        target = update["message"]["reply_to_message"]
        target["rich_message"] = {
            "blocks": [
                {"type": "photo", "photo": [{"file_id": "photo", "width": 320, "future_photo_field": {"x": 1}}]},
                {"type": "video", "video": {"file_id": "video", "duration": 9, "future_video_field": [1, None]}},
            ],
            "unknown_rich_field": {"nested": True},
        }
        update["message"]["future_report_field"] = {"opaque": ["kept"]}

        await self.dispatch(update)

        row = self.db.connection.execute("SELECT raw_update, classification FROM reports").fetchone()
        self.assertEqual(json.loads(row["raw_update"]), update)
        self.assertIn("rich_message", json.loads(row["classification"])["content_types"])

    async def test_member_reports_record_evidence_without_moderation(self) -> None:
        self.telegram.statuses[11] = "member"
        response = await self.dispatch(report_update())
        self.assertEqual(response.body, "report recorded")
        self.assertEqual([call[0] for call in self.telegram.calls], ["getChatMember"])
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 1)

    async def test_edited_text_mention_report_uses_account_username(self) -> None:
        update = report_update()
        message = update.pop("message")
        message["entities"] = [{"type": "text_mention", "user": {"id": 999, "username": BOT_USERNAME}}]
        update["edited_message"] = message
        self.telegram.statuses[11] = "member"
        self.assertEqual((await self.dispatch(update)).body, "report recorded")
        row = self.db.connection.execute("SELECT raw_update FROM reports").fetchone()
        self.assertEqual(json.loads(row[0]), update)
        self.assertEqual([call[0] for call in self.telegram.calls], ["getChatMember"])

    async def test_owner_report_can_moderate_but_target_admins_cannot_be_muted(self) -> None:
        self.telegram.statuses[11] = "creator"
        for target_status in ("creator", "administrator"):
            self.telegram.statuses[22] = target_status
            update = report_update()
            update["update_id"] += len(self.telegram.calls)
            response = await self.dispatch(update)
            self.assertEqual(response.body, "deleted; mute skipped")
        self.assertNotIn("restrictChatMember", [call[0] for call in self.telegram.calls])

    async def test_automatic_moderation_protects_administrators(self) -> None:
        self.telegram.statuses[22] = "administrator"
        update = report_update()
        update["message"] = update["message"]["reply_to_message"]
        update["message"]["via_bot"] = {"id": 273_234_066, "is_bot": True}

        response = await self.dispatch(update)

        self.assertEqual(response.body, "deleted; mute skipped")
        self.assertEqual([call[0] for call in self.telegram.calls], ["deleteMessage", "getChatMember"])

    async def test_automatic_moderation_restricts_left_nonmember_commenters(self) -> None:
        self.telegram.statuses[22] = "left"
        update = report_update()
        update["message"] = update["message"]["reply_to_message"]
        update["message"]["via_bot"] = {"id": 273_234_066, "is_bot": True}

        response = await self.dispatch(update)

        self.assertEqual(response.body, "deleted; muted")
        self.assertEqual(
            [call[0] for call in self.telegram.calls],
            ["deleteMessage", "getChatMember", "restrictChatMember"],
        )

    async def test_anonymous_sender_is_not_resolved_to_fake_user(self) -> None:
        update = report_update()
        update["message"]["reply_to_message"]["sender_chat"] = CHAT
        self.assertEqual((await self.dispatch(update)).body, "deleted; mute skipped")
        self.assertEqual([call[0] for call in self.telegram.calls], ["getChatMember", "deleteMessage"])

    async def test_storage_failure_prevents_report_actions_but_not_automatic_rules(self) -> None:
        self.moderator.store = ReportStore(None)
        self.assertEqual((await self.dispatch(report_update())).status, 503)
        self.assertEqual(self.telegram.calls, [])
        update = report_update()
        update["message"] = update["message"]["reply_to_message"]
        update["message"]["via_bot"] = {"id": 273234066, "is_bot": True}
        self.assertEqual((await self.dispatch(update)).body, "deleted; muted")

    async def test_failure_is_retryable_and_role_is_rechecked(self) -> None:
        self.telegram.fail_method = "restrictChatMember"
        self.assertEqual((await self.dispatch(report_update())).status, 503)
        row = self.db.connection.execute("SELECT response_status, response_body FROM reports").fetchone()
        self.assertIsNone(row["response_status"])
        self.assertEqual(row["response_body"], "deleted; mute failed")
        self.telegram.calls.clear()
        self.telegram.fail_method = None
        self.telegram.statuses[22] = "administrator"
        self.assertEqual((await self.dispatch(report_update())).body, "deleted; mute skipped")
        self.assertNotIn("restrictChatMember", [call[0] for call in self.telegram.calls])

    async def test_deletion_failure_prevents_mute(self) -> None:
        for code, expected_status in ((400, 200), (403, 200), (429, 503), (500, 503)):
            with self.subTest(code=code):
                update = report_update()
                update["update_id"] += code
                self.telegram.calls.clear()
                self.telegram.fail_method = "deleteMessage"
                self.telegram.error_code = code
                response = await self.dispatch(update)
                self.assertEqual(response.status, expected_status)
                self.assertEqual([call[0] for call in self.telegram.calls], ["getChatMember", "deleteMessage"])

    async def test_false_restriction_result_never_marks_report_completed(self) -> None:
        self.telegram.result_overrides["restrictChatMember"] = [False]
        self.assertEqual((await self.dispatch(report_update())).status, 503)
        self.assertIsNone(self.db.connection.execute("SELECT response_status FROM reports").fetchone()[0])

    async def test_failed_reporter_lookup_preserves_evidence_without_deletion(self) -> None:
        self.telegram.fail_method = "getChatMember"
        self.assertEqual((await self.dispatch(report_update())).status, 503)
        self.assertEqual([call[0] for call in self.telegram.calls], ["getChatMember"])
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 1)

    async def test_malformed_or_mismatched_target_membership_prevents_mute(self) -> None:
        malformed_results = ({}, {"status": "member", "user": {"id": 999}}, {"status": "unknown", "user": {"id": 22}})
        for index, malformed in enumerate(malformed_results):
            with self.subTest(result=malformed):
                update = report_update()
                update["update_id"] += index
                self.telegram.result_overrides["getChatMember"] = [
                    {"status": "administrator", "user": {"id": 11}},
                    malformed,
                ]
                response = await self.dispatch(update)
                self.assertEqual((response.status, response.body), (503, "deleted; mute failed"))
        self.assertNotIn("restrictChatMember", [call[0] for call in self.telegram.calls])

    async def test_cross_chat_and_malformed_targets_never_act(self) -> None:
        for bad_id in (-10013, True, "-10012"):
            update = copy.deepcopy(report_update())
            update["message"]["reply_to_message"]["chat"]["id"] = bad_id
            self.assertEqual((await self.dispatch(update)).status, 400)
        self.assertEqual(self.telegram.calls, [])

    async def test_late_duplicate_cannot_overwrite_completed_report(self) -> None:
        update = report_update()
        raw = json.dumps(update)
        target = update["message"]["reply_to_message"]
        other = ReportStore(self.db)
        self.assertIsNone(await self.store.save(123, 71, raw, target, 100))
        self.assertIsNone(await other.save(123, 71, raw, target, 101))
        await self.store.finish(123, 71, 200, "deleted; muted")
        for status, body in ((503, "deleted; mute failed"), (200, "report recorded")):
            with self.subTest(status=status):
                await other.finish(123, 71, status, body)
                self.assertEqual(await self.store.save(123, 71, raw, target, 102), (200, "deleted; muted"))

    async def test_retention_boundary_and_duplicate_do_not_extend_lifetime(self) -> None:
        update = report_update()
        target = update["message"]["reply_to_message"]
        await self.store.save(123, 1, json.dumps(update), target, 100)
        await self.store.save(123, 1, json.dumps(update), target, 150)
        await self.store.save(123, 2, json.dumps(update), target, 101)
        await self.store.expire(259299)
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 2)
        await self.store.expire(259300)
        self.assertEqual([row[0] for row in self.db.connection.execute("SELECT update_id FROM reports")], [2])

    async def test_retention_deletes_at_most_1000_rows_and_honors_boundaries(self) -> None:
        update = report_update()
        target = update["message"]["reply_to_message"]
        raw_update = json.dumps(update)
        for update_id in range(1002):
            await self.store.save(123, update_id, raw_update, target, update_id)

        await self.store.expire(260_200)
        remaining = [row[0] for row in self.db.connection.execute("SELECT update_id FROM reports ORDER BY update_id")]
        self.assertEqual(remaining, [1000, 1001])
        await self.store.expire(260_200)
        self.assertEqual([row[0] for row in self.db.connection.execute("SELECT update_id FROM reports")], [1001])
        await self.store.expire(260_201)
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 0)


class ClassificationTests(unittest.TestCase):
    def test_multiple_media_fields_and_unknown_nested_data_remain_available(self) -> None:
        message = report_update()["message"]["reply_to_message"]
        original = copy.deepcopy(message)
        classification = classify_message(message)
        self.assertEqual(classification["media_fields"], ["animation", "document"])
        self.assertIn("future_field", cast("list[str]", classification["present_fields"]))
        self.assertEqual(message, original)

    def test_non_text_content_and_service_types_are_classified(self) -> None:
        for kind in (
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
            "gift",
            "unique_gift",
            "invoice",
            "successful_payment",
            "new_chat_members",
            "forum_topic_created",
            "video_chat_started",
            "web_app_data",
            "giveaway",
        ):
            with self.subTest(kind=kind):
                self.assertEqual(classify_message({kind: {}, "via_bot": {"id": 7}})["content_types"], [kind])


if __name__ == "__main__":
    unittest.main()
