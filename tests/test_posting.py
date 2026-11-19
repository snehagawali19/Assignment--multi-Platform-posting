"""Tests for POST /posting, the posting service and each platform integration.

No test touches the network: the API tests use fake platforms and the integration tests use httpx.MockTransport.
"""

import asyncio
import json
import re
from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app, get_posting_service
from app.platforms.base import Platform, PlatformError, PlatformPost, PublishedPost
from app.platforms.bluesky import BlueskyPlatform
from app.platforms.github_gist import GitHubGistPlatform
from app.platforms.mastodon import MastodonPlatform
from app.platforms.pastebin import PastebinPlatform
from app.platforms.telegraph import TelegraphPlatform
from app.schemas import parse_message
from app.services.posting_service import PostingService, add_utm_params, build_post, derive_title

MESSAGE = "Check out our latest business platform and grow your online visibility. https://example.com"


class FakePlatform(Platform):
    def __init__(
        self,
        name: str,
        *,
        error: Exception | None = None,
        missing: tuple[str, ...] = (),
        delay: float = 0.0,
        public: bool = True,
    ) -> None:
        self.name = name
        self.slug = name.lower()
        self.error = error
        self.missing = list(missing)
        self.delay = delay
        self.public = public
        self.received: list[PlatformPost] = []

    def missing_settings(self) -> list[str]:
        return self.missing

    async def publish(self, post: PlatformPost) -> PublishedPost:
        self.received.append(post)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return PublishedPost(url=f"https://{self.slug}.example/posts/1")

    async def check_public_access(self, published: PublishedPost) -> bool:
        return self.public


@pytest.fixture
def make_client():
    def _make(platforms: list[Platform], **service_options) -> TestClient:
        service = PostingService(platforms, **service_options)
        app.dependency_overrides[get_posting_service] = lambda: service
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def post_message(client: TestClient, message: str) -> httpx.Response:
    return client.post("/posting", json={"message": message})


# --- Input validation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_error"),
    [
        ("x" * 190 + " https://example.com", "at most 200 characters"),
        ("   ", "at least 1 character"),
        ("No link in this message", "exactly one CTA link"),
        ("Two links https://a.com and https://b.com", "found 2"),
        ("Visit www.example.com today", "must start with http:// or https://"),
        ("Download ftp://example.com/file", "only http and https"),
        ("Local http://localhost:8000/page", "domain name is not valid"),
        ("Server http://192.168.1.10/page", "IP address"),
        ("Bad https://exa_mple.com", "domain name is not valid"),
        ("Bad port https://example.com:99999", "Port"),
        ("Login https://user:secret@example.com", "credentials"),
        ("https://example.com", "text in addition to the CTA link"),
    ],
)
def test_invalid_messages_are_rejected(make_client, message, expected_error):
    platform = FakePlatform("Alpha")
    response = post_message(make_client([platform]), message)

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"] == "Validation failed"
    assert body["details"][0]["field"] == "message"
    assert expected_error in body["details"][0]["message"]
    assert platform.received == []


def test_missing_message_field_is_rejected(make_client):
    response = make_client([FakePlatform("Alpha")]).post("/posting", json={})

    assert response.status_code == 422
    assert response.json()["details"] == [{"field": "message", "message": "Field required"}]


def test_root_redirects_to_docs(make_client):
    response = make_client([FakePlatform("Alpha")]).get("/", follow_redirects=False)

    assert response.status_code in (307, 302)
    assert response.headers["location"] == "/docs"


def test_clients_cannot_choose_platforms(make_client):
    response = make_client([FakePlatform("Alpha")]).post(
        "/posting", json={"message": MESSAGE, "platforms": ["Mastodon"]}
    )

    assert response.status_code == 422
    assert response.json()["details"] == [{"field": "platforms", "message": "Extra inputs are not permitted"}]


