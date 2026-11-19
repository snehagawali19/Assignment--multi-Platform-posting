"""Request/response models and validation of the submitted message."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from pydantic_core import PydanticCustomError

MAX_MESSAGE_LENGTH = 200
EXAMPLE_MESSAGE = "Hey everyone! I’m Sneha, a Robotics and AI grad. I’m new here and excited to explore, share what I’m building, and connect with you all. Here’s my portfolio: https://snehagawali.tech"
_LINK_PATTERN = re.compile(r"(?:\b[a-z][a-z0-9+\-]*://|\bwww\.)\S+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?'\"*>"
_CLOSING_BRACKETS = {")": "(", "]": "[", "}": "{"}
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TOP_LEVEL_DOMAIN = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


@dataclass(frozen=True)
class ParsedMessage:
    """A validated message and the position of its single CTA link."""

    text: str
    cta_url: str
    cta_start: int

    @property
    def cta_end(self) -> int:
        return self.cta_start + len(self.cta_url)


def parse_message(message: str) -> ParsedMessage:
    """Extracts the CTA link from the message, raising PydanticCustomError if the message is invalid."""
    text = message.strip()
    links = [(match.start(), _strip_trailing_punctuation(match.group())) for match in _LINK_PATTERN.finditer(text)]

    if not links:
        raise PydanticCustomError(
            "missing_cta_link",
            "Message must include exactly one CTA link (for example https://example.com)",
        )
    if len(links) > 1:
        raise PydanticCustomError(
            "multiple_cta_links",
            "Message must include exactly one CTA link, found {count}: {links}",
            {"count": len(links), "links": ", ".join(url for _, url in links)},
        )

    start, url = links[0]
    validate_cta_url(url)

    if not re.search(r"\w", text[:start] + text[start + len(url) :]):
        raise PydanticCustomError(
            "missing_message_text",
            "Message must contain text in addition to the CTA link",
        )
    return ParsedMessage(text=text, cta_url=url, cta_start=start)


def validate_cta_url(url: str) -> None:
    """Accepts only absolute http(s) links that point at a public domain name."""

    def invalid(reason: str) -> PydanticCustomError:
        return PydanticCustomError("invalid_cta_link", "Invalid CTA link {url}: {reason}", {"url": url, "reason": reason})

    if url.lower().startswith("www."):
        raise invalid("the link must start with http:// or https://")
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError as exc:
        raise invalid(str(exc)) from None

    if parts.scheme.lower() not in ("http", "https"):
        raise invalid("only http and https links are supported")
    if parts.username is not None or parts.password is not None:
        raise invalid("credentials are not allowed in the link")

    host = parts.hostname
    if not host:
        raise invalid("the link has no domain name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise invalid("use a public domain name instead of an IP address")

    try:
        ascii_host = host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise invalid("the domain name is not valid") from None
    labels = ascii_host.split(".")
    if (
        len(ascii_host) > 253
        or len(labels) < 2
        or not all(_HOST_LABEL.match(label) for label in labels)
        or not _TOP_LEVEL_DOMAIN.match(labels[-1])
    ):
        raise invalid("the domain name is not valid")


def _strip_trailing_punctuation(url: str) -> str:
    """Drops sentence punctuation after a link, keeping balanced brackets such as Wikipedia's (disambiguation)."""
    while url:
        last = url[-1]
        if last in _CLOSING_BRACKETS:
            if url.count(_CLOSING_BRACKETS[last]) >= url.count(last):
                break
        elif last not in _TRAILING_PUNCTUATION:
            break
        url = url[:-1]
    return url


class PostingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"message": EXAMPLE_MESSAGE}]})

    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH),
        Field(description="Text to publish (max 200 characters) containing exactly one http(s) CTA link."),
    ]

    @field_validator("message")
    @classmethod
    def _require_single_valid_cta_link(cls, value: str) -> str:
        parse_message(value)
        return value

    @property
    def parsed(self) -> ParsedMessage:
        return parse_message(self.message)


class PlatformResult(BaseModel):
    platform: str = Field(examples=["Mastodon"])
    status: Literal["success", "failed"]
    posted_url: str | None = Field(default=None, description="Public URL of the published content.")
    public_access: bool = Field(
        default=False,
        description="True when the post was fetched successfully without credentials after publishing.",
    )
    date: str | None = Field(
        default=None,
        description="Posting date (YYYY-MM-DD) in the configured TIMEZONE, Asia/Kolkata by default.",
        examples=["2026-11-16"],
    )
    time: str | None = Field(
        default=None,
        description="Posting time (HH:MM:SS) in the configured TIMEZONE, Asia/Kolkata by default.",
        examples=["11:45:42"],
    )
    error: str | None = Field(default=None, description="Why publishing failed, when status is failed.")


class PostingSummary(BaseModel):
    total: int
    succeeded: int
    failed: int


class PostingResponse(BaseModel):
    success: bool = Field(description="True when at least one platform published the message.")
    summary: PostingSummary
    results: list[PlatformResult]

    @classmethod
    def from_results(cls, results: list[PlatformResult]) -> "PostingResponse":
        succeeded = sum(result.status == "success" for result in results)
        return cls(
            success=succeeded > 0,
            summary=PostingSummary(total=len(results), succeeded=succeeded, failed=len(results) - succeeded),
            results=results,
        )


class FieldError(BaseModel):
    field: str
    message: str


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: str
    details: list[FieldError] = []
