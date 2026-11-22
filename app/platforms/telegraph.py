"""Telegraph (telegra.ph) integration.

API docs: https://telegra.ph/api
"""

import asyncio
import json
import logging
from typing import Any

import httpx

from app.config import Settings
from app.platforms.base import Platform, PlatformError, PlatformPost, PublishedPost, Unreachable

logger = logging.getLogger(__name__)

# graph.org is Telegraph's alternate domain. On some networks the TLS handshake to
# api.telegra.ph is reset and telegra.ph itself never connects, while graph.org answers
# and serves the same pages. api.telegra.ph remains the fallback.
API_ROOTS = ("https://api.graph.org", "https://api.telegra.ph")


class TelegraphPlatform(Platform):
    name = "Telegraph"
    slug = "telegraph"

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        super().__init__(client, settings)
        token = settings.telegraph_access_token
        self._access_token = token.get_secret_value() if token else None
        self._account_lock = asyncio.Lock()
        self._api_root: str | None = None

    async def publish(self, post: PlatformPost) -> PublishedPost:
        access_token = await self._get_access_token()
        content = [{"tag": "p", "children": _paragraph_children(post)}]
        page = await self._call(
            "createPage",
            {
                "access_token": access_token,
                "title": post.title,
                "author_name": self.settings.telegraph_author_name,
                "content": json.dumps(content),
                "return_content": "false",
            },
        )
        return PublishedPost(url=page["url"], check_url=f"{self._api_root}/getPage/{page['path']}")

    async def check_public_access(self, published: PublishedPost) -> bool:
        # Reads the page through the public API rather than telegra.ph itself, which some ISPs block.
        response = await self.client.get(published.check_url)
        return response.status_code == 200 and response.json().get("ok") is True

    async def _get_access_token(self) -> str:
        async with self._account_lock:
            if self._access_token is None:
                account = await self._call(
                    "createAccount",
                    {"short_name": "automated-posting", "author_name": self.settings.telegraph_author_name},
                )
                self._access_token = account["access_token"]
                logger.info("Created a Telegraph account for this run; set TELEGRAPH_ACCESS_TOKEN to reuse one account")
        return self._access_token

    def _roots_to_try(self) -> tuple[str, ...]:
        if self._api_root is None:
            return API_ROOTS
        return (self._api_root, *(root for root in API_ROOTS if root != self._api_root))

    async def _call(self, method: str, data: dict[str, str]) -> dict[str, Any]:
        # Only a failure to connect is retried on the other host. The request was never
        # sent, so this cannot publish the same page twice.
        failures: list[Unreachable] = []
        for root in self._roots_to_try():
            try:
                response = await self._request("POST", f"{root}/{method}", data=data)
            except Unreachable as exc:
                failures.append(exc)
                logger.warning("%s", exc)
                if root == self._api_root:
                    self._api_root = None
                continue
            self._api_root = root
            self._ensure_success(response, expected=(200,))
            body = self._read_json(response)
            if not body.get("ok"):
                raise PlatformError(f"Telegraph {method} failed: {body.get('error', 'unknown error')}")
            return body["result"]
        raise PlatformError("; ".join(str(failure) for failure in failures))


def _paragraph_children(post: PlatformPost) -> list[Any]:
    link = {"tag": "a", "attrs": {"href": post.link}, "children": [post.link]}
    return [*_text_nodes(post.text_before_link), link, *_text_nodes(post.text_after_link)]


def _text_nodes(text: str) -> list[Any]:
    nodes: list[Any] = []
    for index, line in enumerate(text.split("\n")):
        if index:
            nodes.append({"tag": "br"})
        if line:
            nodes.append(line)
    return nodes
