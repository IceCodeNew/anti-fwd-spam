from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from anti_fwd_spam.evidence import ReportStore
from anti_fwd_spam.moderation import Moderator
from anti_fwd_spam.policy import only_mentions_bot
from tests.test_reviews import CONFIG, NOW, Database, Telegram


def mention_update(
    update_id: int, message_id: int, text: str, sender: int = 42, first_name: str = "Member"
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": sender, "is_bot": False, "first_name": first_name},
            "text": text,
        },
    }


class OnlyMentionsBotTests(unittest.TestCase):
    def test_sole_mentions_of_this_bot(self) -> None:
        for text in ("@test_bot", " @test_bot ", "@test_bot @TEST_BOT", "@test_bot\n@test_bot"):
            with self.subTest(text=text):
                self.assertTrue(only_mentions_bot({"text": text}, "test_bot"))
        self.assertTrue(only_mentions_bot({"caption": "@test_bot"}, "test_bot"))

    def test_anything_else_is_not_a_sole_mention(self) -> None:
        for text in (
            "@other_bot",
            "@test_bot @other_bot",
            "@test_bot hello",
            "hello @test_bot",
            "@test_bot2",
            "",
            "   ",
        ):
            with self.subTest(text=text):
                self.assertFalse(only_mentions_bot({"text": text}, "test_bot"))
        self.assertFalse(only_mentions_bot({}, "test_bot"))
        self.assertFalse(only_mentions_bot({"text": "@test_bot", "caption": "spam"}, "test_bot"))


class SelfMentionModerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.store = ReportStore(self.database)
        self.telegram = Telegram()
        self.moderator = Moderator(CONFIG, self.telegram, self.store, "test_bot", ())

    async def _deliver(self, update: dict[str, object]) -> int:
        response = await self.moderator.process("application/json", json.dumps(update).encode())
        if self.moderator.background is not None:
            await self.moderator.background
            self.moderator.background = None
        return response.status

    async def test_member_who_only_mentions_this_bot_is_not_muted(self) -> None:
        self.telegram.messages.add(97)
        status = await self._deliver(mention_update(600, 97, "@test_bot"))
        self.assertEqual(status, 200)
        self.assertEqual(self.telegram.calls_named("deleteMessage"), [])
        self.assertEqual(self.telegram.calls_named("restrictChatMember"), [])
        self.assertIn(97, self.telegram.messages)

    async def test_member_who_only_mentions_another_bot_is_muted(self) -> None:
        self.telegram.messages.add(98)
        status = await self._deliver(mention_update(601, 98, "@other_bot"))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.telegram.calls_named("deleteMessage")), 1)
        restricts = self.telegram.calls_named("restrictChatMember")
        self.assertEqual(len(restricts), 1)
        self.assertEqual(restricts[0]["user_id"], 42)

    async def test_self_mention_with_spam_display_name_is_still_muted(self) -> None:
        self.telegram.messages.add(101)
        update = mention_update(602, 101, "@test_bot", first_name="💰💰💰💰")
        status = await self._deliver(update)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.telegram.calls_named("deleteMessage")), 1)
        restricts = self.telegram.calls_named("restrictChatMember")
        self.assertEqual(len(restricts), 1)
        self.assertEqual(restricts[0]["user_id"], 42)
