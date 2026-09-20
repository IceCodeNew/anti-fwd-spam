from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from anti_fwd_spam.actions import Actions
from anti_fwd_spam.evidence import EvidenceError, ReportStore

if TYPE_CHECKING:
    import builtins

MUTE_SCHEMA = Path("migrations/0004_automatic_mutes.sql").read_text()


class Statement:
    def __init__(self, connection: sqlite3.Connection, sql: str) -> None:
        self.connection, self.sql = connection, sql
        self.parameters: tuple[int, ...] = ()

    def bind(self, *parameters: int) -> Statement:
        self.parameters = parameters
        return self

    async def first(self) -> tuple[int] | None:
        return self.connection.execute(self.sql, self.parameters).fetchone()

    async def run(self) -> None:
        self.connection.execute(self.sql, self.parameters)


class Database:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def prepare(self, sql: str) -> Statement:
        return Statement(self.connection, sql)


class Response:
    status = 200

    def __init__(self, result: object) -> None:
        self.result = result

    async def bytes(self) -> builtins.bytes:
        return json.dumps({"ok": True, "result": self.result}).encode()


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_retries_after_ownership_storage_failure(self) -> None:
        """user: Given deleted spam, When the mute ownership check fails once, Then retry can mute its sender."""
        # A native SQLite connection keeps the actual claim SQL and constraints;
        # the callback exposes a storage-read failure between claim and Telegram.
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript(MUTE_SCHEMA)
            store = ReportStore(Database(connection))
            message: dict[str, object] = {
                "chat": {"id": -10012, "type": "supergroup"},
                "message_id": 81,
                "from": {"id": 22, "is_bot": False},
            }
            permissions = {"can_send_messages": True}

            async def fetcher(url, *, method, headers, body) -> Response:
                parameters = json.loads(body)
                self.assertEqual(method, "POST")
                self.assertEqual(headers["content-type"], "application/json")
                if url.endswith("/getChatMember"):
                    return Response({"status": "member", "user": {"id": parameters["user_id"]}})
                if url.endswith("/restrictChatMember"):
                    self.assertEqual(parameters["user_id"], 22)
                    self.assertEqual(parameters["until_date"], 0)
                    permissions.update(parameters["permissions"])
                else:
                    self.assertTrue(url.endswith("/deleteMessage"))
                    self.assertEqual(parameters["message_id"], 81)
                return Response(result=True)

            async def can_act() -> bool:
                if connection.execute("SELECT 1 FROM automatic_mutes").fetchone():
                    raise EvidenceError
                return True

            actions = Actions(fetcher, "123:test-token", store)
            response = await actions.delete_and_mute(message, can_act=can_act)
            self.assertEqual(response.status, 503)
            self.assertTrue(permissions["can_send_messages"])
            response = await actions.delete_and_mute(message)
            self.assertEqual(response.status, 200)
            self.assertFalse(permissions["can_send_messages"])
