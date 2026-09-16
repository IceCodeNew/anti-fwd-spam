from __future__ import annotations

import asyncio
import unittest
from typing import TYPE_CHECKING

from anti_fwd_spam.telegram import DeleteOutcome, delete_message

if TYPE_CHECKING:
    import builtins


class ClockLoop(asyncio.SelectorEventLoop):
    now = 0.0

    def time(self) -> float:
        return self.now

    def advance(self) -> None:
        self.now += 8


class DeadlineTests(unittest.TestCase):
    def test_user_retries_a_stalled_request_or_response(self) -> None:
        """user: Given Telegram stalls, When eight seconds elapse, Then delivery can retry and the wait is cancelled."""
        for stall_body in (False, True):
            with self.subTest(stall_body=stall_body), asyncio.Runner(loop_factory=ClockLoop) as runner:
                runner.run(self.check_deadline(stall_body=stall_body))

    async def check_deadline(self, *, stall_body: bool) -> None:
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

        class Response:
            status = 200

            async def bytes(self) -> builtins.bytes:
                return await stall()

        async def fetcher(url, *, method, headers, body) -> Response:
            if not stall_body:
                await stall()
            return Response()

        self.assertIs(await delete_message(fetcher, "123:token", -1, 1), DeleteOutcome.RETRYABLE_FAILURE)
        self.assertTrue(cancelled.is_set())
