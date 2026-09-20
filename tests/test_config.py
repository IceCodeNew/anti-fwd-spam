import unittest

from anti_fwd_spam.policy import Config, ConfigError


class ConfigurationTests(unittest.TestCase):
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
        """user: Given invalid credentials, When configured, Then startup fails explicitly."""
        for replacement in (
            {"bot_token": "bad"},
            {"bot_token": "0:token"},
            {"webhook_secret": "é"},
        ):
            with self.subTest(replacement=replacement), self.assertRaises(ConfigError):
                Config.from_values(**{"bot_token": "123:token", "webhook_secret": "secret", **replacement})
