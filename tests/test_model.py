from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING

from anti_fwd_spam.constants import MAX_PROFILE_TEXT, MODEL_PROVIDERS
from anti_fwd_spam.model import (
    ModelConfig,
    ModelRetryError,
    extract_profile_handles,
    join_model_input,
    model_input,
    sender_bio,
    spam_probability,
)
from anti_fwd_spam.rules import INSTRUCTIONS, JOIN_PROFILE_INSTRUCTIONS
from tests.test_telegram import ClockLoop

if TYPE_CHECKING:
    import builtins
    from collections.abc import Awaitable, Callable

CONFIG = ModelConfig("https://openrouter.ai/api/alpha/decisions", "openai/gpt-6-luna-decisions", "test-key")


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


class NativeDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_configures_only_image_capable_luna_routes(self) -> None:
        """user: Given model credentials, When routes are selected, Then only the four Luna platforms participate."""
        self.assertEqual(
            list(MODEL_PROVIDERS),
            ["EXPERIENTIAL_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY"],
        )
        self.assertEqual(
            MODEL_PROVIDERS["EXPERIENTIAL_API_KEY"],
            ("https://api.experientiallabs.ai/v1/decisions", "gpt-6-luna-decisions"),
        )

    async def test_user_classifies_with_native_decisions_on_luna_platforms(self) -> None:
        """Luna receives the same bounded evidence and scoped prompts on native platforms."""
        self.assertEqual(
            list(MODEL_PROVIDERS),
            ["EXPERIENTIAL_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY"],
        )
        state = {**model_input({"from": {"first_name": "Member"}, "text": "普通讨论"}), "bio": None}
        for key, expected_model in (
            ("EXPERIENTIAL_API_KEY", "gpt-6-luna-decisions"),
            ("OPENAI_API_KEY", "gpt-6-luna"),
            ("AI_GATEWAY_API_KEY", "openai/gpt-6-luna-decisions"),
        ):
            endpoint, model = MODEL_PROVIDERS[key]
            self.assertTrue(endpoint.endswith("/v1/decisions"))
            self.assertEqual(model, expected_model)
            config = ModelConfig(endpoint, model, "test-key")
            for joining in (False, True):
                with self.subTest(provider=key, joining=joining):

                    async def fetcher(
                        url, *, method, headers, body, expected=(endpoint, expected_model, joining)
                    ) -> StreamingResponse:
                        endpoint, expected_model, joining = expected
                        self.assertEqual(url, endpoint)
                        self.assertEqual(method, "POST")
                        self.assertEqual(
                            headers, {"authorization": "Bearer test-key", "content-type": "application/json"}
                        )
                        self.assertEqual(
                            json.loads(body),
                            {
                                "model": expected_model,
                                "input": json.dumps(state, ensure_ascii=False),
                                "questions": [
                                    {
                                        "name": "spam",
                                        "type": "predicate",
                                        "instructions": JOIN_PROFILE_INSTRUCTIONS if joining else INSTRUCTIONS,
                                    }
                                ],
                            },
                        )

                        async def read_body() -> bytes:
                            return b'{"answers":[{"name":"spam","type":"predicate","probability":0.95}]}'

                        return StreamingResponse(read_body, asyncio.Event())

                    self.assertEqual(await spam_probability(fetcher, config, state, joining=joining), 0.95)

    async def test_user_openrouter_uses_luna_prompts_with_systemone_wire_format(self) -> None:
        """OpenRouter's Decisions endpoint uses a keyed Noul contract, not native predicates."""
        url, model = MODEL_PROVIDERS["OPENROUTER_API_KEY"]
        self.assertEqual((url, model), ("https://openrouter.ai/api/alpha/decisions", "openai/gpt-6-luna-decisions"))
        state = {**model_input({"from": {"first_name": "Member"}, "text": "普通讨论"}), "bio": None}

        async def fetcher(url, *, method, headers, body) -> StreamingResponse:
            self.assertEqual(
                json.loads(body),
                {
                    "model": model,
                    "state": state,
                    "questions": {"spam": {"type": "noul", "instructions": INSTRUCTIONS}},
                },
            )

            async def read_body() -> bytes:
                return b'{"answers":{"spam":{"type":"noul","noul":0.9}}}'

            return StreamingResponse(read_body, asyncio.Event())

        self.assertEqual(await spam_probability(fetcher, ModelConfig(url, model, "test-key"), state), 0.9)

    async def test_user_does_not_treat_refusals_or_mismatched_decisions_as_spam(self) -> None:
        """Only one correctly named predicate with a valid probability is a model verdict."""
        config = ModelConfig("https://api.experientiallabs.ai/v1/decisions", "gpt-6-luna-decisions", "test-key")
        valid = {"name": "spam", "type": "predicate", "probability": 0.99}
        envelopes = [
            {},
            {"answers": []},
            {"answers": [valid, valid]},
            {"answers": {"spam": {"type": "noul", "noul": 1}}},
            {"answers": [{"name": "spam", "type": "refusal"}]},
            {"answers": [{**valid, "name": "other"}]},
            {"answers": [{"type": "predicate", "probability": 1}]},
            {"answers": [{**valid, "type": "choice"}]},
            {"answers": [None]},
        ]
        envelopes.extend(
            {"answers": [{**valid, "probability": value}]} for value in (True, "0.99", -0.1, 1.1, None, float("nan"))
        )
        for envelope in envelopes:
            with self.subTest(envelope=envelope):

                async def fetcher(url, *, method, headers, body, envelope=envelope) -> StreamingResponse:
                    async def read_body() -> bytes:
                        return json.dumps(envelope).encode()

                    return StreamingResponse(read_body, asyncio.Event())

                self.assertIsNone(await spam_probability(fetcher, config, {}))


class ModelStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_requests_independent_spam_detection_for_a_reply(self) -> None:
        """user: Given an ad reply, When classified, Then the current sender has an independent task."""
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
        self.assertEqual(requests[0]["questions"]["spam"]["instructions"], INSTRUCTIONS)

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
        # A message-shaped state still uses only the join task when the caller selects join scope.
        await spam_probability(fetcher, CONFIG, recruitment, joining=True)
        self.assertEqual(
            [request["questions"]["spam"]["instructions"] for request in requests],
            [
                JOIN_PROFILE_INSTRUCTIONS,
                INSTRUCTIONS,
                INSTRUCTIONS,
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