def test_malformed_json_is_rejected(make_client):
    response = make_client([FakePlatform("Alpha")]).post(
        "/posting", content='{"message": ', headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    detail = response.json()["details"][0]
    assert detail["field"] == "body"
    assert detail["message"].startswith("Invalid JSON")


def test_message_of_exactly_200_characters_is_accepted(make_client):
    message = "a" * 180 + " https://example.com"
    assert len(message) == 200

    response = post_message(make_client([FakePlatform("Alpha")]), message)

    assert response.status_code == 200


def test_surrounding_punctuation_is_not_part_of_the_link(make_client):
    platform = FakePlatform("Alpha")
    response = post_message(make_client([platform]), "Read the guide (https://example.com/guide).")

    assert response.status_code == 200
    assert platform.received[0].link == "https://example.com/guide"


# --- Multi-platform posting -------------------------------------------------------------------


def test_publishes_to_every_platform(make_client):
    platforms = [FakePlatform("Alpha"), FakePlatform("Beta")]
    response = post_message(make_client(platforms), MESSAGE)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["summary"] == {"total": 2, "succeeded": 2, "failed": 0}
    first = body["results"][0]
    assert first["platform"] == "Alpha"
    assert first["status"] == "success"
    assert first["posted_url"] == "https://alpha.example/posts/1"
    assert first["public_access"] is True
    assert first["error"] is None
    datetime.strptime(first["date"], "%Y-%m-%d")
    datetime.strptime(first["time"], "%H:%M:%S")


def test_date_and_time_use_the_configured_timezone(make_client):
    ist = ZoneInfo("Asia/Kolkata")
    before = datetime.now(ist).replace(microsecond=0)
    result = post_message(make_client([FakePlatform("Alpha")], tz=ist), MESSAGE).json()["results"][0]
    after = datetime.now(ist)

    posted_at = datetime.strptime(f"{result['date']} {result['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ist)
    assert before <= posted_at <= after


def test_unknown_timezone_is_rejected_at_startup():
    with pytest.raises(ValidationError, match="unknown timezone 'Mars/Olympus'"):
        Settings(_env_file=None, timezone="Mars/Olympus")
    assert Settings(_env_file=None).tzinfo == ZoneInfo("Asia/Kolkata")


def test_one_failing_platform_does_not_stop_the_others(make_client):
    failing = FakePlatform("Alpha", error=PlatformError("Authentication failed (HTTP 401): Bad credentials"))
    response = post_message(make_client([failing, FakePlatform("Beta")]), MESSAGE)

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert body["results"][0] == {
        "platform": "Alpha",
        "status": "failed",
        "posted_url": None,
        "public_access": False,
        "date": None,
        "time": None,
        "error": "Authentication failed (HTTP 401): Bad credentials",
    }
    assert body["results"][1]["status"] == "success"


def test_unexpected_errors_are_contained_and_not_leaked(make_client):
    crashing = FakePlatform("Alpha", error=RuntimeError("token=super-secret"))
    response = post_message(make_client([crashing, FakePlatform("Beta")]), MESSAGE)

    result = response.json()["results"][0]
    assert result["status"] == "failed"
    assert "Unexpected error" in result["error"]
    assert "super-secret" not in result["error"]


def test_slow_platform_times_out_without_blocking_the_others(make_client):
    slow = FakePlatform("Slow", delay=1.0)
    response = post_message(make_client([slow, FakePlatform("Fast")], publish_timeout=0.05), MESSAGE)

    results = response.json()["results"]
    assert results[0]["error"] == "Timed out after 0.05 seconds"
    assert results[1]["status"] == "success"


def test_unconfigured_platform_is_reported_without_being_called(make_client):
    unconfigured = FakePlatform("Alpha", missing=("ALPHA_TOKEN",))
    response = post_message(make_client([unconfigured, FakePlatform("Beta")]), MESSAGE)

    assert response.json()["results"][0]["error"] == "Not configured: set ALPHA_TOKEN in .env"
    assert unconfigured.received == []


def test_success_is_false_when_every_platform_fails(make_client):
    platforms = [FakePlatform("Alpha", error=PlatformError("down")), FakePlatform("Beta", error=PlatformError("down"))]
    response = post_message(make_client(platforms), MESSAGE)

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is False
    assert body["summary"] == {"total": 2, "succeeded": 0, "failed": 2}


def test_post_that_cannot_be_viewed_publicly_is_still_a_success(make_client):
    response = post_message(make_client([FakePlatform("Alpha", public=False)]), MESSAGE)

    result = response.json()["results"][0]
    assert result["status"] == "success"
    assert result["public_access"] is False


def test_each_platform_gets_its_own_tracking_link(make_client):
    alpha, beta = FakePlatform("Alpha"), FakePlatform("Beta")
    post_message(make_client([alpha, beta], utm_campaign="launch"), MESSAGE)

    assert alpha.received[0].link == "https://example.com?utm_source=alpha&utm_medium=social&utm_campaign=launch"
    assert beta.received[0].link == "https://example.com?utm_source=beta&utm_medium=social&utm_campaign=launch"
    assert alpha.received[0].text.endswith(alpha.received[0].link)


# --- Message formatting -----------------------------------------------------------------------


def test_utm_params_keep_existing_query_and_fragment():
    tagged = add_utm_params("https://example.com/p?ref=a%20b#top", source="mastodon", campaign="launch")
    assert tagged == "https://example.com/p?ref=a%20b&utm_source=mastodon&utm_medium=social&utm_campaign=launch#top"


def test_existing_utm_params_are_left_untouched():
    url = "https://example.com/?utm_source=newsletter"
    assert add_utm_params(url, source="mastodon", campaign="launch") == url


def test_tracking_link_is_dropped_when_it_exceeds_the_platform_limit():
    message = parse_message("a" * 180 + " https://example.com")
    post = build_post(message, source="bluesky", max_length=200, utm_campaign="launch")
    assert post.link == "https://example.com"
    assert post.text == message.text


@pytest.mark.parametrize(
    ("message", "title"),
    [
        (MESSAGE, "Check out our latest business platform and grow your online visibility"),
        ("New release! Details at https://example.com. Try it today.", "New release"),
        ("Go: https://example.com", "Announcement"),
    ],
)
def test_title_is_the_first_sentence_without_the_link(message, title):
    assert derive_title(parse_message(message)) == title


# --- Platform integrations --------------------------------------------------------------------

SAMPLE_POST = build_post(parse_message(MESSAGE), source="test")


def run_platform(platform_class, handler, post=SAMPLE_POST, **settings):
    """Publishes through a platform backed by a mock transport and returns (published post, public access)."""

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            platform = platform_class(client, Settings(_env_file=None, **settings))
            published = await platform.publish(post)
            return published, await platform.check_public_access(published)

    return asyncio.run(scenario())


def test_telegraph_creates_an_account_then_a_page():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/createAccount":
            return httpx.Response(200, json={"ok": True, "result": {"access_token": "tg-token"}})
        if request.url.path == "/createPage":
            page = {"path": "Check-out-11-16", "url": "https://telegra.ph/Check-out-11-16"}
            return httpx.Response(200, json={"ok": True, "result": page})
        if request.url.path == "/getPage/Check-out-11-16" and "access_token" not in request.url.params:
            return httpx.Response(200, json={"ok": True, "result": {"path": "Check-out-11-16"}})
        return httpx.Response(200, json={"ok": False, "error": "PAGE_NOT_FOUND"})

    published, public = run_platform(TelegraphPlatform, handler)

    assert published.url == "https://telegra.ph/Check-out-11-16"
    assert public is True
    form = parse_qs(requests[1].content.decode())
    assert form["access_token"] == ["tg-token"]
    assert form["title"] == ["Check out our latest business platform and grow your online visibility"]
    paragraph = json.loads(form["content"][0])[0]
    assert paragraph["children"][-1] == {
        "tag": "a",
        "attrs": {"href": "https://example.com"},
        "children": ["https://example.com"],
    }


def test_telegraph_api_errors_become_platform_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "ACCESS_TOKEN_INVALID"})

    with pytest.raises(PlatformError, match="createPage failed: ACCESS_TOKEN_INVALID"):
        run_platform(TelegraphPlatform, handler, telegraph_access_token="bad-token")


