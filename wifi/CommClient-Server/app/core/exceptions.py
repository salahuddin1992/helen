"""
Custom exception classes and global exception handlers.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base application error."""

    def __init__(self, message: str, status_code: int = 400, detail: str | None = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail or message
        super().__init__(self.message)


class NotFoundError(AppError):
    def __init__(self, resource: str, identifier: str):
        super().__init__(
            message=f"{resource} '{identifier}' not found",
            status_code=404,
        )


class ConflictError(AppError):
    def __init__(self, message: str):
        super().__init__(message=message, status_code=409)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Access denied"):
        super().__init__(message=message, status_code=403)


class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__(message=message, status_code=422)


def register_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI app."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("app_error", path=str(request.url), error=exc.message, status=exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message, "detail": exc.detail},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", path=str(request.url), error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error"},
        )
