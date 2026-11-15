"""Cloudflare Python Worker entrypoint."""

from __future__ import annotations

import hmac
import json
import logging
import time
from http import HTTPMethod
from urllib.parse import parse_qs, urlsplit

from workers import Request, Response, WorkerEntrypoint, fetch

from anti_fwd_spam.constants import MODEL_PROVIDERS
from anti_fwd_spam.evidence import EvidenceError, ReportStore
from anti_fwd_spam.model import ModelConfig
from anti_fwd_spam.moderation import AppResponse, Moderator
from anti_fwd_spam.policy import Config, ConfigError
from anti_fwd_spam.tasks import ModelTasks

MAX_UPDATE_BYTES = 1_048_576
MAX_REVIEW_BODY_BYTES = 4_096
MAX_REVIEW_LIMIT = 100
DEFAULT_REVIEW_LIMIT = 20
SCHEDULED_TASKS_PER_RUN = 3
REVIEW_PATHS = frozenset({"/reviews/pending", "/reviews/resolve"})


def _environment_string(env: object, name: str, *, default: str | None = None) -> str | None:
    value = getattr(env, name, default)
    if value is not None and not isinstance(value, str):
        message = f"{name} must be a string"
        raise ConfigError(message)
    return value


class Default(WorkerEntrypoint):
    """Serve authenticated Telegram webhook requests."""

    async def fetch(self, request: Request) -> Response:
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
        if path in REVIEW_PATHS:
            return await self._review(request, config, bot_username, path)
        rejection = _reject_request(request, config.webhook_secret)
        if rejection is not None:
            return _response(rejection)
        try:
            body = await _read_bounded_body(request, MAX_UPDATE_BYTES)
        except ValueError:
            return _response(AppResponse(413, "update too large"))

        moderator = Moderator(
            config,
            fetch,
            ReportStore(getattr(self.env, "REPORTS", None)),
            bot_username,
            models,
        )
        app_response = await moderator.process(request.headers.get("content-type"), body)
        if moderator.background is not None:
            self.ctx.waitUntil(moderator.background)
        if any(marker in app_response.body for marker in ("failed", "rejected", "retry")):
            logging.getLogger(__name__).warning("Moderation outcome: %s", app_response.body)
        return _response(app_response)

    async def _review(  # noqa: PLR0911 - Each return is a distinct HTTP outcome of the review endpoints.
        self, request: Request, config: Config, bot_username: str, path: str
    ) -> Response:
        """Serve the optional review endpoints that expose unlisted reports to an external reviewer."""
        secret = config.review_secret
        if secret is None:
            return _json_response(404, {"error": "not found"})
        provided = request.headers.get("x-review-secret")
        if provided is None or not provided.isascii() or not hmac.compare_digest(provided, secret):
            return _json_response(401, {"error": "unauthorized"})
        moderator = Moderator(
            config,
            fetch,
            ReportStore(getattr(self.env, "REPORTS", None)),
            bot_username,
            self._get_models(),
        )
        if path == "/reviews/pending":
            if request.method is not HTTPMethod.GET:
                return _json_response(405, {"error": "method not allowed"})
            try:
                reviews = await moderator.list_pending_reviews(_review_limit(request.url))
            except EvidenceError:
                return _json_response(503, {"error": "review storage unavailable"})
            return _json_response(200, {"reviews": reviews})
        if request.method is not HTTPMethod.POST:
            return _json_response(405, {"error": "method not allowed"})
        try:
            body = await _read_bounded_body(request, MAX_REVIEW_BODY_BYTES)
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _json_response(400, {"error": "invalid review resolution"})
        if not isinstance(parsed, dict):
            return _json_response(400, {"error": "invalid review resolution"})
        update_id, verdict = parsed.get("update_id"), parsed.get("verdict")
        if type(update_id) is not int or update_id <= 0 or not isinstance(verdict, str):
            return _json_response(400, {"error": "invalid review resolution"})
        result = await moderator.resolve_review(update_id, verdict)
        return _json_response(result.status, {"result": result.body})

    def _get_config(self) -> Config:
        bot_token = _environment_string(self.env, "BOT_TOKEN")
        webhook_secret = _environment_string(self.env, "TELEGRAM_WEBHOOK_SECRET")
        return Config.from_values(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            reporter_ids=_environment_string(self.env, "REPORTER_IDS", default=""),
            review_secret=_environment_string(self.env, "REVIEW_SECRET", default=""),
        )

    def _get_models(self) -> tuple[ModelConfig, ...]:
        return tuple(
            ModelConfig(url, model, key)
            for name, (url, model) in MODEL_PROVIDERS.items()
            if (key := _environment_string(self.env, name))
        )

    async def scheduled(self, controller: object, _env: object, _ctx: object) -> None:
        """Expire retained evidence and complete up to SCHEDULED_TASKS_PER_RUN due model tasks in order."""
        # Cloudflare supplies scheduledTime dynamically on the controller.
        now = int(getattr(controller, "scheduledTime") // 1000)  # noqa: B009
        now = max(now, int(time.time()))
        store = ReportStore(self.env.REPORTS)
        await store.expire(now)
        config = self._get_config()
        tasks = ModelTasks(store, int(config.bot_token.split(":", 1)[0]))
        await tasks.expire(now)
        models = self._get_models()
        for _ in range(SCHEDULED_TASKS_PER_RUN):
            # Each lease starts when its task is claimed, not when the run started.
            now = max(now, int(time.time()))
            task = await tasks.claim(now, inference=bool(models))
            if task is None:
                return
            await tasks.run(task, fetch, config.bot_token, models, now)


def _reject_request(request: Request, webhook_secret: str) -> AppResponse | None:
    """Reject requests that are not authenticated webhook deliveries of an acceptable size."""
    if urlsplit(request.url).path != "/webhook":
        return AppResponse(404, "not found")
    if request.method is not HTTPMethod.POST:
        return AppResponse(405, "method not allowed")
    secret = request.headers.get("x-telegram-bot-api-secret-token")
    if secret is None or not secret.isascii() or not hmac.compare_digest(secret, webhook_secret):
        return AppResponse(401, "unauthorized")
    return _reject_declared_length(request.headers.get("content-length"))


def _reject_declared_length(declared_length: str | None) -> AppResponse | None:
    if declared_length is None:
        return None
    try:
        length = int(declared_length)
    except ValueError:
        return AppResponse(400, "invalid content-length")
    if length < 0:
        return AppResponse(400, "invalid content-length")
    return AppResponse(413, "update too large") if length > MAX_UPDATE_BYTES else None


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


def _review_limit(url: str) -> int:
    """Read the pending-review page size, clamped to the supported range."""
    values = parse_qs(urlsplit(url).query).get("limit")
    if not values:
        return DEFAULT_REVIEW_LIMIT
    try:
        limit = int(values[0])
    except ValueError:
        return DEFAULT_REVIEW_LIMIT
    return max(1, min(limit, MAX_REVIEW_LIMIT))


def _json_response(status: int, payload: dict[str, object]) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        headers={"content-type": "application/json; charset=utf-8"},
    )


def _response(result: AppResponse) -> Response:
    return Response(result.body, status=result.status, headers={"content-type": "text/plain; charset=utf-8"})