def test_mastodon_publishes_a_public_status():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "42", "url": "https://mastodon.social/@me/42", "uri": "x", "visibility": "public"},
            )
        return httpx.Response(200, json={"id": "42"})

    published, public = run_platform(MastodonPlatform, handler, mastodon_access_token="masto-token")

    assert published.url == "https://mastodon.social/@me/42"
    assert public is True
    assert requests[0].headers["Authorization"] == "Bearer masto-token"
    assert json.loads(requests[0].content) == {"status": MESSAGE, "visibility": "public"}
    assert str(requests[1].url) == "https://mastodon.social/api/v1/statuses/42"
    assert "Authorization" not in requests[1].headers


def test_mastodon_authentication_failure_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "The access token is invalid"})

    with pytest.raises(PlatformError, match=r"Authentication failed \(HTTP 401\): The access token is invalid"):
        run_platform(MastodonPlatform, handler, mastodon_access_token="bad-token")


@pytest.mark.parametrize(
    ("base_url", "expected_error"),
    [
        ("@me@mastodon.social", "got '@me@mastodon.social'$"),
        ("https://mastodon.social/@me", r"\(did you mean https://mastodon.social\?\)"),
    ],
)
def test_invalid_base_url_is_reported_without_sending_a_request(base_url, expected_error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    with pytest.raises(PlatformError, match="MASTODON_BASE_URL must be only the server address") as error:
        run_platform(MastodonPlatform, handler, mastodon_access_token="token", mastodon_base_url=base_url)
    assert re.search(expected_error, str(error.value))


@pytest.mark.parametrize(
    ("message", "expected_error"),
    [
        ("Connection refused", "Could not reach mastodon.social: Connection refused"),
        ("", "Could not reach mastodon.social: the connection was reset or refused"),
    ],
)
def test_network_errors_are_reported_with_the_host(message, expected_error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(message, request=request)

    with pytest.raises(PlatformError, match=f"^{re.escape(expected_error)}$"):
        run_platform(MastodonPlatform, handler, mastodon_access_token="masto-token")


def test_bluesky_declares_the_link_facet_with_utf8_byte_offsets():
    records: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:abc", "handle": "me.bsky.social"})
        if request.url.path.endswith("createRecord"):
            records.append(json.loads(request.content))
            return httpx.Response(200, json={"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "c"})
        return httpx.Response(200, json={"posts": [{"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz"}]})

    post = build_post(parse_message("Launch 🚀 today https://example.com"), source="bluesky")
    published, public = run_platform(
        BlueskyPlatform, handler, post, bluesky_handle="@me.bsky.social", bluesky_app_password="app-pass"
    )

    assert published.url == "https://bsky.app/profile/me.bsky.social/post/3kxyz"
    assert public is True
    assert records[0]["repo"] == "did:plc:abc"
    facet = records[0]["record"]["facets"][0]
    assert facet["index"] == {"byteStart": 18, "byteEnd": 37}
    assert facet["features"][0]["uri"] == "https://example.com"


def test_bluesky_renews_an_expired_session_once():
    sessions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sessions
        if request.url.path.endswith("createSession"):
            sessions += 1
            return httpx.Response(200, json={"accessJwt": f"jwt-{sessions}", "did": "did:plc:abc", "handle": "me.bsky.social"})
        if request.url.path.endswith("createRecord"):
            if request.headers["Authorization"] == "Bearer jwt-1":
                return httpx.Response(400, json={"error": "ExpiredToken", "message": "Token has expired"})
            return httpx.Response(200, json={"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "c"})
        return httpx.Response(200, json={"posts": [{}]})

    published, _ = run_platform(BlueskyPlatform, handler, bluesky_handle="me.bsky.social", bluesky_app_password="pw")

    assert sessions == 2
    assert published.url.endswith("/post/3kxyz")


def test_github_gist_is_created_public():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"html_url": "https://gist.github.com/me/abc123", "public": True})
        return httpx.Response(200, text="<html>gist</html>")

    published, public = run_platform(GitHubGistPlatform, handler, github_token="gh-token")

    assert published.url == "https://gist.github.com/me/abc123"
    assert public is True
    payload = json.loads(requests[0].content)
    assert payload["public"] is True
    (file,) = payload["files"].values()
    assert file["content"] == MESSAGE + "\n"
    assert requests[0].headers["Authorization"] == "Bearer gh-token"


def test_github_bad_credentials_are_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials", "documentation_url": "https://docs.github.com"})

    with pytest.raises(PlatformError, match=r"Authentication failed \(HTTP 401\): Bad credentials"):
        run_platform(GitHubGistPlatform, handler, github_token="bad-token")


def test_pastebin_creates_a_public_paste():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, text="https://pastebin.com/AbCdEf12")
        return httpx.Response(200, text="<html>paste</html>")

    published, public = run_platform(PastebinPlatform, handler, pastebin_api_dev_key="pb-key")

    assert published.url == "https://pastebin.com/AbCdEf12"
    assert public is True
    assert str(requests[0].url) == "https://pastebin.com/api/api_post.php"
    form = parse_qs(requests[0].content.decode())
    assert form["api_dev_key"] == ["pb-key"]
    assert form["api_option"] == ["paste"]
    assert form["api_paste_private"] == ["0"]
    assert form["api_paste_name"] == ["Check out our latest business platform and grow your online visibility"]
    assert form["api_paste_code"] == [MESSAGE]
    assert str(requests[1].url) == "https://pastebin.com/AbCdEf12"


def test_pastebin_missing_page_is_not_public():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text="https://pastebin.com/AbCdEf12")
        return httpx.Response(404, text="Not Found")

    _, public = run_platform(PastebinPlatform, handler, pastebin_api_dev_key="pb-key")

    assert public is False


def test_pastebin_invalid_api_key_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Bad API request, invalid api_dev_key")

    with pytest.raises(PlatformError, match="Authentication failed: Bad API request, invalid api_dev_key"):
        run_platform(PastebinPlatform, handler, pastebin_api_dev_key="bad-key")


def test_platforms_report_their_missing_settings():
    settings = Settings(_env_file=None)
    client = httpx.AsyncClient()
    assert MastodonPlatform(client, settings).missing_settings() == ["MASTODON_ACCESS_TOKEN"]
    assert BlueskyPlatform(client, settings).missing_settings() == ["BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"]
    assert GitHubGistPlatform(client, settings).missing_settings() == ["GITHUB_TOKEN"]
    assert PastebinPlatform(client, settings).missing_settings() == ["PASTEBIN_API_DEV_KEY"]
    assert TelegraphPlatform(client, settings).missing_settings() == []
