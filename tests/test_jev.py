from __future__ import annotations

import asyncio
import json
import os
import unittest
from http.client import HTTPSConnection
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from anti_fwd_spam.constants import MODEL_PROVIDERS, SPAM_THRESHOLD
from anti_fwd_spam.model import ModelConfig, join_model_input, model_input, spam_probability
from tests.test_model import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anti_fwd_spam.telegram import TelegramResponse


class JevAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def check_case(self, case: dict[str, object]) -> None:
        provider = next((key for key in MODEL_PROVIDERS if os.environ.get(key)), None)
        self.assertIsNotNone(provider, "A configured Luna provider key is required")
        if provider is None:
            return
        url, model = MODEL_PROVIDERS[provider]
        config = ModelConfig(url, model, os.environ[provider])
        message = case["message"]
        assert isinstance(message, dict)
        joining = case.get("mode") == "join_profile"
        if joining:
            member = message["from"]
            assert isinstance(member, dict)
            state = join_model_input(member)
        else:
            state = model_input(message)
        bio = case.get("bio")
        state["bio"] = bio if isinstance(bio, str) else None
        if "profile_targets" in case:
            state["profile_targets"] = case["profile_targets"]
        if joining:
            probability = await spam_probability(self.fetcher, config, state, joining=True)
        else:
            probability = await spam_probability(self.fetcher, config, state)
        self.assertIsNotNone(probability, "The selected model must return a valid score")
        if probability is not None:
            self.assertEqual(probability >= SPAM_THRESHOLD, case["spam"], f"Model score: {probability}")

    @staticmethod
    async def fetcher(url: str, *, method: str, headers: dict[str, str], body: str) -> TelegramResponse:
        def send() -> tuple[int, bytes]:
            endpoint = urlsplit(url)
            assert endpoint.scheme == "https"
            assert endpoint.hostname is not None
            connection = HTTPSConnection(endpoint.hostname, endpoint.port, timeout=8)
            try:
                path = endpoint.path + (f"?{endpoint.query}" if endpoint.query else "")
                connection.request(method, path, body=body.encode(), headers=headers)
                response = connection.getresponse()
                return response.status, response.read(65_537)
            finally:
                connection.close()

        status, payload = await asyncio.to_thread(send)

        async def read_body() -> bytes:
            return payload

        response = StreamingResponse(read_body, asyncio.Event())
        response.status = status
        return response


def acceptance_case(case: dict[str, object]) -> Callable[[JevAcceptanceTests], Awaitable[None]]:
    async def test(self: JevAcceptanceTests) -> None:
        await self.check_case(case)

    test.__doc__ = "user: Given a regression sample, When Luna evaluates it, Then its score matches the contract."
    if reason := case.get("skip"):
        assert isinstance(reason, str)
        return unittest.skip(reason)(test)
    return unittest.skipUnless(
        os.environ.get("JEV_LIVE_TEST") == "1", "Set JEV_LIVE_TEST=1 to query the real Luna provider"
    )(test)


for _case in json.loads(Path(__file__).with_name("jev-cases.json").read_text()):
    _name = "test_user_" + _case["name"].replace("-", "_")
    assert not hasattr(JevAcceptanceTests, _name), f"Duplicate Jev case: {_case['name']}"
    setattr(JevAcceptanceTests, _name, acceptance_case(_case))
