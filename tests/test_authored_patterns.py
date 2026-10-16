from __future__ import annotations

import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

BULK_OFFER = "承接广*告*戴*發\uff0c十万精准各行业裙  \n包效果 包效果 @sample_contact"


class AuthoredPatternTests(unittest.TestCase):
    def test_income_link_buttons_use_only_complete_current_button_labels(self) -> None:
        for text in ("💰一天七八万随便拿💰", "💰一天6000\uff0c轻松到手💰", "一\u200b天七💎八万随便拿"):
            with self.subTest(text=text):
                markup = {"inline_keyboard": [[{"text": text, "url": "https://example.invalid/offer"}]]}
                self.assertTrue(matches_spam_pattern({"text": "1", "reply_markup": markup}))
                self.assertFalse(matches_spam_pattern({"text": "1", "reply_to_message": {"reply_markup": markup}}))
                self.assertFalse(matches_spam_pattern({"text": text}))
                self.assertFalse(matches_spam_pattern({"reply_markup": {"inline_keyboard": [[{"text": text}]]}}))
        for text in (
            "财富入口",
            "0本金搬砖",
            "一天6000步",
            "一天6000元住院费",
            "一天七八万随便拿是骗局",
            "举报一天七八万随便拿",
        ):
            with self.subTest(text=text):
                markup = {"inline_keyboard": [[{"text": text, "url": "https://example.invalid/info"}]]}
                self.assertFalse(matches_spam_pattern({"reply_markup": markup}))

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
