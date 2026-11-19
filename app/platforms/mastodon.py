"""Mastodon integration.

API docs: https://docs.joinmastodon.org/methods/statuses/#create
"""

from app.platforms.base import Platform, PlatformPost, PublishedPost


class MastodonPlatform(Platform):
    name = "Mastodon"
    slug = "mastodon"
    max_length = 500

    def missing_settings(self) -> list[str]:
        return [] if self.settings.mastodon_access_token else ["MASTODON_ACCESS_TOKEN"]

    async def publish(self, post: PlatformPost) -> PublishedPost:
        base_url = self._base_url(self.settings.mastodon_base_url, "MASTODON_BASE_URL")
        token = self.settings.mastodon_access_token.get_secret_value()
        response = await self._request(
            "POST",
            f"{base_url}/api/v1/statuses",
            headers={"Authorization": f"Bearer {token}"},
            json={"status": post.text, "visibility": "public"},
        )
        self._ensure_success(response, expected=(200,))
        status = self._read_json(response)
        return PublishedPost(
            url=status.get("url") or status["uri"],
            check_url=f"{base_url}/api/v1/statuses/{status['id']}",
            reported_public=status.get("visibility") == "public",
        )
