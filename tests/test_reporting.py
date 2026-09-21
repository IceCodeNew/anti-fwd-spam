from __future__ import annotations

import unittest

from anti_fwd_spam.actions import AppResponse
from anti_fwd_spam.reporting import Reporting, ReportingPlugin


async def acknowledge(update: dict[str, object], message: dict[str, object], raw_json: str) -> AppResponse:
    return AppResponse(200, "accepted by extension")


class ReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_extensions_share_authorization(self) -> None:
        """user: Given another reporting plugin, When identities request it, Then only listed senders enter it."""
        plugins = (ReportingPlugin("extension", lambda message: message.get("text") == "/extra", acknowledge),)
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
                response = await reporting.dispatch(plugins, update, "{}")
                self.assertEqual(
                    response,
                    AppResponse(
                        200, "accepted by extension" if accepted else "extension ignored; reporter not allowed"
                    ),
                )
        self.assertIsNone(await reporting.dispatch(plugins, {"message": {"text": "hello"}}, "{}"))
        self.assertIsNone(await reporting.dispatch(plugins, {}, "{}"))

    async def test_user_authority_is_checked_on_redelivery(self) -> None:
        """user: Given a revoked reporter, When the same extension request returns, Then it is rejected."""
        plugins = (ReportingPlugin("extension", lambda _message: True, acknowledge),)
        update: dict[str, object] = {"message": {"from": {"id": 11, "is_bot": False}}}
        self.assertEqual(
            await Reporting(frozenset({11})).dispatch(plugins, update, "{}"),
            AppResponse(200, "accepted by extension"),
        )
        self.assertEqual(
            await Reporting(frozenset()).dispatch(plugins, update, "{}"),
            AppResponse(200, "extension ignored; reporter not allowed"),
        )
