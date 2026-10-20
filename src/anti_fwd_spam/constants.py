"""Static data shared by the moderation and model runtime modules.

This module has no application imports; changing a value here must not create
an import cycle between policy parsing and model classification.
"""

from __future__ import annotations

MODEL_TIMEOUT_SECONDS = 8

MAX_MODEL_RESPONSE_BYTES = 65_536

SPAM_THRESHOLD = 0.90

MODEL_PROVIDERS = {
    "EXPERIENTIAL_API_KEY": ("https://api.experientiallabs.ai/v1/decisions", "gpt-6-luna-decisions"),
    "AI_GATEWAY_API_KEY": ("https://ai-gateway.vercel.sh/v1/decisions", "openai/gpt-6-luna-decisions"),
    "TYPESAFE_AI_API_KEY": ("https://api.typesafe.ai/v1/systemone", "jev-latest"),
    "OPENCODE_API_KEY": ("https://opencode.ai/zen/v1/systemone", "jev-1.13"),
    "CMD_API_KEY": ("https://api.commandcode.ai/provider/v1/systemone", "typesafe/jev"),
}

MEDIA_FIELDS = frozenset(
    [
        "animation",
        "audio",
        "document",
        "live_photo",
        "paid_media",
        "photo",
        "sticker",
        "story",
        "video",
        "video_note",
        "voice",
    ],
)

MODEL_CONTENT_FIELDS = MEDIA_FIELDS | frozenset(
    {
        "text",
        "caption",
        "rich_message",
        "checklist",
        "poll",
        "dice",
        "game",
        "contact",
        "location",
        "venue",
        "invoice",
        "giveaway",
    }
)

PUBLIC_PROFILE_HOSTS = frozenset({"t.me", "telegram.me"})

MAX_PROFILE_HANDLES = 3

MAX_PROFILE_BIO_BYTES = 4096

MAX_PROFILE_TEXT = 512

MAX_TELEGRAM_ID = (1 << 52) - 1

MAX_CONFIG_LENGTH = 4_096

MAX_ID_ENTRIES = 500

TOKEN_PART_COUNT = 2

GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})

SUPPORTED_CHAT_TYPES = GROUP_CHAT_TYPES | {"private", "channel"}

SUPPORTED_FORWARD_ORIGINS = frozenset({"user", "hidden_user", "chat", "channel"})
