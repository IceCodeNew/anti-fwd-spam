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

    def __init__(self, *, retryable: bool, rejected: bool = False) -> None:
        """Distinguish explicit rejection from an uncertain remote outcome."""
        super().__init__("Telegram request failed")
        self.retryable = retryable
        self.rejected = rejected


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
    """Call a Bot API method and validate its success envelope."""
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
    if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        raise TelegramError(retryable=True, rejected=rejected)
    if payload is None or not isinstance(payload.get("ok"), bool):
        raise TelegramError(retryable=True)
    if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES and payload["ok"] is True and "result" in payload:
        return payload["result"]
    retryable = (
        type(code) is not int or code == HTTPStatus.TOO_MANY_REQUESTS or code >= HTTPStatus.INTERNAL_SERVER_ERROR
    )
    raise TelegramError(retryable=retryable, rejected=rejected)


async def delete_message(
    fetcher: Fetch,
    token: str,
    chat_id: int,
    message_id: int,
) -> DeleteOutcome:
    """Await one bounded deleteMessage call and classify its outcome."""
    try:
        status, response_body = await _request(
            fetcher,
            token,
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )
    except TelegramError:
        return DeleteOutcome.RETRYABLE_FAILURE

    return classify_telegram_response(status, response_body)


def classify_telegram_response(status: int, body: bytes) -> DeleteOutcome:
    """Classify Telegram's HTTP status and JSON result without false success."""
    if (
        status in {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
        or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    ):
        return DeleteOutcome.RETRYABLE_FAILURE

    typed_payload = _decode_response(body)
    if typed_payload is None or not isinstance(typed_payload.get("ok"), bool):
        return _malformed_response_outcome(status)
    if typed_payload["ok"] is True:
        deleted = HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES and typed_payload.get("result") is True
        return DeleteOutcome.DELETED if deleted else DeleteOutcome.RETRYABLE_FAILURE

    error_code = typed_payload.get("error_code")
    if type(error_code) is not int:
        return DeleteOutcome.RETRYABLE_FAILURE
    description = typed_payload.get("description")
    if (
        error_code == HTTPStatus.BAD_REQUEST
        and isinstance(description, str)
        and "message to delete not found" in description.casefold()
    ):
        return DeleteOutcome.ALREADY_ABSENT
    retryable = (
        error_code in {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
        or error_code >= HTTPStatus.INTERNAL_SERVER_ERROR
    )
    return DeleteOutcome.RETRYABLE_FAILURE if retryable else DeleteOutcome.PERMANENT_FAILURE


def _decode_response(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _malformed_response_outcome(status: int) -> DeleteOutcome:
    return (
        DeleteOutcome.PERMANENT_FAILURE
        if HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
        else DeleteOutcome.RETRYABLE_FAILURE
    )
