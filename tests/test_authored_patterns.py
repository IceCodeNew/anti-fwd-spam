from __future__ import annotations

import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

BULK_OFFER = "承接广*告*戴*發\uff0c十万精准各行业裙  \n包效果 包效果 @sample_contact"
GROUP_ACCOUNT_OFFER = "出+86裙發号 搭配运控端口 不封浩\n2秒發一个裙  日蕟4W @sample_contact"
DISTRIBUTION_OFFER = "接TG厂吿𠰺发/𠰺查同行裙\n日发千万 统统高质量 @sample_contact"
SALES_BUTTON = "1️⃣🔥卖薇信号/手机咔/接码等-啥都有"


class AuthoredPatternTests(unittest.TestCase):
    def test_distribution_templates_require_a_complete_current_offer(self) -> None:
        for offer in (GROUP_ACCOUNT_OFFER, DISTRIBUTION_OFFER):
            for field in ("text", "caption"):
                with self.subTest(offer=offer, field=field):
                    self.assertTrue(matches_spam_pattern({field: " \n" + offer + "\t"}))
                    for framed in (
                        "警惕广告\uff1a" + offer,
                        offer + "\uff0c请勿联系",
                        "“" + offer + "”",
                        offer + "\uff1f",
                    ):
                        self.assertFalse(matches_spam_pattern({field: framed}))
                    for contact in ("", "@a", "@" + "x" * 33):
                        self.assertFalse(matches_spam_pattern({field: offer.replace("@sample_contact", contact)}))
            self.assertFalse(matches_spam_text(offer))
            for field in ("from", "contact", "reply_to_message", "quote", "external_reply"):
                self.assertFalse(matches_spam_pattern({"text": "请核实", field: {"text": offer, "first_name": offer}}))
            first, second = offer.split("\n")
            self.assertFalse(matches_spam_pattern({"text": first, "caption": second}))

    def test_exact_button_offer_requires_an_empty_or_numeric_current_body(self) -> None:
        markup = {"inline_keyboard": [[{"text": SALES_BUTTON}]]}
        for body in ({}, {"text": ""}, {"text": "1792391006137"}, {"caption": " 123\n"}):
            with self.subTest(body=body):
                self.assertTrue(matches_spam_pattern({**body, "reply_markup": markup}))
        for body in ("请举报这个售号菜单", "分析广告样本", "123abc", "🙂"):
            for field in ("text", "caption"):
                self.assertFalse(matches_spam_pattern({field: body, "reply_markup": markup}))
        self.assertFalse(
            matches_spam_pattern(
                {"text": "123", "rich_message": {"text": "请举报这个售号菜单"}, "reply_markup": markup}
            )
        )
        for label in ("警惕\uff1a" + SALES_BUTTON, SALES_BUTTON + "\uff08样本\uff09", "查看教程", "卖账号", ""):
            self.assertFalse(
                matches_spam_pattern({"text": "123", "reply_markup": {"inline_keyboard": [[{"text": label}]]}})
            )
        for key in ("url", "callback_data"):
            self.assertFalse(
                matches_spam_pattern({"reply_markup": {"inline_keyboard": [[{"text": "普通按钮", key: SALES_BUTTON}]]}})
            )
        for field in ("reply_to_message", "quote", "external_reply", "from", "contact"):
            self.assertFalse(
                matches_spam_pattern({"text": "123", field: {"reply_markup": markup, "first_name": SALES_BUTTON}})
            )
        for markup in (None, [], {}, {"inline_keyboard": None}, {"inline_keyboard": [None, [None, {"text": 123}]]}):
            self.assertFalse(matches_spam_pattern({"text": "123", "reply_markup": markup}))

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


if __name__ == "__main__":
    unittest.main()
