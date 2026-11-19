# Automated Multi-Platform Posting System

A FastAPI service with a single endpoint, `POST /posting`, that validates a short message containing one call-to-action (CTA) link and publishes it to five third-party platforms at the same time. Every platform is handled independently: if one fails, the others still publish, and the response reports the outcome, public URL, public visibility and timestamp for each platform.



## Platforms

| Platform | Published as | API endpoint | Authentication |

 - [Telegraph](https://telegra.ph/api) | A public page | `POST https://api.telegra.ph/createPage` | Access token, created automatically if not set 
 - [Mastodon](https://docs.joinmastodon.org/methods/statuses/#create) | A public status | `POST {instance}/api/v1/statuses` | Bearer access token 
 - [Bluesky](https://docs.bsky.app/docs/advanced-guides/posts) | A post with a clickable link | `POST /xrpc/com.atproto.repo.createRecord` | Handle and app password
 - [GitHub Gist](https://docs.github.com/en/rest/gists/gists#create-a-gist) | A public gist | `POST https://api.github.com/gists` | Personal access token 
 - [Pastebin](https://pastebin.com/doc_api) | A public paste | `POST https://pastebin.com/api/api_post.php` | Developer API key 




These platforms were chosen because each offers a free, documented REST API that a single token can use, with no OAuth redirect flow or app review, and each returns a URL for the published content. Platforms such as X, LinkedIn, Facebook, Instagram and Reddit were considered but left out. They need paid API access, an app review, an OAuth redirect flow or (for Instagram) an image.


## How it works

1. `POST /posting` receives `{"message": "..."}`. Pydantic validates the message length and extracts and validates the single CTA link (see [Validation rules](#validation-rules)).

2. `PostingService` builds a separate version of the post for each platform:
   - The CTA link is tagged with `utm_source=<platform>`, so each platform's traffic can be tracked separately.
   - Platforms that need a title (Telegraph, Gist, Pastebin) get one taken from the first sentence of the message.
   - The post uses the platform's own format, for example a link facet on Bluesky, HTML on Telegraph or plain text on Pastebin.

3. All platforms are published concurrently with `asyncio.gather`. Each platform has its own timeout and error handling, so a failure, timeout or crash on one platform becomes a `failed` result for that platform only.

4. After a successful post, the service confirms public visibility with a request that sends **no credentials**:

   | Platform | Public access check |
   |---|---|
   | Telegraph | `getPage` API without a token |
   | Mastodon | Status `visibility` is `public` and `GET /api/v1/statuses/{id}` succeeds anonymously |
   | Bluesky | The post is returned by the public AppView (`app.bsky.feed.getPosts`) |
   | GitHub Gist | The gist was created as public and its page opens anonymously |
   | Pastebin | The paste URL returned by the API opens anonymously |

5. The response lists one result per platform, in a fixed order.

## Project structure


```text
automated-posting/
├── app/
│   ├── main.py                  # FastAPI app, POST /posting, validation error handler
│   ├── schemas.py               # Request/response models and message + CTA link validation
│   ├── config.py                # Settings loaded from environment variables / .env
│   ├── platforms/
│   │   ├── base.py              # Platform interface
│   │   ├── telegraph.py
│   │   ├── mastodon.py
│   │   ├── bluesky.py
│   │   ├── github_gist.py
│   │   └── pastebin.py
│   └── services/
│       └── posting_service.py   # Platform list, per-platform formatting, concurrent publishing
├── tests/
│   └── test_posting.py
├── .env.example
├── requirements.txt
└── README.md
```



## Setup
1. Python 3.12
2. python -m venv .venv && activate
3. pip install -r requirements.txt
4. copy .env.example .env  → fill tokens
5. uvicorn app.main:app --host 0.0.0.0 --port 4000
6. Open http://localhost:4000/docs



## Tests
python -m pytest

## Note
Do not commit .env


### Getting credentials
Each platform has Steps and variables menitoned in the .env.example




```bash
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 4000
```

Before starting, check: `.venv\Scripts\python --version` must show **3.12.x**, not 3.15.

Then open these links in your browser:

| Link | What it is |
 http://localhost:4000/ | Redirects to the docs 
 http://localhost:4000/docs | Interactive API (Try it out → POST /posting) 
http://localhost:4000/posting | API endpoint (POST only; use /docs or curl) 

`--host 0.0.0.0` is what the assignment specifies. You still open the app as **localhost** on port **4000**.

## API

### `POST /posting`

**Request**

```json
{
  "message": "Hey everyone! I’m Sneha,a Robotics and AI grad.I’m new here and excited to explore, share what I’m building, and connect with you all, portfolio: https://snehagawali.tech"
}
```



### Validation rules

- The message is trimmed and must be between 1 and 200 characters, including the link.
- It must contain **exactly one** link. Links are detected by a scheme (`https://`, `ftp://`, ...) or a `www.` prefix.
- The link must use `http` or `https` and a public domain name with a valid top-level domain. IP addresses, `localhost`, embedded credentials and invalid ports are rejected.
- Punctuation that ends a sentence right after the link (for example `https://example.com).`) is not treated as part of the link.
- The message must contain text besides the link.
- Only the `message` field is accepted. The application chooses the platforms, so extra fields such as `platforms` are rejected.

**Response:`422 Unprocessable Content`**

```json
{
  "success": false,
  "error": "Validation failed",
  "details": [
    {
      "field": "message",
      "message": ""
    }
  ]
}
'''


## Error handling

| Situation | Result for that platform |
|---|---|
| Credentials missing | `Not configured: set MASTODON_ACCESS_TOKEN in .env` (no request is sent) |
| Rejected credentials (401/403) | `Authentication failed (HTTP 401): <platform message>` |
| Rate limit (429) | `Rate limited by the platform (HTTP 429): <platform message>` |
| Other HTTP error | `Platform returned HTTP 404: Repository not found` |
| Network failure | `Could not reach mastodon.social: <reason>`. Connections that fail to open are first retried up to `CONNECT_RETRIES` times (default 5) with increasing delays. Because the request was never sent, nothing can be posted twice. |
| Slow platform | `Timed out after 20 seconds` (`PLATFORM_TIMEOUT_SECONDS`) |
| Unexpected exception | `Unexpected error while publishing; see server logs for details`. The full traceback is logged, not returned. |

Successful posts are never rolled back when another platform fails.


```bash
python -m pytest
```

The 50 tests never touch the network. API tests replace the platforms with fakes, and integration tests use `httpx.MockTransport`.

Also includes a result file stored as Response_1.json


