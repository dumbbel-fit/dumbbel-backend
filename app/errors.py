"""Error envelope: { "error": { code, message, details, request_id } }"""
import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("dumbbel")

# Brand-voice default messages, safe to show users.
DEFAULT_MESSAGES = {
    "BAD_REQUEST": "Something about that request isn’t right.",
    "PHONE_INVALID": "That phone number doesn’t look right.",
    "OTP_INVALID": "That code isn’t right.",
    "UNAUTHENTICATED": "Please log in to continue.",
    "TOKEN_EXPIRED": "Your session timed out. Refreshing…",
    "SESSION_EXPIRED": "You’ve been logged out. Please log in again.",
    "FORBIDDEN": "You don’t have access to this.",
    "NOT_GYM_STAFF": "No gym is linked to this number.",
    "PAYMENT_NOT_ALLOWED": "Only the owner or staff with payment access can record money.",
    "OWNER_ONLY": "Only the gym owner can do this.",
    "NOT_FOUND": "We couldn’t find that.",
    "MEMBER_EXISTS": "This number is already a member here.",
    "OVERLAPPING_MEMBERSHIP": "That overlaps with an existing membership.",
    "DUPLICATE_REFERENCE": "This payment reference has already been recorded.",
    "POSSIBLE_DUPLICATE": "Looks like the same payment was recorded a few minutes ago.",
    "PLAN_NAME_TAKEN": "A plan with this name already exists.",
    "ALREADY_STAFF": "This person is already on your staff.",
    "ALREADY_OWNER": "This number already owns a gym. Contact support to add another.",
    "NOT_ACTIVE": "This membership isn’t active.",
    "PHONE_LOCKED_AFTER_LOGIN": "This member already uses the app, so their number can only be changed by support.",
    "OTP_EXPIRED": "That code has expired. Request a new one.",
    "FILE_TOO_LARGE": "That file is too big. Max 10 MB or 5,000 rows.",
    "FILE_TYPE": "Upload a CSV, XLS or XLSX file.",
    "VALIDATION": "Please check the highlighted fields.",
    "PLAN_INACTIVE": "This plan isn’t on sale any more.",
    "FREEZE_LIMIT": "That’s more freeze days than this gym allows.",
    "OVERPAYMENT": "That’s more than what’s due.",
    "NO_HEADER_ROW": "We couldn’t find a header row in this file.",
    "PHONE_LOCKED": "Too many tries. Please wait a bit and try again.",
    "GYM_SUSPENDED": "This gym is suspended.",
    "GYM_PENDING": "Your gym is still being verified. This unlocks once it’s approved.",
    "APP_UPDATE_REQUIRED": "Please update the app to continue.",
    "OTP_RATE_LIMITED": "Please wait before requesting another code.",
    "RATE_LIMITED": "Slow down a little and try again.",
    "INVITE_TOO_SOON": "An invite was sent recently. Try again later.",
    "INTERNAL": "Something went wrong on our side. Please try again.",
    "UPSTREAM_SMS": "We couldn’t send the SMS. Please try again.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, "Something went wrong.")
        self.details = details or {}
        super().__init__(f"{status} {code}")


def not_found(what: str = "That") -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} wasn’t found.")


def validation(fields: dict) -> ApiError:
    return ApiError(422, "VALIDATION", None, {"fields": fields})


def _body(request: Request, code: str, message: str, details: dict | None):
    return {"error": {"code": code, "message": message, "details": details or {},
                      "request_id": getattr(request.state, "request_id", None)}}


async def api_error_handler(request: Request, exc: ApiError):
    headers = {}
    if "retry_after_sec" in exc.details:
        headers["Retry-After"] = str(exc.details["retry_after_sec"])
    return JSONResponse(_body(request, exc.code, exc.message, exc.details), status_code=exc.status, headers=headers)


async def validation_handler(request: Request, exc: RequestValidationError):
    fields = {}
    for e in exc.errors():
        loc = [str(x) for x in e.get("loc", []) if x not in ("body", "query", "path", "header")]
        fields[".".join(loc) or "body"] = e.get("msg", "Invalid value")
    return JSONResponse(_body(request, "VALIDATION", DEFAULT_MESSAGES["VALIDATION"], {"fields": fields}), status_code=422)


async def http_error_handler(request: Request, exc: StarletteHTTPException):
    code = {400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
            405: "BAD_REQUEST", 429: "RATE_LIMITED"}.get(exc.status_code, "INTERNAL")
    msg = DEFAULT_MESSAGES.get(code) if exc.status_code != 405 else "Method not allowed."
    return JSONResponse(_body(request, code, msg, {}), status_code=exc.status_code)


async def unhandled_handler(request: Request, exc: Exception):
    log.exception("unhandled error request_id=%s", getattr(request.state, "request_id", None))
    return JSONResponse(_body(request, "INTERNAL", DEFAULT_MESSAGES["INTERNAL"], {}), status_code=500)
