from __future__ import annotations

import json
import unittest

from anti_fwd_spam.app import AppResponse, handle_update
from anti_fwd_spam.policy import Config

BOT_TOKEN = f"{123}:test-token"
WEBHOOK_SECRET = "test-secret"  # noqa: S105
POSTBOT_ID = 273_234_066


class Recorder:
    def __init__(self, response: AppResponse | None = None) -> None:
        self.response = response or AppResponse(200, "deleted")
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, update, target, raw_json) -> AppResponse:
        if target is None:
            return AppResponse(200, "ignored")
        self.calls.append(target)
        return self.response


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = Config.from_values(bot_token=BOT_TOKEN, webhook_secret=WEBHOOK_SECRET)
        self.matching_body = json.dumps(
            {
                "message": {
                    "message_id": 12,
                    "chat": {"id": -99, "type": "group"},
                    "via_bot": {"id": POSTBOT_ID, "is_bot": True},
                    "sticker": {"file_id": "x"},
                }
            }
        ).encode()

    async def call(
        self,
        recorder: Recorder,
        *,
        content_type: str | None = "application/json; charset=utf-8",
        body: bytes | None = None,
    ) -> AppResponse:
        return await handle_update(
            content_type=content_type,
            body=self.matching_body if body is None else body,
            config=self.config,
            process_update=recorder,
        )

    async def test_validates_content_type_and_json(self) -> None:
        cases = (
            (await self.call(Recorder(), content_type="text/plain"), 415),
            (await self.call(Recorder(), body=b"{"), 400),
            (await self.call(Recorder(), body='{"update_id":1}'.encode("utf-16")), 400),
        )
        for response, expected_status in cases:
            self.assertEqual(response.status, expected_status)

    async def test_calls_delete_once_for_a_match_and_never_for_a_nonmatch(self) -> None:
        matching = Recorder()
        matched_response = await self.call(matching)
        nonmatching = Recorder()
        ignored_response = await self.call(nonmatching, body=b'{"update_id":1}')
        self.assertEqual((matched_response.status, matching.calls), (200, [(-99, 12)]))
        self.assertEqual((ignored_response.status, nonmatching.calls), (200, []))

    async def test_preserves_moderation_response_without_false_success(self) -> None:
        for response_values in ((200, "deleted; muted"), (200, "report recorded"), (503, "mute pending retry")):
            response = await self.call(Recorder(AppResponse(*response_values)))
            self.assertEqual((response.status, response.body), response_values)


if __name__ == "__main__":
    unittest.main()
