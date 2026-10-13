from __future__ import annotations

import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

BULK_OFFER = "承接广*告*戴*發\uff0c十万精准各行业裙  \n包效果 包效果 @sample_contact"


class AuthoredPatternTests(unittest.TestCase):
    def test_complete_offers_match_only_the_current_authored_field(self) -> None:
        for text in (BULK_OFFER,):
            for field in ("text", "caption"):
                with self.subTest(text=text[:12], field=field):
                    self.assertTrue(matches_spam_pattern({field: text}))
                    self.assertTrue(matches_spam_pattern({field: text.replace("\n", "\r\n") + "\n"}))
            self.assertTrue(matches_spam_pattern({"text": text, "reply_to_message": {"text": "Good morning"}}))

    def test_warning_analysis_and_partial_templates_are_not_direct_matches(self) -> None:
        for text in (BULK_OFFER,):
            for framed in (
                "请举报这条广告\uff1a\n" + text,
                text + "\n这条是广告\uff0c请勿联系。",
                "```\n" + text + "\n```",
                "“" + text + "”",
                text.replace("接", "不接", 1),
                text.replace("@sample_contact", ""),
                text.replace("@sample_contact", "@a"),
                text.replace("@sample_contact", "@" + "a" * 33),
            ):
                with self.subTest(framed=framed[:24]):
                    self.assertFalse(matches_spam_pattern({"text": framed}))
        for text in ("如何防御DDOS/CC攻击\uff1f", "昨天营业额一万", "假钞的鉴别方法"):
            self.assertFalse(matches_spam_pattern({"text": text}))

    def test_offers_in_other_peoples_content_or_profiles_do_not_trigger_new_patterns(self) -> None:
        for text in (BULK_OFFER,):
            self.assertFalse(matches_spam_text(text))
            for field in ("contact", "from"):
                self.assertFalse(matches_spam_pattern({"text": "请核实这个广告样本", field: {"first_name": text}}))
            for field in ("reply_to_message", "quote", "external_reply"):
                self.assertFalse(matches_spam_pattern({"text": "请核实这个广告样本", field: {"text": text}}))


if __name__ == "__main__":
    unittest.main()
