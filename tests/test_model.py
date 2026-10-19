from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING

from anti_fwd_spam.constants import MAX_PROFILE_TEXT
from anti_fwd_spam.model import (
    ModelConfig,
    ModelRetryError,
    contextual_hints,
    extract_profile_handles,
    join_model_input,
    model_input,
    sender_bio,
    spam_probability,
)
from anti_fwd_spam.rules import (
    COUNTERFEIT_CARD_HINT,
    EASY_INCOME_HINT,
    INSTRUCTIONS,
    JOIN_PROFILE_INSTRUCTIONS,
    PROJECT_RECRUITMENT_HINT,
)
from tests.test_telegram import ClockLoop

if TYPE_CHECKING:
    import builtins
    from collections.abc import Awaitable, Callable

CONFIG = ModelConfig("https://api.experientiallabs.ai/v1/systemone", "jev-latest", "test-key")


class ProfileHandleTests(unittest.TestCase):
    def test_user_extracts_only_bounded_public_handles(self) -> None:
        """user: Given profile links, When parsed, Then unique public handles remain."""
        self.assertEqual(
            extract_profile_handles(
                "频道 @Example_Channel https://t.me/Example_Channel https://telegram.me/Other_Group "
                "https://t.me/+private https://example.invalid/public @Example_Channel"
            ),
            ("example_channel", "other_group"),
        )

    def test_user_rejects_non_public_or_oversized_profile_targets(self) -> None:
        """user: Given private links or oversized profile text, When parsed, Then no targets are returned."""
        self.assertEqual(
            extract_profile_handles("https://t.me/+invite https://t.me/joinchat/hash https://t.me/c/1/2"), ()
        )
        self.assertEqual(extract_profile_handles("x" * 4097), ())


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


class JoinModelInputTests(unittest.TestCase):
    def test_button_text_is_visible_but_callback_and_reference_are_isolated(self) -> None:
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "Advertising offer",
                        "url": "https://example.invalid/offer",
                        "callback_data": "private-callback",
                    }
                ]
            ]
        }
        state = model_input(
            {
                "from": {"first_name": "Member"},
                "text": "请核实",
                "reply_to_message": {"text": "sample", "reply_markup": markup},
            }
        )
        content, context = state["message"], state["context"]
        assert isinstance(content, dict)
        assert isinstance(context, dict)
        reference = context["reply"]
        assert isinstance(reference, dict)
        self.assertNotIn("reply_markup", content)
        self.assertEqual(reference["reply_markup"], ["Advertising offer", "https://example.invalid/offer"])
        state = model_input({"from": {"first_name": "Member"}, "text": "offer", "reply_markup": markup})
        content = state["message"]
        assert isinstance(content, dict)
        self.assertEqual(content["reply_markup"], ["Advertising offer", "https://example.invalid/offer"])
        self.assertNotIn("private-callback", json.dumps(state))

    def test_user_sends_only_joining_members_profile_fields(self) -> None:
        """Given a joined account, snapshot its own name and username without identity or authored content."""
        member: dict[str, object] = {
            "id": 987654321,
            "is_bot": False,
            "first_name": "Alice",
            "last_name": "Example",
            "username": "synthetic_member",
            "language_code": "zh",
            "text": "This account metadata is not an authored message",
            "bio": "Biography must come from the separate profile lookup",
        }
        self.assertEqual(join_model_input(member), {"nickname": "Alice Example", "username": "synthetic_member"})
        for username in (None, "", 123):
            with self.subTest(username=username):
                self.assertEqual(join_model_input({"first_name": "Alice", "username": username}), {"nickname": "Alice"})

    def test_user_bounds_joining_profile_text(self) -> None:
        """Given oversized account strings, snapshot only bounded profile text."""
        member: dict[str, object] = {
            "first_name": "名" * (MAX_PROFILE_TEXT + 1),
            "username": "u" * (MAX_PROFILE_TEXT + 1),
        }
        self.assertEqual(
            join_model_input(member), {"nickname": "名" * MAX_PROFILE_TEXT, "username": "u" * MAX_PROFILE_TEXT}
        )


