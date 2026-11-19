"""Pastebin integration.

API docs: https://pastebin.com/doc_api
"""

from app.platforms.base import Platform, PlatformError, PlatformPost, PublishedPost

API_URL = "https://pastebin.com/api/api_post.php"


class PastebinPlatform(Platform):
    name = "Pastebin"
    slug = "pastebin"

    def missing_settings(self) -> list[str]:
        return [] if self.settings.pastebin_api_dev_key else ["PASTEBIN_API_DEV_KEY"]

    async def publish(self, post: PlatformPost) -> PublishedPost:
        response = await self._request(
            "POST",
            API_URL,
            data={
                "api_dev_key": self.settings.pastebin_api_dev_key.get_secret_value(),
                "api_option": "paste",
                "api_paste_code": post.text,
                "api_paste_name": post.title,
                "api_paste_private": "0",
                "api_paste_format": "text",
            },
        )
        self._ensure_success(response, expected=(200,))
        body = response.text.strip()
        if body.startswith("Bad API request"):
            if "invalid api_dev_key" in body:
                raise PlatformError(f"Authentication failed: {body}")
            raise PlatformError(f"Pastebin failed: {body}")
        if not body.startswith("http"):
            raise PlatformError(f"Pastebin returned an unexpected response: {body[:300]}")
        return PublishedPost(url=body)
