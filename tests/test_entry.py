from __future__ import annotations

import json
import sys
import types
import unittest
from http import HTTPMethod
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_reports import BOT_USERNAME, FakeTelegram, SQLiteD1, report_update


class _FakeWorkerEntrypoint:
    pass


fake_workers = types.ModuleType("workers")
fake_workers.__dict__.update(
    {
        "Request": object,
        "Response": object,
        "WorkerEntrypoint": _FakeWorkerEntrypoint,
        "fetch": object(),
    }
)

with patch.dict(sys.modules, {"workers": fake_workers}):
    import entry


BOT_TOKEN = f"{123456}:test-token"
WEBHOOK_SECRET = "test-secret"  # noqa: S105


class EntrypointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.worker = entry.Default.__new__(entry.Default)
        self.db = SQLiteD1()
        self.addCleanup(self.db.connection.close)
        self.worker.env = SimpleNamespace(
            BOT_TOKEN=BOT_TOKEN,
            TELEGRAM_WEBHOOK_SECRET=WEBHOOK_SECRET,
            BLACKLIST_BOT_IDS="273234066",
            BOT_USERNAME=BOT_USERNAME,
            REPORTS=self.db,
        )
        self.telegram = FakeTelegram()
        self.enterContext(patch.object(entry, "fetch", self.telegram))
        self.enterContext(
            patch.object(entry, "Response", side_effect=lambda body, **kwargs: SimpleNamespace(body=body, **kwargs))
        )

    def request(self, *chunks: bytes) -> Mock:
        results = [
            SimpleNamespace(done=False, value=SimpleNamespace(to_bytes=lambda chunk=chunk: chunk)) for chunk in chunks
        ]
        reader = SimpleNamespace(
            read=AsyncMock(side_effect=[*results, SimpleNamespace(done=True)]),
            cancel=AsyncMock(),
            releaseLock=Mock(),
        )
        return Mock(
            spec=entry.Request,
            url="https://worker.example/webhook",
            method=HTTPMethod.POST,
            headers={"content-type": "application/json", "x-telegram-bot-api-secret-token": WEBHOOK_SECRET},
            body=SimpleNamespace(getReader=Mock(return_value=reader)),
        )

    async def test_rejects_invalid_route_auth_and_declared_length_before_reading(self) -> None:
        cases = (
            ("url", "https://worker.example/", 404),
            ("method", HTTPMethod.GET, 405),
            ("x-telegram-bot-api-secret-token", None, 401),
            ("x-telegram-bot-api-secret-token", "wrong", 401),
            ("x-telegram-bot-api-secret-token", "é", 401),
            ("content-length", "-1", 400),
            ("content-length", "invalid", 400),
            ("content-length", str(entry.MAX_UPDATE_BYTES + 1), 413),
        )
        for field, value, status in cases:
            with self.subTest(field=field, value=value):
                request = self.request(b"invalid JSON")
                if field in {"url", "method"}:
                    setattr(request, field, value)
                else:
                    request.headers[field] = value
                response = await self.worker.fetch(request)
                self.assertEqual(response.status, status)
                request.body.getReader.assert_not_called()
        self.assertEqual(self.telegram.calls, [])

    async def test_stream_limit_accepts_boundary_and_cancels_overflow_despite_header(self) -> None:
        for extra, expected in ((0, 200), (1, 413)):
            with self.subTest(extra=extra):
                request = self.request(b"{}", b" " * (entry.MAX_UPDATE_BYTES - 2 + extra))
                request.headers["content-length"] = "2"
                response = await self.worker.fetch(request)
                self.assertEqual(response.status, expected)
                reader = request.body.getReader.return_value
                reader.releaseLock.assert_called_once()
                self.assertEqual(reader.cancel.await_count, extra)

    async def test_invalid_content_type_and_json_never_reach_telegram(self) -> None:
        for content_type, body, status in (("text/plain", b"{}", 415), ("application/json", b"{", 400)):
            request = self.request(body)
            request.headers["content-type"] = content_type
            self.assertEqual((await self.worker.fetch(request)).status, status)
        self.assertEqual(self.telegram.calls, [])

    async def test_report_reaches_bound_database_and_moderates_only_reply_target(self) -> None:
        update = report_update()
        raw = json.dumps(update).encode()
        response = await self.worker.fetch(self.request(raw[:11], raw[11:]))
        self.assertEqual((response.status, response.body), (200, "deleted; muted"))
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(
            [method for method, _ in self.telegram.calls],
            ["getChatMember", "deleteMessage", "getChatMember", "restrictChatMember"],
        )
        self.assertEqual(self.telegram.calls[1][1], {"chat_id": -10012, "message_id": 81})
        self.assertEqual(self.db.connection.execute("SELECT raw_update FROM reports").fetchone()[0], raw.decode())

    async def test_invalid_environment_is_rejected_before_reading(self) -> None:
        for name, value in (("BLACKLIST_BOT_IDS", []), ("BOT_TOKEN", {}), ("BOT_USERNAME", "@invalid")):
            with self.subTest(name=name), patch.object(self.worker.env, name, value):
                request = self.request(b"{}")
                self.assertEqual((await self.worker.fetch(request)).status, 500)
                request.body.getReader.assert_not_called()

    def test_configuration_reflects_environment_changes(self) -> None:
        self.assertEqual(self.worker._get_config().bot_ids, frozenset({273234066}))
        self.worker.env.BLACKLIST_BOT_IDS = "987654321"
        self.assertEqual(self.worker._get_config().bot_ids, frozenset({987654321}))

    async def test_scheduled_cleanup_uses_bound_database(self) -> None:
        update = report_update()
        store = entry.ReportStore(self.db)
        await store.save(123, 1, json.dumps(update), update["message"]["reply_to_message"], 100)
        with patch.object(entry.time, "time", return_value=259299):
            await self.worker.scheduled(None, None, None)
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 1)
        with patch.object(entry.time, "time", return_value=259300):
            await self.worker.scheduled(None, None, None)
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM reports").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
