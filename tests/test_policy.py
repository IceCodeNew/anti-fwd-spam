from __future__ import annotations

import unittest
from typing import cast

from anti_fwd_spam.policy import (
    DEFAULT_BLACKLIST_BOT_IDS,
    MAX_TELEGRAM_ID,
    Config,
    ConfigError,
    extract_deletion_target,
)

BOT_TOKEN = f"{123}:test-token"
WEBHOOK_SECRET = "test-secret"  # noqa: S105
POSTBOT_ID = 273_234_066
CHAT_ID = -100_273_234_066
MESSAGE_ID = 41


def bot(identifier: int = POSTBOT_ID, **extra: object) -> dict[str, object]:
    return {"id": identifier, "is_bot": True, **extra}


def update_with(
    *,
    provenance: dict[str, object] | None = None,
    field: str = "message",
    chat_type: str = "supergroup",
    sender: dict[str, object] | None = None,
    media: bool = False,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": MESSAGE_ID,
        "date": 1,
        "chat": {"id": CHAT_ID, "type": chat_type},
        "from": sender or {"id": 7, "is_bot": False, "first_name": "Member"},
    }
    if provenance:
        message.update(provenance)
    if media:
        message["photo"] = [{"file_id": "x", "file_unique_id": "y", "width": 1, "height": 1}]
    else:
        message["text"] = "message"
    return {"update_id": 99, field: message}


def configured(*, bot_ids: str = DEFAULT_BLACKLIST_BOT_IDS) -> Config:
    return Config.from_values(bot_token=BOT_TOKEN, webhook_secret=WEBHOOK_SECRET, bot_ids=bot_ids)


