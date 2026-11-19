#FastAPI application exposing POST /posting.

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.schemas import ErrorResponse, FieldError, PostingRequest, PostingResponse
from app.services.posting_service import PostingService, build_platforms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
USER_AGENT = "automated-posting/1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with httpx.AsyncClient(
        # Only connection attempts that failed to open are retried, so a post is never sent twice.
        transport=httpx.AsyncHTTPTransport(retries=settings.connect_retries),
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        platforms = build_platforms(client, settings)
        app.state.posting_service = PostingService(
            platforms,
            publish_timeout=settings.platform_timeout_seconds,
            public_check_timeout=settings.public_check_timeout_seconds,
            utm_campaign=settings.utm_campaign if settings.utm_tagging_enabled else None,
            tz=settings.tzinfo,
        )
        logger.info("Posting dates and times are reported in %s", settings.timezone)
        for platform in platforms:
            missing = platform.missing_settings()
            logger.info("%s: %s", platform.name, f"not configured (missing {', '.join(missing)})" if missing else "ready")
        yield


app = FastAPI(
    title="Automated Multi-Platform Posting API",
    version="1.0.0",
    description=(
        "Publishes a short message with one CTA link to each platorm"
        
    ),
    lifespan=lifespan,
)


def get_posting_service(request: Request) -> PostingService:
    return request.app.state.posting_service


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Browsers opening http://localhost:4000/ go straight to the API docs."""
    return RedirectResponse(url="/docs")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [_field_error(error) for error in exc.errors()]
    body = ErrorResponse(error="Validation failed", details=details)
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=body.model_dump())


def _field_error(error: dict[str, Any]) -> FieldError:
    if error["type"] == "json_invalid":
        reason = error.get("ctx", {}).get("error", "malformed JSON")
        return FieldError(field="body", message=f"Invalid JSON: {reason}")
    field = ".".join(str(part) for part in error["loc"] if part != "body")
    return FieldError(field=field or "body", message=error["msg"])


@app.post(
    "/posting",
    response_model=PostingResponse,
    responses={422: {"model": ErrorResponse, "description": "The message failed validation"}},
    summary="Publish a message to every supported platform",
    tags=["Posting"],
)
async def create_posting(
    payload: PostingRequest,
    service: Annotated[PostingService, Depends(get_posting_service)],
) -> PostingResponse:
    results = await service.publish(payload.parsed)
    return PostingResponse.from_results(results)