class CardHintTests(unittest.TestCase):
    def test_easy_income_hint_stays_in_current_unframed_text_or_caption(self) -> None:
        offer = "好项目无费用年底带你搞奔驰 @sample_contact"
        for field in ("text", "caption"):
            self.assertIn(EASY_INCOME_HINT, contextual_hints({"message": {field: offer}}))
            for warning in ("请举报\uff1a", "请问是否可信\uff1a", "警告\uff1a", "诈骗广告\uff1a", "“"):
                self.assertNotIn(EASY_INCOME_HINT, contextual_hints({"message": {field: warning + offer}}))
        for state in (
            {"message": {"text": "请核实"}, "bio": offer},
            {"message": {"text": "请核实"}, "context": {"reply": {"text": offer}}},
            {"message": {"text": "正常讨论"}, "contact": {"card_name": offer}},
            {"message": {"text": "举报", "caption": offer}},
            {"message": {"text": None, "caption": 123}},
            {"nickname": offer, "bio": offer},
        ):
            self.assertNotIn(EASY_INCOME_HINT, contextual_hints(dict(state)))

    def test_hint_uses_only_the_current_visible_card_not_profiles_or_references(self) -> None:
        card = {"first_name": "假币真用可过机找妹扫街没问题"}
        for text in (None, "管理员请处理这张广告名片", "这张名片是不是广告"):
            message: dict[str, object] = {"from": {"first_name": "Member"}, "contact": card}
            if text is not None:
                message["text"] = text
            self.assertIn(COUNTERFEIT_CARD_HINT, contextual_hints(model_input(message)))
        for related_message in (
            {"from": {"first_name": "Member"}, "contact": {"first_name": "普通商户"}},
            {"from": {"first_name": "Member"}, "reply_to_message": {"contact": card}},
        ):
            state = model_input(dict(related_message))
            state["bio"] = "假钞销售"
            self.assertNotIn(COUNTERFEIT_CARD_HINT, contextual_hints(state))


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

        state = {
            **model_input({"from": {"id": 22, "first_name": "Alice"}, "text": "Hello"}),
            "bio": await sender_bio(fetcher, "123:token", 22),
        }
        if stall_profile:
            self.assertEqual(await spam_probability(fetcher, CONFIG, state), 0.99)
        else:
            with self.assertRaises(ModelRetryError):
                await spam_probability(fetcher, CONFIG, state)
        self.assertTrue(cancelled.is_set())


class ModelStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_requests_independent_spam_detection_for_a_reply(self) -> None:
        """user: Given an ad under a normal reply, When sent to Jev, Then the task includes independent sender spam."""
        cancelled = asyncio.Event()
        requests = []

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            requests.append(json.loads(body))

            async def read_body() -> bytes:
                return b'{"answers":{"spam":{"type":"noul","noul":0.99}}}'

            return StreamingResponse(read_body, cancelled)

        state = model_input(
            {
                "from": {"id": 22, "first_name": "User"},
                "text": "Join my paid promotion group",
                "reply_to_message": {"from": {"id": 11, "first_name": "Member"}, "text": "Today is Saturday"},
            }
        )
        self.assertEqual(await spam_probability(fetcher, CONFIG, state), 0.99)
        self.assertEqual(requests[0]["state"]["message"]["text"], "Join my paid promotion group")
        # The provider task is a separate contract; a fixed fake score cannot prove its interpretation.
        self.assertIn("普通引用不能使当前正文的广告免责", requests[0]["questions"]["spam"]["instructions"])

    async def test_user_adds_project_recruitment_hint_only_for_matching_sender_context(self) -> None:
        """Given project recruitment and matching bio, Jev receives the scoped hint."""
        requests = []

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            requests.append(json.loads(body))

            async def read_body() -> bytes:
                return b'{"answers":{"spam":{"type":"noul","noul":0.99}}}'

            return StreamingResponse(read_body, asyncio.Event())

        state = {
            **model_input({"from": {"id": 22, "first_name": "User"}, "text": "找想干事的兄弟合作"}),
            "bio": "项目大量找人合作\uff0c点击简介做项目 @<contact>",
        }
        self.assertEqual(await spam_probability(fetcher, CONFIG, state), 0.99)
        self.assertIn("项目合作招募", requests[0]["questions"]["spam"]["instructions"])

    async def test_user_keeps_the_base_prompt_for_unrelated_messages(self) -> None:
        """user: Given an unrelated message, When sent to Jev, Then no project-specific hint is added."""
        requests = []

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            requests.append(json.loads(body))

            async def read_body() -> bytes:
                return b'{"answers":{"spam":{"type":"noul","noul":0.01}}}'

            return StreamingResponse(read_body, asyncio.Event())

        state = {**model_input({"from": {"id": 22, "first_name": "User"}, "text": "普通讨论"}), "bio": "项目合作经历"}
        self.assertEqual(await spam_probability(fetcher, CONFIG, state), 0.01)
        self.assertNotIn("项目合作招募", requests[0]["questions"]["spam"]["instructions"])

    async def test_user_scopes_false_positive_hints_to_their_matching_context(self) -> None:
        """Given known discussion-shaped inputs, only matching clarifications are added."""
        cases = [
            (
                {
                    "from": {"id": 22, "first_name": "User"},
                    "text": "搬家师傅不用定金\uff0c当面付\uff0c联系电话在名片里。",
                },
                None,
                "第三方服务的付款安排",
            ),
            (
                {"from": {"id": 22, "first_name": "User"}, "text": "全套上门安装"},
                None,
                "泛化服务短语",
            ),
            (
                {
                    "from": {"id": 22, "first_name": "User"},
                    "text": "这段文案是不是广告\uff1f",
                    "reply_to_message": {"from": {"id": 11, "first_name": "Member"}, "text": "电话卡支持eSIM"},
                },
                None,
                "询问引用文案是否为广告",
            ),
        ]
        for message, bio, hint in cases:
            requests = []

            async def fetcher(url, *, method, headers, body, requests=requests) -> StreamingResponse:
                requests.append(json.loads(body))

                async def read_body() -> bytes:
                    return b'{"answers":{"spam":{"type":"noul","noul":0.01}}}'

                return StreamingResponse(read_body, asyncio.Event())

            state = {**model_input(dict(message)), "bio": bio}
            self.assertEqual(await spam_probability(fetcher, CONFIG, state), 0.01)
            self.assertIn(hint, requests[-1]["questions"]["spam"]["instructions"])

    async def test_user_binds_join_prompt_without_changing_ordinary_tasks(self) -> None:
        """Given joining and ordinary inputs, bind only joining tasks to the standalone profile prompt."""
        requests = []

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            requests.append(json.loads(body))

            async def read_body() -> bytes:
                return b'{"answers":{"spam":{"type":"noul","noul":0.5}}}'

            return StreamingResponse(read_body, asyncio.Event())

        member: dict[str, object] = {"id": 987654321, "first_name": "Member", "username": "synthetic_member"}
        joined = {**join_model_input(member), "bio": "项目招人合作\uff0c私聊联系 @<contact>"}
        plain = {**model_input({"from": member, "text": "普通讨论"}), "bio": joined["bio"]}
        recruitment = {**model_input({"from": member, "text": "找想干事的兄弟合作"}), "bio": joined["bio"]}
        await spam_probability(fetcher, CONFIG, joined, joining=True)
        await spam_probability(fetcher, CONFIG, plain)
        await spam_probability(fetcher, CONFIG, recruitment, joining=False)
        # Exercise the same hint-bearing state in join mode: ordinary hints must not contaminate its static task.
        await spam_probability(fetcher, CONFIG, recruitment, joining=True)
        self.assertEqual(
            [request["questions"]["spam"]["instructions"] for request in requests],
            [
                JOIN_PROFILE_INSTRUCTIONS,
                INSTRUCTIONS,
                INSTRUCTIONS + "\n" + PROJECT_RECRUITMENT_HINT,
                JOIN_PROFILE_INSTRUCTIONS,
            ],
        )
        self.assertEqual(
            requests[0]["state"],
            {"nickname": "Member", "username": "synthetic_member", "bio": "项目招人合作\uff0c私聊联系 @<contact>"},
        )
        self.assertNotIn(str(member["id"]), json.dumps(requests))

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
