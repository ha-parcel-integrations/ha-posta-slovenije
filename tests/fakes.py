"""Scripted stand-ins for aiohttp sessions and responses."""

from __future__ import annotations

import json
from typing import Any, Self


class FakeResponse:
    """An aiohttp response with a fixed status, body and headers."""

    def __init__(
        self,
        status: int = 200,
        body: bytes | str | object = b"",
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode()
        elif not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = self

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class FakeSession:
    """Answers each request with the next scripted response or exception."""

    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _next(self, method: str, url: str, **kwargs: Any):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url: str, **kwargs: Any):
        return self._next("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self._next("POST", url, **kwargs)
