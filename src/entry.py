"""Cloudflare Python Worker entrypoint."""

from __future__ import annotations

import hmac
import logging
import time
from http import HTTPMethod
from urllib.parse import urlsplit

from workers import Request, Response, WorkerEntrypoint, fetch

from anti_fwd_spam.evidence import ReportStore
from anti_fwd_spam.model import MODEL_PROVIDERS, ModelConfig
from anti_fwd_spam.moderation import AppResponse, Moderator
from anti_fwd_spam.policy import DEFAULT_BLACKLIST_BOT_IDS, Config, ConfigError
from anti_fwd_spam.tasks import ModelTasks

MAX_UPDATE_BYTES = 1_048_576


def _environment_string(env: object, name: str, *, default: str | None = None) -> str | None:
    value = getattr(env, name, default)
    if value is not None and not isinstance(value, str):
        message = f"{name} must be a string"
        raise ConfigError(message)
    return value


class Default(WorkerEntrypoint):
    """Serve authenticated Telegram webhook requests."""

    async def fetch(self, request: Request) -> Response:  # noqa: C901, PLR0911
        """Validate one request and dispatch it to the pure Python application."""
        try:
            config = self._get_config()
            bot_username = _environment_string(self.env, "BOT_USERNAME")
            models = self._get_models()
        except ConfigError:
            return _response(AppResponse(500, "invalid worker configuration"))
        if (
            not bot_username
            or not bot_username.isascii()
            or not all(character.isalnum() or character == "_" for character in bot_username)
        ):
            return _response(AppResponse(500, "invalid BOT_USERNAME configuration"))

        path = urlsplit(request.url).path
        if path != "/webhook":
            return _response(AppResponse(404, "not found"))
        if request.method is not HTTPMethod.POST:
            return _response(AppResponse(405, "method not allowed"))

        secret = request.headers.get("x-telegram-bot-api-secret-token")
        if secret is None or not secret.isascii() or not hmac.compare_digest(secret, config.webhook_secret):
            return _response(AppResponse(401, "unauthorized"))

        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
                if parsed_length < 0:
                    return _response(AppResponse(400, "invalid content-length"))
                if parsed_length > MAX_UPDATE_BYTES:
                    return _response(AppResponse(413, "update too large"))
            except ValueError:
                return _response(AppResponse(400, "invalid content-length"))

        try:
            body = await _read_bounded_body(request, MAX_UPDATE_BYTES)
        except ValueError:
            return _response(AppResponse(413, "update too large"))

        app_response = await Moderator(
            config,
            fetch,
            ReportStore(getattr(self.env, "REPORTS", None)),
            bot_username,
            models,
        ).process(request.headers.get("content-type"), body)
        if any(marker in app_response.body for marker in ("failed", "rejected", "retry")):
            logging.getLogger(__name__).warning("Moderation outcome: %s", app_response.body)
        return _response(app_response)

    def _get_config(self) -> Config:
        bot_token = _environment_string(self.env, "BOT_TOKEN")
        webhook_secret = _environment_string(self.env, "TELEGRAM_WEBHOOK_SECRET")
        bot_ids = _environment_string(self.env, "BLACKLIST_BOT_IDS", default=DEFAULT_BLACKLIST_BOT_IDS)
        if bot_ids is None:
            message = "BLACKLIST_BOT_IDS must be a string"
            raise ConfigError(message)
        return Config.from_values(bot_token=bot_token, webhook_secret=webhook_secret, bot_ids=bot_ids)

    def _get_models(self) -> tuple[ModelConfig, ...]:
        return tuple(
            ModelConfig(url, model, key)
            for name, (url, model) in MODEL_PROVIDERS.items()
            if (key := _environment_string(self.env, name))
        )

    async def scheduled(self, controller: object, _env: object, _ctx: object) -> None:
        """Expire retained evidence and process one due model task."""
        # Cloudflare supplies scheduledTime dynamically on the controller.
        now = int(getattr(controller, "scheduledTime") // 1000)  # noqa: B009
        now = max(now, int(time.time()))
        store = ReportStore(self.env.REPORTS)
        await store.expire(now)
        config = self._get_config()
        tasks = ModelTasks(store, int(config.bot_token.split(":", 1)[0]))
        await tasks.expire(now)
        models = self._get_models()
        if models:
            task = await tasks.claim(now)
            if task is not None:
                await tasks.run(task, fetch, config.bot_token, models, now)


async def _read_bounded_body(request: Request, maximum: int) -> bytes:
    reader = request.body.getReader()
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            result = await reader.read()
            if result.done:
                return b"".join(chunks)
            chunk = result.value.to_bytes()
            total += len(chunk)
            if total > maximum:
                await reader.cancel("request body exceeds limit")
                message = "request body exceeds limit"
                raise ValueError(message)
            chunks.append(chunk)
    finally:
        reader.releaseLock()


def _response(result: AppResponse) -> Response:
    return Response(result.body, status=result.status, headers={"content-type": "text/plain; charset=utf-8"})
