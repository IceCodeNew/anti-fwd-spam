from __future__ import annotations

import itertools
import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

TRIGGER = "/start 501 @example_bot @sample_bot @demo_bot"
RESOURCE_LINES = (
    "点我搜索 酒店偷拍",
    "看情侣实时直播",
    "清纯校花白虎被金主内射",
    "媚黑国女被狂草",
    "福利姬付费门槛免费看",
    "限时免费搜索",
    "暴力强奸",
    "万能检索 千万用户的选择",
)
RESOURCE_BLOCK = "\n".join(RESOURCE_LINES)
RESOURCE_MESSAGE = "点下方链接 限时免费看禁忌资源\n\n" + (RESOURCE_BLOCK + "\n\n") * 3 + "点上方链接 限时免费看禁忌资源"


class ResourcePatternTests(unittest.TestCase):
    def test_start_trigger_requires_a_complete_numeric_multi_bot_command(self) -> None:
        for text in (
            TRIGGER,
            " \n\t" + TRIGGER + "\r\n ",
            "/start\t501\n@sample_bot\n@demo_bot",
            "/start " + chr(0xFF15) + "01 @aabot @" + "x" * 29 + "BOT",
            "/start 999 @different_bot @another_bot",
        ):
            for field in ("text", "caption"):
                with self.subTest(text=text, field=field):
                    self.assertTrue(matches_spam_pattern({field: text}))
            self.assertFalse(matches_spam_text(text))

    def test_start_does_not_match_plain_commands_mentions_or_framed_samples(self) -> None:
        for text in (
            "/start",
            "/start 501",
            "/start 501 @demo_bot",
            "/start 501 @alice_user @bobby_user",
            "/start @alice_bot @bobby_bot",
            "/start abc @alice_bot @bobby_bot",
            "/restart 501 @alice_bot @bobby_bot",
            "/start 501 @abot @bobby_bot",
            "/start 501 @" + "x" * 30 + "bot @bobby_bot",
            "/start 501 @alice_bot @bobby_bot_extra",
            "/start 501 @alice_bot,@bobby_bot",
            "@alice_bot @bobby_bot 看一下",
            "请举报\uff1a" + TRIGGER,
            TRIGGER + "\n这是一条垃圾广告",
            "```\n" + TRIGGER + "\n```",
        ):
            with self.subTest(text=text):
                self.assertFalse(matches_spam_pattern({"text": text}))

    def test_start_in_profiles_or_references_does_not_match_authored_signature(self) -> None:
        for field in ("from", "contact"):
            self.assertFalse(matches_spam_pattern({field: {"first_name": TRIGGER}, "text": "普通聊天"}))
        for field in ("reply_to_message", "quote", "external_reply"):
            self.assertFalse(matches_spam_pattern({field: {"text": TRIGGER}, "text": "普通聊天"}))

    def test_resource_message_and_independent_combinations_match(self) -> None:
        for text in (
            RESOURCE_MESSAGE,
            RESOURCE_BLOCK,
            "限时免费看禁忌资源",
            "点上方链接 限时免费看禁忌资源",
            "点下方链接 限时免费看禁忌资源",
            "万能检索\n千万用户的选择",
            "千万用户的选择\n万能检索",
            *("点我搜索\n" + term for term in ("偷拍", "直播", "福利姬", "内射", "狂草", "资源")),
            "酒店偷拍\n点我搜索",
            "酒店偷拍\n看情侣实时直播\n清纯校花白虎被金主内射",
            "暴力强奸\n媚黑国女被狂草\n限时免费搜索",
        ):
            for field in ("text", "caption"):
                with self.subTest(text=text[:40], field=field):
                    self.assertTrue(matches_spam_pattern({field: text}))
            self.assertTrue(matches_spam_text(text))

    def test_resource_variants_tolerate_bounded_noise_and_spelling(self) -> None:
        for separator in ("\u200b", "\u200c\u200d", "\ufe0f", "🔎", "\u3000", "\uff0c", "\uff64"):
            for text in ("限时免费看禁忌资源", "万能检索 千万用户", "点我搜索 偷拍"):
                with self.subTest(separator=repr(separator), text=text):
                    self.assertTrue(matches_spam_text(separator.join(text)))
        for text in (
            "限時免費看禁禁資源",
            "萬能檢索 千萬擁戶的選擇",
            "點我搜索 內射",
            "酒店偷拍\n情侶實時直播\n福利鸡付費門檻免費看",
            RESOURCE_MESSAGE.replace("\n", "\r\n"),
        ):
            with self.subTest(text=text[:40]):
                self.assertTrue(matches_spam_text(text))

    def test_three_distinct_list_features_match_in_any_order_but_not_repetitions(self) -> None:
        # Remove the independent banners and search-entry combination to isolate
        # the three-feature rule through the public policy matcher.
        lines = ("酒店偷拍", *RESOURCE_LINES[1:7], "万能检索")
        for features in itertools.combinations(lines, 3):
            for ordered in (features, features[::-1]):
                with self.subTest(features=ordered):
                    self.assertTrue(matches_spam_text("\n".join(ordered)))
        for features in itertools.combinations(lines, 2):
            with self.subTest(features=features):
                self.assertFalse(matches_spam_text("\n".join(features * 3)))
        for line in lines:
            self.assertFalse(matches_spam_text((line + "\n") * 3))
        self.assertFalse(matches_spam_text("酒店偷拍\n" + "甲" * 385 + "看情侣实时直播\n暴力强奸"))
        self.assertFalse(matches_spam_text("万能检索" + "甲" * 129 + "千万用户"))
        self.assertFalse(matches_spam_text("点我搜索" + "甲" * 129 + "资源"))

    def test_ordinary_discussion_and_isolated_sensitive_terms_stay_unmatched(self) -> None:
        for text in (
            "新闻报道暴力强奸案件\uff0c呼吁保护受害者",
            "讨论酒店偷拍的防范与受害者保护",
            "请举报偷拍和禁忌资源",
            "如何搜索开源资源\uff1f",
            "点我搜索 Python 文档",
            "直播搜索功能怎么实现\uff1f",
            "万能检索功能如何设计\uff1f",
            "千万用户的体验反馈",
            "限时免费搜索学术论文",
            "暴力强奸\n暴力强奸\n暴力强奸",
            "点下方链接搜索酒店价格",
            "限时免费看电影资源",
        ):
            with self.subTest(text=text):
                self.assertFalse(matches_spam_pattern({"text": text}))

    def test_resource_evidence_does_not_accumulate_from_other_fields_or_references(self) -> None:
        for field in ("reply_to_message", "quote", "external_reply"):
            self.assertFalse(matches_spam_pattern({"text": "请处理这些垃圾广告", field: {"text": RESOURCE_MESSAGE}}))
        self.assertFalse(matches_spam_pattern({"text": "万能检索", "caption": "千万用户"}))
        self.assertFalse(matches_spam_pattern({"text": "点我搜索", "from": {"first_name": "资源"}}))
        self.assertFalse(matches_spam_pattern({"text": "酒店偷拍", "caption": "情侣实时直播\n暴力强奸"}))


if __name__ == "__main__":
    unittest.main()
