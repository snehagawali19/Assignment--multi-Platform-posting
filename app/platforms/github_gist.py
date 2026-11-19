"""GitHub Gist integration.

API docs: https://docs.github.com/en/rest/gists/gists#create-a-gist
"""

from datetime import datetime, timezone

from app.platforms.base import Platform, PlatformPost, PublishedPost

API_URL = "https://api.github.com/gists"


class GitHubGistPlatform(Platform):
    name = "GitHub Gist"
    slug = "github_gist"

    def missing_settings(self) -> list[str]:
        return [] if self.settings.github_token else ["GITHUB_TOKEN"]

    async def publish(self, post: PlatformPost) -> PublishedPost:
        filename = f"post-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.md"
        response = await self._request(
            "POST",
            API_URL,
            headers={
                "Authorization": f"Bearer {self.settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"description": post.title, "public": True, "files": {filename: {"content": f"{post.text}\n"}}},
        )
        self._ensure_success(response, expected=(201,))
        gist = self._read_json(response)
        return PublishedPost(url=gist["html_url"], reported_public=bool(gist.get("public")))
