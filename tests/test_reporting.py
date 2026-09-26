from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable

from anti_fwd_spam.actions import AppResponse
from anti_fwd_spam.evidence import EvidenceError
from anti_fwd_spam.reporting import Reporting, ReportingPlugin
from anti_fwd_spam.telegram import TelegramError


async def acknowledge(update: dict[str, object], message: dict[str, object], raw_json: str) -> AppResponse:
    return AppResponse(200, "accepted by extension")


Handler = Callable[[dict[str, object], dict[str, object], str], Awaitable[AppResponse]]


def extension(handle: Handler = acknowledge, name: str = "extension") -> ReportingPlugin:
    return ReportingPlugin(name, lambda message: message.get("text") == "/extra", handle)


def failing(error: Exception) -> Handler:
    async def fail(update: dict[str, object], message: dict[str, object], raw_json: str) -> AppResponse:
        raise error

    return fail


class ReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_extensions_share_authorization(self) -> None:
        """user: Given another reporting plugin, When identities request it, Then only listed senders enter it."""
        reporting = Reporting(frozenset({11, -10012}))
        for sender, accepted in (
            ({"from": {"id": 11, "is_bot": False}}, True),
            ({"from": {"id": 22, "is_bot": False}}, False),
            ({"from": {"id": 11, "is_bot": True}}, False),
            ({"sender_chat": {"id": -10012}}, True),
            ({"sender_chat": {"id": -10099}, "from": {"id": 11, "is_bot": False}}, False),
            ({"sender_chat": None, "from": {"id": 11, "is_bot": False}}, False),
        ):
            with self.subTest(sender=sender):
                update: dict[str, object] = {"message": {"text": "/extra", **sender}}
                response = await reporting.dispatch((extension(),), update, "{}")
                self.assertEqual(response, AppResponse(200, "accepted by extension") if accepted else None)
        self.assertIsNone(await reporting.dispatch((extension(),), {"message": {"text": "hello"}}, "{}"))
        self.assertIsNone(await reporting.dispatch((extension(),), {}, "{}"))

    async def test_user_reaches_only_the_first_matching_plugin(self) -> None:
        """user: Given two matching plugins, When a listed sender reports, Then only the first plugin handles it."""

        async def second(update: dict[str, object], message: dict[str, object], raw_json: str) -> AppResponse:
            return AppResponse(200, "second plugin")

        update: dict[str, object] = {"message": {"text": "/extra", "from": {"id": 11, "is_bot": False}}}
        response = await Reporting(frozenset({11})).dispatch((extension(), extension(second, "second")), update, "{}")
        self.assertEqual(response, AppResponse(200, "accepted by extension"))

    async def test_user_retries_plugin_failures(self) -> None:
        """user: Given a failing plugin, When storage or Telegram fails, Then only temporary failures request retry."""
        for error, status in (
            (EvidenceError(), 503),
            (TelegramError(retryable=True), 503),
            (TelegramError(retryable=False), 200),
        ):
            with self.subTest(error=error):
                update: dict[str, object] = {"message": {"text": "/extra", "from": {"id": 11, "is_bot": False}}}
                response = await Reporting(frozenset({11})).dispatch((extension(failing(error)),), update, "{}")
                self.assertEqual(response.status if response else None, status)
