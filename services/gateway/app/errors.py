from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


class AppError(Exception):
    code = "app_error"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND


class ValidationError(AppError):
    code = "validation_error"
    # Literal 422: HTTP_422_UNPROCESSABLE_CONTENT only exists in Starlette >= 0.47,
    # and requirements.txt allows fastapi versions pinned to Starlette < 0.42.
    http_status = 422


class AuthError(AppError):
    code = "unauthorized"
    http_status = status.HTTP_401_UNAUTHORIZED

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message, details=details)


class ForbiddenError(AppError):
    code = "forbidden"
    http_status = status.HTTP_403_FORBIDDEN


class RateLimitError(AppError):
    code = "rate_limited"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, message: str, *, retry_after_seconds: int = 1) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderError(AppError):
    code = "provider_error"
    http_status = status.HTTP_502_BAD_GATEWAY


class SearchNoResultsError(AppError):
    code = "no_results"
    http_status = status.HTTP_404_NOT_FOUND


def error_body(exc: AppError) -> dict[str, Any]:
    """The one and only error envelope shape."""
    return {"code": exc.code, "message": exc.message, "details": exc.details}


def error_headers(exc: AppError) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitError):
        headers["Retry-After"] = str(exc.retry_after_seconds)
    if isinstance(exc, AuthError):
        headers["WWW-Authenticate"] = 'Bearer realm="searchos"'
    return headers or None


def to_http_error(exc: AppError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail=error_body(exc),
        headers=error_headers(exc),
    )
