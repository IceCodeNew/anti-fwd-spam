"""Call Telegram moderation APIs with bounded waits and classified failures."""

from __future__ import annotations

import asyncio
import json
from enum import Enum, auto
from http import HTTPMethod, HTTPStatus
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import builtins
    from collections.abc import Awaitable

TELEGRAM_TIMEOUT_SECONDS = 8
MAX_TELEGRAM_RESPONSE_BYTES = 65_536


class DeleteOutcome(Enum):
    """The application's actionable result of a deleteMessage call."""

    DELETED = auto()
    ALREADY_ABSENT = auto()
    RETRYABLE_FAILURE = auto()
    PERMANENT_FAILURE = auto()


class TelegramResponse(Protocol):
    """The response surface consumed from the Workers fetch API."""

    @property
    def status(self) -> int:
        """Return the HTTP status without requiring a writable attribute."""
        ...

    async def bytes(self) -> builtins.bytes:
        """Return the complete response body."""
        ...


class Fetch(Protocol):
    """The subset of the Workers fetch callable used by this application."""

    def __call__(
        self,
        url: str,
        /,
        *,
        method: HTTPMethod,
        headers: dict[str, str],
        body: str,
    ) -> Awaitable[TelegramResponse]:
        """Issue one asynchronous HTTP request."""
        ...


class TelegramError(Exception):
    """A Bot API failure, without credentials or remote response contents."""

    def __init__(
        self, *, retryable: bool, rejected: bool = False, code: int | None = None, description: str = ""
    ) -> None:
        """Distinguish explicit rejection from an uncertain remote outcome."""
        super().__init__("Telegram request failed")
        self.retryable = retryable
        self.rejected = rejected
        self.code = code
        self.description = description


async def _request(
    fetcher: Fetch,
    token: str,
    method: str,
    parameters: dict[str, object],
) -> tuple[int, bytes]:
    async def send_and_read() -> tuple[int, bytes]:
        response = await fetcher(
            f"https://api.telegram.org/bot{token}/{method}",
            method=HTTPMethod.POST,
            headers={"content-type": "application/json"},
            body=json.dumps(parameters, separators=(",", ":")),
        )
        return response.status, await response.bytes()

    try:
        status, body = await asyncio.wait_for(send_and_read(), timeout=TELEGRAM_TIMEOUT_SECONDS)
    except Exception as error:
        # Workers fetch can raise a JavaScript exception rather than OSError.
        raise TelegramError(retryable=True) from error
    if len(body) > MAX_TELEGRAM_RESPONSE_BYTES:
        raise TelegramError(retryable=True)
    return status, body


async def call_method(fetcher: Fetch, token: str, method: str, parameters: dict[str, object]) -> object:
    """Call a Bot API method, validate its success envelope and classify failures."""
    status, body = await _request(fetcher, token, method, parameters)
    payload = _decode_response(body)
    code = payload.get("error_code") if payload else None
    rejected = (
        status < HTTPStatus.INTERNAL_SERVER_ERROR
        and payload is not None
        and payload.get("ok") is False
        and type(code) is int
        and HTTPStatus.BAD_REQUEST <= code < HTTPStatus.INTERNAL_SERVER_ERROR
    )
    if _temporary(status):
        raise TelegramError(retryable=True, rejected=rejected)
    if payload is None or not isinstance(payload.get("ok"), bool):
        # An unreadable 4xx body still means that Telegram refused the request.
        refused = HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
        raise TelegramError(retryable=not refused, rejected=refused)
    if payload["ok"] is True:
        if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES and "result" in payload:
            return payload["result"]
        raise TelegramError(retryable=True)
    description = payload.get("description")
    raise TelegramError(
        retryable=type(code) is not int or _temporary(code),
        rejected=rejected,
        code=code if type(code) is int else None,
        description=description if isinstance(description, str) else "",
    )


async def delete_message(fetcher: Fetch, token: str, chat_id: int, message_id: int) -> DeleteOutcome:
    """Delete one message and classify the outcome that moderation can act on."""
    try:
        result = await call_method(fetcher, token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except TelegramError as error:
        if error.retryable:
            return DeleteOutcome.RETRYABLE_FAILURE
        if error.code == HTTPStatus.BAD_REQUEST and "message to delete not found" in error.description.casefold():
            return DeleteOutcome.ALREADY_ABSENT
        return DeleteOutcome.PERMANENT_FAILURE
    return DeleteOutcome.DELETED if result is True else DeleteOutcome.RETRYABLE_FAILURE


def _temporary(status: int) -> bool:
    return (
        status in {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
        or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    )


def _decode_response(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload
