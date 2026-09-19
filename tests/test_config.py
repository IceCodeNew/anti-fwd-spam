import unittest

from anti_fwd_spam.policy import Config, ConfigError


class ConfigurationTests(unittest.TestCase):
    def test_user_configures_numeric_sources(self) -> None:
        """user: Given duplicate numeric IDs, When configured, Then both sources remain active."""
        config = Config.from_values(bot_token="123:token", webhook_secret="secret", bot_ids="777, 4503599627370495,777")
        self.assertEqual(config.bot_ids, {777, 4503599627370495})

    def test_user_configures_reporters(self) -> None:
        """user: Given mixed user and chat IDs, When configured, Then one list retains both identities."""
        config = Config.from_values(
            bot_token="123:token",
            webhook_secret="secret",
            reporter_ids="11, 4503599627370495,11, -10012,-4503599627370495",
        )
        self.assertEqual(config.reporter_ids, {11, 4503599627370495, -10012, -4503599627370495})
        empty = Config.from_values(bot_token="123:token", webhook_secret="secret")
        self.assertFalse(empty.reporter_ids)

    def test_user_rejects_invalid_reporter_configuration(self) -> None:
        """user: Given malformed identity lists, When configured, Then no permissive fallback is enabled."""
        for value in (None, [], "0", "4503599627370496", "@someone", "1.0", "true", "\u0661\u0661", "1," * 501):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_values(bot_token="123:token", webhook_secret="secret", reporter_ids=value)

    def test_user_rejects_invalid_configuration(self) -> None:
        """user: Given invalid credentials or source IDs, When configured, Then startup fails explicitly."""
        for replacement in (
            {"bot_token": "bad"},
            {"bot_token": "0:token"},
            {"webhook_secret": "é"},
            {"bot_ids": ""},
            {"bot_ids": "-1"},
            {"bot_ids": "4503599627370496"},
            {"bot_ids": []},
        ):
            with self.subTest(replacement=replacement), self.assertRaises(ConfigError):
                Config.from_values(**{"bot_token": "123:token", "webhook_secret": "secret", **replacement})
