from __future__ import annotations

import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

DISTRIBUTION_OFFERS = (
    "出+86裙發号 搭配运控端口 不封浩\n2秒發一个裙  日蕟4W @sample_contact",
    "接TG厂吿𠰺发/𠰺查同行裙\n日发千万 统统高质量 @sample_contact",
)

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

    def test_user_matches_each_complete_current_distribution_offer(self) -> None:
        """user: Given a complete offer, When authored in text or caption, Then its local signature matches."""
        for offer in (BULK_OFFER, *DISTRIBUTION_OFFERS):
            for field in ("text", "caption"):
                with self.subTest(offer=offer[:20], field=field):
                    self.assertTrue(matches_spam_pattern({field: offer}))
                    self.assertTrue(matches_spam_pattern({field: offer.replace("\n", "\r\n") + "\n"}))
            self.assertTrue(matches_spam_pattern({"text": offer, "reply_to_message": {"text": "Good morning"}}))

    def test_user_keeps_framed_or_incomplete_offers_out_of_local_signatures(self) -> None:
        """user: Given warnings or incomplete templates, When filtered, Then these authored signatures do not match."""
        for offer in (BULK_OFFER, *DISTRIBUTION_OFFERS):
            framed_cases = (
                "请举报这条广告\uff1a\n" + offer,
                offer + "\n这条是广告\uff0c请勿联系。",
                "```\n" + offer + "\n```",
                "“" + offer + "”",
                offer.replace("@sample_contact", ""),
                offer.replace("@sample_contact", "@a"),
                offer.replace("@sample_contact", "@" + "a" * 33),
            )
            for framed in framed_cases:
                with self.subTest(framed=framed[:24]):
                    self.assertFalse(matches_spam_pattern({"text": framed}))
        self.assertFalse(matches_spam_pattern({"text": BULK_OFFER.replace("接", "不接", 1)}))
        for text in ("如何防御DDOS/CC攻击\uff1f", "昨天营业额一万", "假钞的鉴别方法"):
            self.assertFalse(matches_spam_pattern({"text": text}))

    def test_user_keeps_profile_reference_and_split_fields_out_of_authored_signatures(self) -> None:
        """user: Given offers outside one current field, When filtered, Then no authored signature applies."""
        for offer in (BULK_OFFER, *DISTRIBUTION_OFFERS):
            self.assertFalse(matches_spam_text(offer))
            for field in ("contact", "from"):
                self.assertFalse(matches_spam_pattern({"text": "请核实这个广告样本", field: {"first_name": offer}}))
            for field in ("reply_to_message", "quote", "external_reply"):
                self.assertFalse(matches_spam_pattern({"text": "请核实这个广告样本", field: {"text": offer}}))
            first, rest = offer.split("\n", 1)
            self.assertFalse(matches_spam_pattern({"text": first, "caption": rest}))

    def test_user_limits_sales_menu_signature_to_unframed_current_buttons(self) -> None:
        """user: Given an exact menu, When only a neutral body is present, Then the button is a local offer."""
        label = "1️⃣🔥卖薇信号/手机咔/接码等-啥都有"
        markup = {"inline_keyboard": [[{"text": label, "url": "https://example.invalid/menu"}]]}
        for body in ({}, {"text": ""}, {"caption": "123"}, {"text": " 123 ", "caption": "\n"}):
            with self.subTest(body=body):
                self.assertTrue(matches_spam_pattern({**body, "reply_markup": markup}))
        negatives: list[dict[str, object]] = [
            {
                "text": "123",
                "reply_markup": {
                    "inline_keyboard": [[{"text": "警告\uff1a以下是广告样本\uff0c请勿联系"}], [{"text": label}]]
                },
            },
            {"text": "123", "poll": {"question": "这条广告为什么是骗局\uff1f"}, "reply_markup": markup},
            {"text": "123", "checklist": {"title": "举报广告样本"}, "reply_markup": markup},
            {"text": "请举报这个菜单", "reply_markup": markup},
            {"text": "123", "rich_message": {"text": "请勿联系"}, "reply_markup": markup},
            {"text": "123", "rich_message": None, "reply_markup": markup},
            {"text": "123", "caption": "请核实", "reply_markup": markup},
            {"text": "123", "reply_to_message": {"reply_markup": markup}},
            {
                "text": "123",
                "reply_markup": {"inline_keyboard": [[{"text": "normal", "url": label, "callback_data": label}]]},
            },
            {"text": "123", "reply_markup": {"inline_keyboard": [[{"text": " " + label}]]}},
            {"text": "123", "reply_markup": {"inline_keyboard": [[{"text": label + " 请勿联系"}]]}},
            {"text": "\uff11\uff12\uff13", "reply_markup": markup},
            {"text": None, "reply_markup": markup},
            {"text": "123", "reply_markup": {"inline_keyboard": [None, "row", [None, {"text": 12}]]}},
        ]
        for message in negatives:
            with self.subTest(message=message):
                self.assertFalse(matches_spam_pattern(message))


if __name__ == "__main__":
    unittest.main()
