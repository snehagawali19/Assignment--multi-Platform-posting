"""Shared contract and HTTP helpers for platform integrations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from app.config import Settings

MAX_ERROR_DETAIL_LENGTH = 300


class PlatformError(Exception):
    """A publishing failure whose message is safe to return to API clients."""


@dataclass(frozen=True)
class PlatformPost:
    """The message as it should be published on one platform."""

    text: str
    link: str
    link_start: int
    title: str

    @property
    def link_end(self) -> int:
        return self.link_start + len(self.link)

    @property
    def text_before_link(self) -> str:
        return self.text[: self.link_start]

    @property
    def text_after_link(self) -> str:
        return self.text[self.link_end :]


@dataclass(frozen=True)
class PublishedPost:
    url: str
    check_url: str | None = None
    """Unauthenticated endpoint used to confirm the post is public; defaults to ``url``."""
    reported_public: bool = True
    """Visibility reported by the platform when the post was created."""


class Platform(ABC):
    name: ClassVar[str]
    slug: ClassVar[str]
    max_length: ClassVar[int | None] = None

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def missing_settings(self) -> list[str]:
        """Environment variables that must be set before this platform can publish."""
        return []

    @abstractmethod
    async def publish(self, post: PlatformPost) -> PublishedPost:
        """Publishes the post and returns where it lives. Raises PlatformError on failure."""

    async def check_public_access(self, published: PublishedPost) -> bool:
        """Confirms with an unauthenticated request that anyone can view the post."""
        if not published.reported_public:
            return False
        response = await self.client.get(published.check_url or published.url, follow_redirects=True)
        return response.status_code == 200

    @staticmethod
    def _base_url(value: str, setting: str) -> str:
        """API paths are appended to this URL, so it must be just the server address (scheme and host)."""
        url = value.strip().rstrip("/")
        parts = urlsplit(url)
        is_http = parts.scheme in ("http", "https") and bool(parts.hostname)
        if is_http and not (parts.path or parts.query or parts.fragment):
            return url
        hint = f" (did you mean {parts.scheme}://{parts.netloc}?)" if is_http else ""
        raise PlatformError(f"{setting} must be only the server address starting with https://, got {value!r}{hint}")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise PlatformError(f"Request to {httpx.URL(url).host} timed out") from None
        except httpx.HTTPError as exc:
            reason = str(exc) or (
                "the connection was reset or refused" if isinstance(exc, httpx.ConnectError) else type(exc).__name__
            )
            raise PlatformError(f"Could not reach {httpx.URL(url).host}: {reason}") from None

    def _ensure_success(self, response: httpx.Response, expected: tuple[int, ...] = (200, 201)) -> None:
        if response.status_code in expected:
            return
        status = response.status_code
        if status in (401, 403):
            summary = f"Authentication failed (HTTP {status})"
        elif status == 429:
            summary = "Rate limited by the platform (HTTP 429)"
        else:
            summary = f"Platform returned HTTP {status}"
        detail = _error_detail(response)
        raise PlatformError(f"{summary}: {detail}" if detail else summary)

    def _read_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise PlatformError(f"{self.name} returned a response that is not JSON") from None
        if not isinstance(body, dict):
            raise PlatformError(f"{self.name} returned an unexpected response")
        return body


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        detail = response.reason_phrase if text.startswith("<") else text
    else:
        if isinstance(body, dict):
            parts = [str(body[key]) for key in ("error", "message", "error_description") if body.get(key)]
            detail = " - ".join(parts) or str(body)
        else:
            detail = str(body)
    return detail[:MAX_ERROR_DETAIL_LENGTH]
