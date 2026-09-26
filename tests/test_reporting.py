from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable, Callable

from anti_fwd_spam.actions import AppResponse
from anti_fwd_spam.evidence import EvidenceError
from anti_fwd_spam.policy import TelegramUpdate, parse_update
from anti_fwd_spam.reporting import Reporting, ReportingPlugin
from anti_fwd_spam.telegram import TelegramError

Handler = Callable[[TelegramUpdate], Awaitable[AppResponse]]


async def acknowledge(update: TelegramUpdate) -> AppResponse:
    return AppResponse(200, "accepted by extension")


def extension(handle: Handler = acknowledge, name: str = "extension") -> ReportingPlugin:
    return ReportingPlugin(name, lambda update: update.message.get("text") == "/extra", handle)


def failing(error: Exception) -> Handler:
    async def fail(update: TelegramUpdate) -> AppResponse:
        raise error

    return fail


def delivery(sender: dict[str, object], text: str = "/extra") -> TelegramUpdate:
    message = {"message_id": 1, "chat": {"id": -10012, "type": "supergroup"}, "text": text, **sender}
    update = parse_update(json.dumps({"update_id": 1, "message": message}))
    assert update is not None
    return update


LISTED: dict[str, object] = {"from": {"id": 11, "is_bot": False}}


class ReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_extensions_share_authorization(self) -> None:
        """user: Given another reporting plugin, When identities request it, Then only listed senders enter it."""
        reporting = Reporting(frozenset({11, -10012}))
        senders: tuple[tuple[dict[str, object], bool], ...] = (
            (LISTED, True),
            ({"from": {"id": 22, "is_bot": False}}, False),
            ({"from": {"id": 11, "is_bot": True}}, False),
            ({"sender_chat": {"id": -10012}}, True),
            ({"sender_chat": {"id": -10099}, "from": {"id": 11, "is_bot": False}}, False),
            ({"sender_chat": None, "from": {"id": 11, "is_bot": False}}, False),
        )
        for sender, accepted in senders:
            with self.subTest(sender=sender):
                response = await reporting.dispatch((extension(),), delivery(sender))
                self.assertEqual(response, AppResponse(200, "accepted by extension") if accepted else None)
        self.assertIsNone(await reporting.dispatch((extension(),), delivery(LISTED, "hello")))

    async def test_user_reaches_only_the_first_matching_plugin(self) -> None:
        """user: Given two matching plugins, When a listed sender reports, Then only the first plugin handles it."""

        async def second(update: TelegramUpdate) -> AppResponse:
            return AppResponse(200, "second plugin")

        plugins = (extension(), extension(second, "second"))
        response = await Reporting(frozenset({11})).dispatch(plugins, delivery(LISTED))
        self.assertEqual(response, AppResponse(200, "accepted by extension"))

    async def test_user_retries_plugin_failures(self) -> None:
        """user: Given a failing plugin, When storage or Telegram fails, Then only temporary failures request retry."""
        for error, status in (
            (EvidenceError(), 503),
            (TelegramError(retryable=True), 503),
            (TelegramError(retryable=False), 200),
        ):
            with self.subTest(error=error):
                response = await Reporting(frozenset({11})).dispatch((extension(failing(error)),), delivery(LISTED))
                self.assertEqual(response.status if response else None, status)
