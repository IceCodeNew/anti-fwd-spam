from __future__ import annotations

import asyncio
import json
import unittest
from http import HTTPMethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import builtins

from anti_fwd_spam.telegram import (
    MAX_TELEGRAM_RESPONSE_BYTES,
    DeleteOutcome,
    classify_telegram_response,
    delete_message,
)


class FakeResponse:
    def __init__(self, status: int, body: builtins.bytes) -> None:
        self.status = status
        self.body = body

    async def bytes(self) -> builtins.bytes:
        return self.body


class TelegramDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_exact_delete_request_and_awaits_api_result(self) -> None:
        calls: list[tuple[str, HTTPMethod, dict[str, str], str]] = []

        async def fetcher(url: str, *, method: HTTPMethod, headers: dict[str, str], body: str) -> FakeResponse:
            calls.append((url, method, headers, body))
            return FakeResponse(200, b'{"ok":true,"result":true}')

        outcome = await delete_message(fetcher, "123:token", -55, 91)
        self.assertIs(outcome, DeleteOutcome.DELETED)
        self.assertEqual(len(calls), 1)
        url, method, headers, body = calls[0]
        self.assertEqual(url, "https://api.telegram.org/bot123:token/deleteMessage")
        self.assertIs(method, HTTPMethod.POST)
        self.assertEqual(headers, {"content-type": "application/json"})
        self.assertEqual(json.loads(body), {"chat_id": -55, "message_id": 91})

    async def test_network_error_is_retryable(self) -> None:
        async def fetcher(
            url: str,
            *,
            method: HTTPMethod,
            headers: dict[str, str],
            body: str,
        ) -> FakeResponse:
            msg = "network unavailable"
            raise OSError(msg)

        self.assertIs(await delete_message(fetcher, "123:token", -1, 1), DeleteOutcome.RETRYABLE_FAILURE)

    async def test_deadline_is_retryable_and_cancels_wait(self) -> None:
        cancelled = asyncio.Event()

        async def fetcher(
            url: str,
            *,
            method: HTTPMethod,
            headers: dict[str, str],
            body: str,
        ) -> FakeResponse:
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
            return FakeResponse(200, b'{"ok":true,"result":true}')

        outcome = await delete_message(fetcher, "123:token", -1, 1, deadline_seconds=0.001)
        self.assertIs(outcome, DeleteOutcome.RETRYABLE_FAILURE)
        self.assertTrue(cancelled.is_set())

    async def test_deadline_covers_response_body(self) -> None:
        reading_cancelled = asyncio.Event()

        class SlowResponse(FakeResponse):
            async def bytes(self) -> builtins.bytes:
                try:
                    await asyncio.sleep(60)
                finally:
                    reading_cancelled.set()
                return self.body

        async def fetcher(
            url: str,
            *,
            method: HTTPMethod,
            headers: dict[str, str],
            body: str,
        ) -> FakeResponse:
            return SlowResponse(200, b'{"ok":true,"result":true}')

        outcome = await delete_message(fetcher, "123:token", -1, 1, deadline_seconds=0.001)
        self.assertIs(outcome, DeleteOutcome.RETRYABLE_FAILURE)
        self.assertTrue(reading_cancelled.is_set())

    async def test_oversized_api_response_is_retryable(self) -> None:
        async def fetcher(
            url: str,
            *,
            method: HTTPMethod,
            headers: dict[str, str],
            body: str,
        ) -> FakeResponse:
            return FakeResponse(200, b"x" * (MAX_TELEGRAM_RESPONSE_BYTES + 1))

        self.assertIs(await delete_message(fetcher, "123:token", -1, 1), DeleteOutcome.RETRYABLE_FAILURE)


class TelegramResponseTests(unittest.TestCase):
    def test_requires_api_json_success_not_only_http_success(self) -> None:
        cases = (
            (200, b'{"ok":true,"result":true}', DeleteOutcome.DELETED),
            (200, b'{"ok":false,"error_code":403}', DeleteOutcome.PERMANENT_FAILURE),
            (200, b'{"ok":true,"result":false}', DeleteOutcome.RETRYABLE_FAILURE),
            (200, b'{"result":true}', DeleteOutcome.RETRYABLE_FAILURE),
            (200, b"not json", DeleteOutcome.RETRYABLE_FAILURE),
        )
        for status, body, expected in cases:
            with self.subTest(status=status, body=body):
                self.assertIs(classify_telegram_response(status, body), expected)

    def test_classifies_retryable_permanent_and_duplicate_errors(self) -> None:
        cases = (
            (
                429,
                b'{"ok":false,"error_code":429,"parameters":{"retry_after":3}}',
                DeleteOutcome.RETRYABLE_FAILURE,
            ),
            (502, b'{"ok":false,"error_code":502}', DeleteOutcome.RETRYABLE_FAILURE),
            (200, b'{"ok":false,"error_code":500}', DeleteOutcome.RETRYABLE_FAILURE),
            (401, b'{"ok":false,"error_code":401}', DeleteOutcome.PERMANENT_FAILURE),
            (403, b'{"ok":false,"error_code":403}', DeleteOutcome.PERMANENT_FAILURE),
            (401, b"not json", DeleteOutcome.PERMANENT_FAILURE),
            (
                400,
                b'{"ok":false,"error_code":400,"description":"Bad Request: message to delete not found"}',
                DeleteOutcome.ALREADY_ABSENT,
            ),
            (
                400,
                b'{"ok":false,"error_code":400,"description":"message can not be deleted"}',
                DeleteOutcome.PERMANENT_FAILURE,
            ),
        )
        for status, body, expected in cases:
            with self.subTest(status=status, expected=expected):
                self.assertIs(classify_telegram_response(status, body), expected)
