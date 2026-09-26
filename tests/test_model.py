from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING

from anti_fwd_spam.model import ModelConfig, ModelRetryError, model_input, spam_probability
from tests.test_telegram import ClockLoop

if TYPE_CHECKING:
    import builtins
    from collections.abc import Awaitable, Callable

CONFIG = ModelConfig("https://api.experientiallabs.ai/v1/systemone", "jev-latest", "test-key")


class StreamingResponse:
    status = 200

    def __init__(self, read_body: Callable[[], Awaitable[builtins.bytes]], cancelled: asyncio.Event) -> None:
        self.read_body = read_body
        self.cancelled = cancelled
        self.body = self
        self.sent = False

    def getReader(self) -> StreamingResponse:  # noqa: N802 - Match the JavaScript stream API.
        return self

    async def bytes(self) -> builtins.bytes:
        return await self.read_body()

    async def read(self) -> SimpleNamespace:
        if self.sent:
            return SimpleNamespace(done=True)
        payload = await self.bytes()
        self.sent = True
        return SimpleNamespace(done=False, value=SimpleNamespace(byteLength=len(payload), to_bytes=lambda: payload))

    async def cancel(self) -> None:
        self.cancelled.set()

    def releaseLock(self) -> None:  # noqa: N802 - Match the JavaScript stream API.
        pass


class ModelDeadlineTests(unittest.TestCase):
    def test_user_retains_message_when_inference_stalls(self) -> None:
        """user: Given a stalled request, When time expires, Then biography is unknown or inference is skipped."""
        for stall_profile, stall_body in ((False, False), (False, True), (True, False)):
            with self.subTest(profile=stall_profile, body=stall_body), asyncio.Runner(loop_factory=ClockLoop) as runner:
                runner.run(self.check_deadline(stall_profile=stall_profile, stall_body=stall_body))

    async def check_deadline(self, *, stall_profile: bool, stall_body: bool) -> None:
        loop = asyncio.get_running_loop()
        assert isinstance(loop, ClockLoop)
        cancelled = asyncio.Event()

        async def stall() -> bytes:
            loop.call_soon(loop.advance)
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
            return b""

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            profile = url.endswith("/getChat")

            async def read_body() -> bytes:
                if profile:
                    if stall_profile:
                        return await stall()
                    return b'{"ok":true,"result":{"id":22,"type":"private"}}'
                if stall_profile:
                    return b'{"answers":{"spam":{"type":"noul","noul":0.99}}}'
                return await stall()

            if not profile and stall_profile:
                self.assertIsNone(json.loads(body)["state"]["bio"])
            if not profile and not stall_profile and not stall_body:
                await stall()
            return StreamingResponse(read_body, cancelled)

        state = await model_input(
            fetcher,
            "123:token",
            {"from": {"id": 22, "first_name": "Alice"}, "text": "Hello"},
        )
        if stall_profile:
            self.assertEqual(await spam_probability(fetcher, CONFIG, state), 0.99)
        else:
            with self.assertRaises(ModelRetryError):
                await spam_probability(fetcher, CONFIG, state)
        self.assertTrue(cancelled.is_set())


class ModelStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_closes_the_stream_after_http_rejection(self) -> None:
        """user: Given rejected credentials and a stalled body, When headers arrive, Then the stream closes."""
        cancelled = asyncio.Event()

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            async def read_body() -> bytes:
                await asyncio.Future()
                return b""

            response = StreamingResponse(read_body, cancelled)
            response.status = 401
            return response

        with self.assertRaises(ModelRetryError):
            await spam_probability(fetcher, CONFIG, {})
        self.assertTrue(cancelled.is_set())
