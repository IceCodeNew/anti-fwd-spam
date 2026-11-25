from __future__ import annotations

import json
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

from anti_fwd_spam.evidence import PendingReview, ReportStore
from anti_fwd_spam.moderation import Moderator
from tests.test_reviews import (
    BOT_ID,
    CONFIG,
    NOW,
    TARGET_TEXT,
    Database,
    Response,
    Telegram,
    report_update,
    source_command_update,
)

if TYPE_CHECKING:
    from anti_fwd_spam.actions import AppResponse


class StatusTelegram(Telegram):
    """Telegram fake with per-user member statuses and username resolution."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[int, str] = {}
        self.accounts: dict[str, int] = {}

    async def __call__(self, url: str, *, method: str, headers: dict[str, str], body: str) -> Response:
        name = url.rsplit("/", 1)[1]
        parameters = json.loads(body)
        if name == "getChatMember":
            self.calls.append((name, parameters))
            status = self.statuses.get(parameters["user_id"], "member")
            return Response({"ok": True, "result": {"status": status, "user": {"id": parameters["user_id"]}}})
        if name == "getChat":
            self.calls.append((name, parameters))
            chat_id = parameters["chat_id"]
            if isinstance(chat_id, str) and chat_id.startswith("@") and chat_id[1:] in self.accounts:
                username = chat_id[1:]
                account = {"id": self.accounts[username], "type": "private", "username": username}
                return Response({"ok": True, "result": account})
            return Response({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, 400)
        return await super().__call__(url, method=method, headers=headers, body=body)


def own_target(sender: int) -> dict[str, object]:
    return {
        "message_id": 81,
        "date": NOW,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": sender, "is_bot": False, "first_name": "Member"},
        "text": TARGET_TEXT,
    }


def self_report_update(update_id: int, reporter: int) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 95,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": reporter, "is_bot": False, "first_name": "Member"},
            "text": "@test_bot spam",
            "entities": [{"type": "mention", "offset": 0, "length": 9}],
            "reply_to_message": own_target(reporter),
        },
    }


def allow_update(update_id: int, message_id: int, sender: int) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": NOW,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": sender, "is_bot": False, "first_name": "Member"},
            "text": "/allow 42",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }


class ModerationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.store = ReportStore(self.database)
        self.telegram = StatusTelegram()
        self.moderator = Moderator(CONFIG, self.telegram, self.store, "test_bot", ())
        await self.store.remember_message(BOT_ID, -100, 80, 33, NOW)
        await self.store.remember_message(BOT_ID, -100, 81, 33, NOW)

    async def dispatch(self, update: dict[str, object]) -> AppResponse:
        response = await self.moderator.process("application/json", json.dumps(update).encode())
        if self.moderator.background is not None:
            await self.moderator.background
            self.moderator.background = None
        return response

    async def queued_count(self) -> int:
        rows = await self.database.prepare("SELECT * FROM pending_reviews").all()
        return len(rows.results)

    def restored(self) -> list[dict[str, object]]:
        result = []
        for parameters in self.telegram.calls_named("restrictChatMember"):
            permissions = parameters["permissions"]
            if (
                parameters["user_id"] == 42
                and isinstance(permissions, dict)
                and permissions.get("can_send_messages") is True
            ):
                result.append(parameters)
        return result

    def reply_texts(self) -> list[str]:
        texts = []
        for parameters in self.telegram.calls_named("sendMessage"):
            text = parameters["text"]
            if isinstance(text, str):
                texts.append(text)
        return texts

    async def test_unlisted_self_report_is_not_queued(self) -> None:
        """user: Given an unlisted member reports their own message, When it is recorded, Then nothing is queued."""
        response = await self.dispatch(self_report_update(700, 42))
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.queued_count(), 0)
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])

    async def test_listed_self_report_does_not_ban(self) -> None:
        """user: Given a listed reporter reports their own message, When it is handled, Then they are not banned."""
        response = await self.dispatch(self_report_update(701, 99))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])
        self.assertEqual(self.telegram.calls_named("deleteMessage"), [])
        self.assertIn(81, self.telegram.messages)

    async def test_listed_report_of_another_member_still_bans(self) -> None:
        """user: Given a listed reporter reports another member, When it is handled, Then that member is banned."""
        response = await self.dispatch(report_update(update_id=702, reporter=99))
        self.assertEqual(response.status, 200)
        bans = self.telegram.calls_named("banChatMember")
        self.assertTrue(any(parameters["user_id"] == 33 for parameters in bans))

    async def test_replayed_self_report_does_not_ban(self) -> None:
        """user: Given a queued report targets its own reporter, When a spam verdict replays it, Then no ban follows."""
        update = self_report_update(710, 42)
        await self.store.save((BOT_ID, 710), json.dumps(update), NOW, 0)
        await self.store.enqueue_review(
            BOT_ID,
            PendingReview(
                update_id=710,
                chat_id=-100,
                target_message_id=81,
                target_sender_id=42,
                reporter_id=42,
                payload_json="{}",
                created_at=NOW,
            ),
        )
        response = await self.moderator.resolve_review(710, "spam")
        self.assertEqual(response.status, 200)
        self.assertIn("no_action", response.body)
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])

    async def test_bs_reply_to_own_message_registers_nothing(self) -> None:
        """user: Given a reporter replies /bs to their own message, When handled, Then nothing is registered."""
        self.telegram.messages.add(96)
        update = source_command_update(update_id=703, reporter=99, target=own_target(99))
        response = await self.dispatch(update)
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.store.matching_sources(frozenset({99})), ())
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])
        self.assertTrue(any("Your own message" in text for text in self.reply_texts()))

    async def test_bs_with_own_username_registers_nothing(self) -> None:
        """user: Given a reporter names their own account in /bs, When handled, Then it is not registered."""
        self.telegram.messages.add(97)
        self.telegram.accounts["reporter99"] = 99
        update: dict[str, object] = {
            "update_id": 704,
            "message": {
                "message_id": 97,
                "date": NOW,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 99, "is_bot": False, "first_name": "Reporter"},
                "text": "/bs @reporter99",
                "entities": [{"type": "bot_command", "offset": 0, "length": 3}],
            },
        }
        response = await self.dispatch(update)
        self.assertEqual(response.status, 200)
        self.assertEqual(await self.store.matching_sources(frozenset({99})), ())
        self.assertEqual(self.telegram.calls_named("banChatMember"), [])
        self.assertTrue(any("Your own account" in text for text in self.reply_texts()))

    async def test_administrator_allow_restores_restricted_member(self) -> None:
        """user: Given a supergroup administrator is not a reporter, When they /allow, Then access is restored."""
        self.telegram.messages.add(98)
        self.telegram.statuses = {55: "administrator", 42: "restricted"}
        response = await self.dispatch(allow_update(705, 98, 55))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.restored()), 1)
        self.assertTrue(any("access restored" in text for text in self.reply_texts()))

    async def test_ordinary_member_allow_changes_nothing(self) -> None:
        """user: Given an ordinary member is not a reporter, When they /allow, Then no access is restored."""
        self.telegram.messages.add(99)
        response = await self.dispatch(allow_update(706, 99, 55))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.restored(), [])
        self.assertFalse(any("access restored" in text for text in self.reply_texts()))

    async def test_reporter_allow_still_restores(self) -> None:
        """user: Given a listed reporter, When they /allow a restricted member, Then access is restored as before."""
        self.telegram.messages.add(100)
        self.telegram.statuses = {42: "restricted"}
        response = await self.dispatch(allow_update(707, 100, 99))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.restored()), 1)
