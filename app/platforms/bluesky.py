"""Bluesky (AT Protocol) integration.

API docs: https://docs.bsky.app/docs/advanced-guides/posts
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.platforms.base import Platform, PlatformPost, PublishedPost

PUBLIC_APPVIEW_URL = "https://public.api.bsky.app"
SESSION_TTL_SECONDS = 30 * 60
EXPIRED_TOKEN_ERRORS = {"ExpiredToken", "InvalidToken"}
VISIBILITY_CHECK_ATTEMPTS = 3


@dataclass(frozen=True)
class _Session:
    access_jwt: str
    did: str
    handle: str
    created_at: float


class BlueskyPlatform(Platform):
    name = "Bluesky"
    slug = "bluesky"
    max_length = 300

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        super().__init__(client, settings)
        self._session: _Session | None = None
        self._session_lock = asyncio.Lock()

    @property
    def _service_url(self) -> str:
        return self._base_url(self.settings.bluesky_service_url, "BLUESKY_SERVICE_URL")

    def missing_settings(self) -> list[str]:
        missing = []
        if not self.settings.bluesky_handle:
            missing.append("BLUESKY_HANDLE")
        if not self.settings.bluesky_app_password:
            missing.append("BLUESKY_APP_PASSWORD")
        return missing

    async def publish(self, post: PlatformPost) -> PublishedPost:
        record = {
            "$type": "app.bsky.feed.post",
            "text": post.text,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "facets": [_link_facet(post)],
        }
        session = await self._get_session()
        response = await self._create_record(session, record)
        if _is_expired_token(response):
            session = await self._get_session(refresh=True)
            response = await self._create_record(session, record)
        self._ensure_success(response, expected=(200,))

        uri = self._read_json(response)["uri"]
        record_key = uri.rsplit("/", 1)[-1]
        return PublishedPost(url=f"https://bsky.app/profile/{session.handle}/post/{record_key}", check_url=uri)

    async def check_public_access(self, published: PublishedPost) -> bool:
        # The public AppView indexes new posts asynchronously, so a fresh post can take a moment to appear.
        for attempt in range(VISIBILITY_CHECK_ATTEMPTS):
            if attempt:
                await asyncio.sleep(1)
            response = await self.client.get(
                f"{PUBLIC_APPVIEW_URL}/xrpc/app.bsky.feed.getPosts",
                params={"uris": published.check_url},
            )
            if response.status_code == 200 and response.json().get("posts"):
                return True
        return False

    async def _get_session(self, refresh: bool = False) -> _Session:
        async with self._session_lock:
            session = self._session
            if refresh or session is None or time.monotonic() - session.created_at > SESSION_TTL_SECONDS:
                session = await self._create_session()
                self._session = session
            return session

    async def _create_session(self) -> _Session:
        response = await self._request(
            "POST",
            f"{self._service_url}/xrpc/com.atproto.server.createSession",
            json={
                "identifier": self.settings.bluesky_handle.strip().lstrip("@"),
                "password": self.settings.bluesky_app_password.get_secret_value(),
            },
        )
        self._ensure_success(response, expected=(200,))
        body = self._read_json(response)
        return _Session(body["accessJwt"], body["did"], body["handle"], time.monotonic())

    async def _create_record(self, session: _Session, record: dict[str, Any]) -> httpx.Response:
        return await self._request(
            "POST",
            f"{self._service_url}/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session.access_jwt}"},
            json={"repo": session.did, "collection": "app.bsky.feed.post", "record": record},
        )


def _link_facet(post: PlatformPost) -> dict[str, Any]:
    """Bluesky only renders links that are declared as facets, indexed by UTF-8 byte offsets."""
    byte_start = len(post.text_before_link.encode("utf-8"))
    return {
        "index": {"byteStart": byte_start, "byteEnd": byte_start + len(post.link.encode("utf-8"))},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": post.link}],
    }


def _is_expired_token(response: httpx.Response) -> bool:
    if response.status_code not in (400, 401):
        return False
    try:
        return response.json().get("error") in EXPIRED_TOKEN_ERRORS
    except ValueError:
        return False