class ConfigTests(unittest.TestCase):
    def test_defaults_to_postbot_numeric_id(self) -> None:
        self.assertEqual(configured().bot_ids, frozenset({POSTBOT_ID}))

    def test_parses_deduplicated_numeric_ids(self) -> None:
        config = configured(bot_ids=f"777, {MAX_TELEGRAM_ID},777")
        self.assertEqual(config.bot_ids, frozenset({777, MAX_TELEGRAM_ID}))

    def test_rejects_malformed_required_and_blacklist_values(self) -> None:
        cases = (
            {"bot_token": "bad", "webhook_secret": WEBHOOK_SECRET},
            {"bot_token": BOT_TOKEN, "webhook_secret": "é"},
            {"bot_token": BOT_TOKEN, "webhook_secret": WEBHOOK_SECRET, "bot_ids": ""},
            {"bot_token": BOT_TOKEN, "webhook_secret": WEBHOOK_SECRET, "bot_ids": "-1"},
            {"bot_token": BOT_TOKEN, "webhook_secret": WEBHOOK_SECRET, "bot_ids": str(MAX_TELEGRAM_ID + 1)},
            {"bot_token": BOT_TOKEN, "webhook_secret": WEBHOOK_SECRET, "bot_ids": []},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ConfigError):
                Config.from_values(**values)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = configured()

    def test_matches_via_bot_id_without_reading_username(self) -> None:
        absent = update_with(provenance={"via_bot": bot()})
        changed = update_with(provenance={"via_bot": bot(username="RenamedBot")})
        malformed = update_with(provenance={"via_bot": bot(username=[])})
        for candidate in (absent, changed, malformed):
            self.assertEqual(extract_deletion_target(candidate, self.config), (CHAT_ID, MESSAGE_ID))

    def test_same_username_and_neighboring_ids_do_not_match(self) -> None:
        cases = (
            update_with(provenance={"via_bot": bot(POSTBOT_ID - 1, username="PostBot")}),
            update_with(provenance={"via_bot": bot(POSTBOT_ID + 1, username="PostBot")}),
        )
        for candidate in cases:
            self.assertIsNone(extract_deletion_target(candidate, self.config))

    def test_matches_only_forward_origin_user_sender_bot_id(self) -> None:
        matched = update_with(
            provenance={"forward_origin": {"type": "user", "date": 1, "sender_user": bot(username=None)}}
        )
        self.assertEqual(extract_deletion_target(matched, self.config), (CHAT_ID, MESSAGE_ID))

        for origin_type in ("hidden_user", "chat", "channel"):
            origin: dict[str, object] = {
                "type": origin_type,
                "date": 1,
                "sender_user": bot(),
                "sender_user_name": "PostBot",
                "sender_chat": {"id": POSTBOT_ID, "type": "channel"},
                "chat": {"id": POSTBOT_ID, "type": "channel"},
            }
            self.assertIsNone(extract_deletion_target(update_with(provenance={"forward_origin": origin}), self.config))

    def test_ignores_destination_sender_text_replies_and_direct_bot_authorship(self) -> None:
        current_bot = update_with(sender=bot())
        destination_id = update_with()
        destination_message = destination_id["message"]
        if not isinstance(destination_message, dict):
            self.fail("fixture message must be an object")
        typed_destination = cast("dict[str, object]", destination_message)
        typed_destination["chat"] = {"id": -POSTBOT_ID, "type": "group", "username": "PostBot"}

        surfaces = update_with()
        surfaces_message = surfaces["message"]
        if not isinstance(surfaces_message, dict):
            self.fail("fixture message must be an object")
        typed_surfaces = cast("dict[str, object]", surfaces_message)
        typed_surfaces.update(
            {
                "text": "@PostBot",
                "caption": "PostBot",
                "reply_to_message": {"via_bot": bot()},
            }
        )
        for candidate in (current_bot, destination_id, surfaces):
            self.assertIsNone(extract_deletion_target(candidate, self.config))

    def test_current_sender_has_no_exemption(self) -> None:
        provenance: dict[str, object] = {"via_bot": bot()}
        senders: tuple[dict[str, object], ...] = (
            {"id": 8, "is_bot": False, "first_name": "Member"},
            {"id": 9, "is_bot": False, "first_name": "Admin", "status": "administrator"},
            {"id": 1_087_968_824, "is_bot": False, "first_name": "Group"},
        )
        for sender in senders:
            candidate = update_with(provenance=provenance, sender=sender)
            self.assertEqual(extract_deletion_target(candidate, self.config), (CHAT_ID, MESSAGE_ID))

    def test_handles_message_variants_and_group_types_without_text(self) -> None:
        for field in ("message", "edited_message"):
            for chat_type in ("group", "supergroup"):
                candidate = update_with(provenance={"via_bot": bot()}, field=field, chat_type=chat_type, media=True)
                self.assertEqual(extract_deletion_target(candidate, self.config), (CHAT_ID, MESSAGE_ID))

    def test_ignores_private_channel_and_unrelated_updates(self) -> None:
        cases = (
            update_with(provenance={"via_bot": bot()}, chat_type="private"),
            update_with(provenance={"via_bot": bot()}, chat_type="channel"),
            {"update_id": 4, "callback_query": {}},
        )
        for candidate in cases:
            self.assertIsNone(extract_deletion_target(candidate, self.config))

    def test_rejects_invalid_consumed_shapes_and_ids(self) -> None:
        valid_update = update_with(provenance={"via_bot": bot()})
        valid_message = valid_update["message"]
        if not isinstance(valid_message, dict):
            self.fail("fixture message must be an object")
        without_via = {key: value for key, value in valid_message.items() if key != "via_bot"}
        cases = (
            [],
            {"message": [], "update_id": 1},
            {"message": valid_message, "edited_message": valid_message},
            {"message": {**valid_message, "message_id": True}},
            {"message": {**valid_message, "chat": {"id": 0, "type": "group"}}},
            {"message": {**valid_message, "chat": {"id": -1, "type": []}}},
            {"message": {**valid_message, "via_bot": {"id": POSTBOT_ID, "is_bot": False}}},
            {"message": {**valid_message, "via_bot": {"id": "273234066", "is_bot": True}}},
            {"message": {**without_via, "forward_origin": {"type": []}}},
            {
                "message": {
                    **without_via,
                    "forward_origin": {"type": "user", "sender_user": {"id": POSTBOT_ID, "is_bot": []}},
                }
            },
        )
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises((TypeError, ValueError)):
                extract_deletion_target(candidate, self.config)


if __name__ == "__main__":
    unittest.main()
