#Publishes a validated message to every supported platform concurrently and collects one result per platform.

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import datetime, timezone, tzinfo
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.config import Settings
from app.platforms.base import Platform, PlatformError, PlatformPost, PublishedPost
from app.platforms.bluesky import BlueskyPlatform
from app.platforms.github_gist import GitHubGistPlatform
from app.platforms.mastodon import MastodonPlatform
from app.platforms.pastebin import PastebinPlatform
from app.platforms.telegraph import TelegraphPlatform
from app.schemas import ParsedMessage, PlatformResult

logger = logging.getLogger(__name__)

PLATFORM_CLASSES: tuple[type[Platform], ...] = (
    TelegraphPlatform,
    MastodonPlatform,
    BlueskyPlatform,
    GitHubGistPlatform,
    PastebinPlatform,
)
MAX_TITLE_LENGTH = 80
DEFAULT_TITLE = "Announcement"


def build_platforms(client: httpx.AsyncClient, settings: Settings) -> list[Platform]:
    return [platform_class(client, settings) for platform_class in PLATFORM_CLASSES]


class PostingService:
    def __init__(
        self,
        platforms: Sequence[Platform],
        *,
        publish_timeout: float = 20.0,
        public_check_timeout: float = 10.0,
        utm_campaign: str | None = None,
        tz: tzinfo = timezone.utc,
    ) -> None:
        self.platforms = list(platforms)
        self.publish_timeout = publish_timeout
        self.public_check_timeout = public_check_timeout
        self.utm_campaign = utm_campaign
        self.tz = tz

    async def publish(self, message: ParsedMessage) -> list[PlatformResult]:
        #Never raises: every platform produces a result, so one failure cannot affect the others.
        return list(await asyncio.gather(*(self._publish_to(platform, message) for platform in self.platforms)))

    async def _publish_to(self, platform: Platform, message: ParsedMessage) -> PlatformResult:
        missing = platform.missing_settings()
        if missing:
            return self._failed(platform, f"Not configured: set {', '.join(missing)} in .env")

        post = build_post(message, source=platform.slug, max_length=platform.max_length, utm_campaign=self.utm_campaign)
        try:
            published = await asyncio.wait_for(platform.publish(post), self.publish_timeout)
        except PlatformError as exc:
            return self._failed(platform, str(exc))
        except asyncio.TimeoutError:
            return self._failed(platform, f"Timed out after {self.publish_timeout:g} seconds")
        except Exception:
            logger.exception("%s: unexpected error while publishing", platform.name)
            return self._failed(platform, "Unexpected error while publishing; see server logs for details")

        posted_at = datetime.now(self.tz)
        public_access = await self._check_public_access(platform, published)
        logger.info("%s: published %s (public access: %s)", platform.name, published.url, public_access)
        return PlatformResult(
            platform=platform.name,
            status="success",
            posted_url=published.url,
            public_access=public_access,
            date=posted_at.strftime("%Y-%m-%d"),
            time=posted_at.strftime("%H:%M:%S"),
        )

    async def _check_public_access(self, platform: Platform, published: PublishedPost) -> bool:
        #A failed visibility check never turns a published post into a failure.
        try:
            return await asyncio.wait_for(platform.check_public_access(published), self.public_check_timeout)
        except Exception as exc:
            logger.warning("%s: could not confirm public access for %s (%s)", platform.name, published.url, type(exc).__name__)
            return False

    @staticmethod
    def _failed(platform: Platform, error: str) -> PlatformResult:
        logger.warning("%s: failed - %s", platform.name, error)
        return PlatformResult(platform=platform.name, status="failed", error=error)


def build_post(
    message: ParsedMessage,
    *,
    source: str,
    max_length: int | None = None,
    utm_campaign: str | None = None,
) -> PlatformPost:
    
    link = add_utm_params(message.cta_url, source=source, campaign=utm_campaign) if utm_campaign else message.cta_url
    text = message.text[: message.cta_start] + link + message.text[message.cta_end :]
    if max_length is not None and len(text) > max_length:
        link, text = message.cta_url, message.text
    return PlatformPost(text=text, link=link, link_start=message.cta_start, title=derive_title(message))


def add_utm_params(url: str, *, source: str, campaign: str) -> str:
    """Appends UTM parameters so each platform's traffic is distinguishable, unless the link already has some."""
    parts = urlsplit(url)
    if any(key.lower().startswith("utm_") for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return url
    utm_query = urlencode({"utm_source": source, "utm_medium": "social", "utm_campaign": campaign})
    query = f"{parts.query}&{utm_query}" if parts.query else utm_query
    return urlunsplit(parts._replace(query=query))


def derive_title(message: ParsedMessage) -> str:
    """Uses the first sentence of the message (without the link) as a title for platforms that require one."""
    text = " ".join((message.text[: message.cta_start] + " " + message.text[message.cta_end :]).split())
    title = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip(" .!?:;,-")
    if len(title) > MAX_TITLE_LENGTH:
        title = title[: MAX_TITLE_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return title if len(title) >= 3 else DEFAULT_TITLE
