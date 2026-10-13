from __future__ import annotations

import unittest
from itertools import permutations, product

from anti_fwd_spam.policy import matches_spam_pattern, matches_spam_text

DDOS_CAMPAIGN = (
    "24小时在线接非法DDOS/CC攻击\n"
    "私服\uff0c服务器\uff0cAPP棋牌\uff0c黄色\uff0c资金盘\uff0c博彩网站\uff0c一切等等\n"
    "宿迁\uff0c杭州\uff0c扬州\uff0c厦门\uff0c成都\uff0c绍兴\uff0c枣庄\uff0c部分台州\uff0c吞金兽\uff0c等等\n"
    "我们只保证足够的力量和打死目标\n秒杀各种CF节点\uff0c无视CDN\uff0c穿cloudfare\n"
    "纯国内流量在线\uff0c精准打击护老板周全\n"
    "欢迎有实力的老板来谈合作\uff0c有项目的联系\uff0c只接稳定单\n"
    "全天在线接单  @example_sales\n全天在线接单  @another_contact"
)
DISTRIBUTION_CAMPAIGN = "承接广*告*戴*發\uff0c十万精准各行业裙  \n包效果 包效果 @example_sales"


class SpamPatternTests(unittest.TestCase):
    def test_user_matches_reviewed_phrases_in_authored_fields(self) -> None:
        for text in (
            *(
                kind + offer
                for kind, offer in product(("萝莉", "约炮", "包夜", "外围", "楼凤"), ("上门", "包夜", "资源"))
            ),
            "极品小萝莉",
            "看头像加Q",
            "@sample campaign_123",
            "💰 💰 💰 💰",
            DDOS_CAMPAIGN,
            "接ddos攻击",
            "接非法CC攻击",
            "接 DDOS / CC 攻击",
            "接cc/ddos攻击",
            "广告代发",
            "广#告#代*发",
            "广🔥🔥告__代.发",
            "需要代发\n24小时在线",
            "全天在线\uff0c需要代发",
            "代发 24H",
            "批量出售飞机号",
            "源头直出飞机号",
            *(
                prefix + verb + suffix
                for prefix, verb, suffix in product(
                    ("批量", "源头"), ("出售", "出手", "直出", "直发", "直卖"), ("", "服务器")
                )
            ),
            "源头直出飞机号是卖什么\uff1f",
            "有人发“源头直出飞机号”\uff0c请举报。",
            "源头直出飞机号\n别信这种广告",
            " 源头直出飞机号",
            "来洗米 日入上W",
            "小白莱洗钱 日赚九千",
            "日收8千\n洗米",
            "洗钱 日撸1w",
            "手速快来搞 日赚1W",
            "手速快莱 日收1W",
            "手\U000e010a速\U000e0145快\U000e013e来搞 日撸\U000e01d41W",
            "手速快日撸1W",
            "来搞日赚1W",
            "日🔥入上万\uff0c莱",
            "只吸引对的人",
            "CC攻击测试打死目标",
            "DDOS攻击测试打穿",
            "日死目标\nCC攻击测试",
            "攻击测试DDOS日穿",
            "接攻击测试",
            "攻击测试打穿",
            "我把“批量出售飞机号”这个昵称举报了。",
            "请举报这个广告\uff1a\n" + DDOS_CAMPAIGN,
        ):
            for field in ("text", "caption"):
                with self.subTest(field=field, text=text):
                    self.assertTrue(matches_spam_pattern({field: text}))
            with self.subTest(profile=text):
                self.assertTrue(matches_spam_text(text))

    def test_user_combines_elements_without_order_or_cross_field_matching(self) -> None:
        for elements in (
            *product(("DDOS", "CC"), ("攻击",), ("接", "打死", "打穿", "日死", "日穿")),
            *product(("攻击测试",), ("接", "打死", "打穿", "日死", "日穿")),
            ("代发", "24小时"),
            *product(("洗米", "洗钱", "手速快", "来", "莱"), ("日赚", "日收", "日撸", "日入")),
        ):
            for order in permutations(elements):
                with self.subTest(order=order):
                    self.assertTrue(matches_spam_text("\n".join(order)))
            self.assertFalse(matches_spam_pattern({"text": elements[0], "caption": "\n".join(elements[1:])}))

    def test_user_keeps_unapproved_variants_and_reference_text_out(self) -> None:
        for text in (
            "极品小萝莉塔",
            "萝莉",
            "外围",
            "包夜",
            "资源",
            "上门",
            "看头像加QQ音乐",
            "@sample CAMPAIGN_123",
            "💰 💰 💰",
            DISTRIBUTION_CAMPAIGN,
            "广###告代发",
            "广a告代发",
            "广新告代发",
            "我们遭到DDOS攻击",
            "接DDOS防御",
            "接攻击",
            "接access攻击",
            "广告",
            "代发",
            "24小时在线",
            "批量",
            "出售飞机号",
            "源头直供",
            "源头直接发货",
            "源头直 出",
            "源头",
            "直卖",
            "源头 出售",
            "出售源头",
            "来洗米\uff0c放进电饭锅。",
            "日赚九千",
            "手速快来搞",
            "只吸引对的人\n",
            " 只吸引对的人",
            "只吸引对的人。",
            "攻击测试",
            "DDOS打死目标",
            "打穿目标",
            "国庆快乐呀大家",
        ):
            with self.subTest(text=text):
                self.assertFalse(matches_spam_pattern({"text": text}))
        self.assertFalse(matches_spam_pattern({"text": "请举报", "reply_to_message": {"text": DDOS_CAMPAIGN}}))
        self.assertFalse(matches_spam_pattern({"text": "请举报", "reply_to_message": {"text": "源头直出飞机号"}}))

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
