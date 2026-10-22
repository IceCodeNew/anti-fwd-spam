from __future__ import annotations

import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

BULK_OFFER = "承接广*告*戴*發\uff0c十万精准各行业裙  \n包效果 包效果 @sample_contact"


class AuthoredPatternTests(unittest.TestCase):
    def test_repeated_symbols_accumulate_across_current_visible_fields(self) -> None:
        for labels, expected in (
            (["💰甲💰", "乙💰"], False),
            (["💰甲💰", "乙💰\u200b丙💰"], True),
            (["💰甲💰乙💰丙💰"], True),
            (["🔴甲🔴", "🔴乙🔴"], True),
            (["💰甲💰", "🔴乙🔴"], False),
        ):
            with self.subTest(labels=labels):
                markup = {"inline_keyboard": [[{"text": label}] for label in labels]}
                self.assertEqual(matches_spam_pattern({"text": "1", "reply_markup": markup}), expected)
                self.assertFalse(matches_spam_pattern({"reply_to_message": {"reply_markup": markup}}))
        self.assertFalse(
            matches_spam_pattern(
                {
                    "text": "💰",
                    "from": {"first_name": "ordinary"},
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "💰甲💰",
                                    "url": "https://example.invalid/💰💰💰💰",
                                    "callback_data": "💰💰💰💰",
                                }
                            ]
                        ]
                    },
                }
            )
        )

        for symbol in ("💰", "🔴"):
            with self.subTest(symbol=symbol):
                self.assertTrue(matches_spam_text(f"{symbol}甲{symbol}\n{symbol}\u200b{symbol}"))
                message: dict[str, object] = {
                    "text": symbol,
                    "from": {"first_name": symbol},
                    "reply_markup": {"inline_keyboard": [[{"text": f"{symbol}甲{symbol}"}]]},
                }
                self.assertTrue(matches_spam_pattern(message))
                message["text"] = "ordinary"
                self.assertFalse(matches_spam_pattern(message))

        self.assertFalse(
            matches_spam_pattern(
                {
                    "text": "批量",
                    "reply_markup": {"inline_keyboard": [[{"text": "出售"}]]},
                }
            )
        )

    def test_complete_offers_match_only_the_current_authored_field(self) -> None:
        for field in ("text", "caption"):
            with self.subTest(field=field):
                self.assertTrue(matches_spam_pattern({field: BULK_OFFER}))
                self.assertTrue(matches_spam_pattern({field: BULK_OFFER.replace("\n", "\r\n") + "\n"}))
        self.assertTrue(matches_spam_pattern({"text": BULK_OFFER, "reply_to_message": {"text": "Good morning"}}))

    def test_warning_analysis_and_partial_templates_are_not_direct_matches(self) -> None:
        for framed in (
            "请举报这条广告\uff1a\n" + BULK_OFFER,
            BULK_OFFER + "\n这条是广告\uff0c请勿联系。",
            "```\n" + BULK_OFFER + "\n```",
            "“" + BULK_OFFER + "”",
            BULK_OFFER.replace("接", "不接", 1),
            BULK_OFFER.replace("@sample_contact", ""),
            BULK_OFFER.replace("@sample_contact", "@a"),
            BULK_OFFER.replace("@sample_contact", "@" + "a" * 33),
        ):
            with self.subTest(framed=framed[:24]):
                self.assertFalse(matches_spam_pattern({"text": framed}))
        for text in ("如何防御DDOS/CC攻击\uff1f", "昨天营业额一万", "假钞的鉴别方法"):
            self.assertFalse(matches_spam_pattern({"text": text}))

    def test_offers_in_other_peoples_content_or_profiles_do_not_trigger_new_patterns(self) -> None:
        self.assertFalse(matches_spam_text(BULK_OFFER))
        for field in ("contact", "from"):
            self.assertFalse(matches_spam_pattern({"text": "请核实这个广告样本", field: {"first_name": BULK_OFFER}}))
        for field in ("reply_to_message", "quote", "external_reply"):
            self.assertFalse(matches_spam_pattern({"text": "请核实这个广告样本", field: {"text": BULK_OFFER}}))

    def test_sexual_endorsement_requires_current_reaction_and_visible_resource_offer(self) -> None:
        offer = "成人cos私密写真资源"
        for current in ("射了", " 射了\uff01 "):
            for reference in (
                {"reply_to_message": {"text": offer}},
                {"reply_to_message": {"caption": offer}},
                {
                    "external_reply": {"origin": {"type": "channel", "chat": {"title": "资源分享"}}},
                    "quote": {"text": offer},
                },
                {"quote": {"text": offer}},
            ):
                for field in ("text", "caption"):
                    with self.subTest(current=current, field=field, reference=reference):
                        self.assertTrue(matches_spam_pattern({field: current, **reference}))

    def test_sexual_endorsement_keeps_warnings_neutral_replies_and_unrelated_context(self) -> None:
        offer = {"reply_to_message": {"text": "成人cos私密写真资源"}}
        for text in (
            "这是什么\uff1f",
            "举报了",
            "别发这种东西",
            "这是广告\uff0c别点",
            "这种广告用“射了”附和引流\uff0c请处理",
            "刚射了以后不舒服\uff0c应该看哪个科\uff1f",
            "有人配文“射了”\uff0c算推广吗\uff1f",
        ):
            with self.subTest(text=text):
                self.assertFalse(matches_spam_pattern({"text": text, **offer}))
        for text in (
            "刚才游戏里谁先开枪\uff1f",
            "成人cos服装裁剪教程",
            "请举报成人私密写真资源广告",
            "这是色情资源广告的新闻分析\uff0c不要传播",
            "色情资源",
        ):
            with self.subTest(reference=text):
                self.assertFalse(matches_spam_pattern({"text": "射了", "reply_to_message": {"text": text}}))
        messages: tuple[dict[str, object], ...] = (
            {"text": "射了", "reply_to_message": {"from": {"first_name": "小资"}, "text": "源成人cos私密写真"}},
            {"text": "射了"},
            {"text": "举报", "caption": "射了", **offer},
            {"text": "射了", "reply_to_message": {"text": "成人cos私密写真资源", "forum_topic_created": {}}},
            {"text": "射了", "reply_to_message": {"from": {"first_name": "成人cos私密写真资源"}}},
            {"text": "ordinary", "from": {"first_name": "射了"}, **offer},
            {"text": "ordinary", "contact": {"first_name": "射了"}, **offer},
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(matches_spam_pattern(message))
        self.assertFalse(matches_spam_text("射了"))


if __name__ == "__main__":
    unittest.main()
