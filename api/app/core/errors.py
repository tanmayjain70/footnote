"""A single error shape for the whole API.

Every failure the client can encounter serialises as
``{"error": {"code": ..., "message": ..., "detail": ...}}``. The frontend reads
``code`` and never parses prose.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

#: Starlette renamed this constant (ENTITY -> CONTENT) and deprecated the old
#: spelling. Pinning the number here keeps the code working on both without a
#: warning, and 422 is not going to change meaning.
HTTP_422 = 422


class AppError(Exception):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(self, message: str, detail: Any = None, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code:
            self.code = code


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class Forbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class Unauthorized(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class PayloadTooLarge(AppError):
    """The upload exceeds ``max_upload_mb``. Refused before it is parsed."""

    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    code = "file_too_large"


class UnprocessableUpload(AppError):
    """The file was readable but not usable -- not a PDF, or no extractable text."""

    status_code = HTTP_422
    code = "unprocessable_upload"


class BudgetExhausted(AppError):
    """Today's model spend has reached the daily limit.

    A 429 rather than a 403: the caller is allowed to ask, just not right now.
    Raised before any tokens are spent, so the limit holds server-side however
    many browser tabs are open.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "budget_exhausted"


class Terminal(RuntimeError):
    """A background job failed in a way a retry cannot mend.

    Not an ``AppError``: nothing about it reaches an HTTP client. It is how a
    job handler tells the runner "stop, and do not spend another attempt" --
    the daily budget is gone, or the payload names something that no longer
    exists. Everything else is assumed worth trying again.
    """


class ServiceUnavailable(AppError):
    """A dependency the request needs is not running or not installed."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def _body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=_body(exc.code, exc.message, exc.detail)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_422,
            content=_body("validation_error", "The request body is not valid.", exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code, content=_body(code, str(exc.detail))
        )
