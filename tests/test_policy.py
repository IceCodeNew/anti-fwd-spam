from __future__ import annotations

import unittest

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text


class SpamPatternTests(unittest.TestCase):
    def test_user_matches_disabled_group_replacement_notice(self) -> None:
        """user: Given a disabled-group replacement notice, When matched, Then local filtering flags it."""
        for text in (
            "本群已停用\uff0c请加入新群一起聊天\uff1ahttps://example.invalid/new-group",
            "频道搬迁了\uff0c点击新地址加入新频道\uff1ahttps://example.invalid/new-channel",
            "群组关闭\uff0c移步新群继续讨论 https://example.invalid/moved",
        ):
            with self.subTest(text=text):
                self.assertTrue(matches_spam_pattern({"text": text}))

    def test_user_requires_both_disabled_status_and_new_destination(self) -> None:
        """user: Given incomplete migration language, When matched, Then ordinary text stays unflagged."""
        for text in (
            "本群已停用\uff0c我已经举报了这个假通知。",
            "请加入新群一起聊天\uff1ahttps://example.invalid/new-group",
            "群聊什么时候停用\uff1f有没有备份\uff1f",
            "不要加入新群\uff0c这是诈骗广告。",
        ):
            with self.subTest(text=text):
                self.assertFalse(matches_spam_pattern({"text": text}))

    def test_user_keeps_replacement_notice_out_of_profile_regex(self) -> None:
        """user: Given a profile containing a replacement notice, When matched, Then it stays model-only context."""
        self.assertFalse(matches_spam_text("本群已停用\uff0c请加入新群\uff1ahttps://example.invalid/new-group"))
