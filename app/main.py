"""DUMBBEL.fit v1 API — FastAPI app. Swagger UI at /docs, ReDoc at /redoc, OpenAPI JSON at /openapi.json."""
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.errors import (DEFAULT_MESSAGES, ApiError, api_error_handler, http_error_handler, unhandled_handler,
                        validation_handler)
from app.utils import mask_phones_in_text, version_tuple


class PhoneMaskFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = mask_phones_in_text(str(record.getMessage()))
            record.args = ()
        except Exception:
            pass
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
for h in logging.getLogger().handlers:
    h.addFilter(PhoneMaskFilter())
log = logging.getLogger("dumbbel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_tables:
        from app.db import Base, engine
        import app.models  # noqa: F401
        Base.metadata.create_all(engine)
    sched = None
    if settings.run_scheduler:
        from app.services.scheduled import start_scheduler
        sched = start_scheduler()
    yield
    if sched:
        sched.shutdown(wait=False)


TAGS = [
    {"name": "Auth", "description": "Phone + OTP login (2Factor SMS), token refresh, logout."},
    {"name": "Me", "description": "Profile, account deletion, push devices."},
    {"name": "Common", "description": "Health and app config (public)."},
    {"name": "Gym setup", "description": "Register gym, documents, verification, profile, plans."},
    {"name": "Members & memberships", "description": "Members, assign/renew, freeze, unfreeze, cancel."},
    {"name": "Payments", "description": "Record, list, void, receipts. Money fields need payments permission."},
    {"name": "Check-ins & QR", "description": "QR scan, manual check-in, live feed, stats."},
    {"name": "Import & invites", "description": "4-step member import and app invites."},
    {"name": "Staff, insights & activity", "description": "Staff management, home summary, reminders, audit log."},
    {"name": "Member app", "description": "Member’s own memberships, rotating QR, history, insights, payments."},
    {"name": "Internal console", "description": "Platform admins only: gym approvals, support phone change."},
    {"name": "Files", "description": "Presigned uploads (S3/R2 or local)."},
]

DESCRIPTION = """
One backend for the Gym Admin app, Member app, landing page and internal console.

**Auth:** call `POST /v1/auth/otp/send` → `POST /v1/auth/otp/verify`, then click **Authorize** and paste the `access_token`.

**Conventions:** money in paise (integers) · phones accepted as `{"country":"IN","number":"98…"}` or E.164 ·
timestamps ISO 8601 UTC · business dates in the gym timezone (Asia/Kolkata) · cursor pagination (`limit` ≤ 100, `next_cursor`) ·
POSTs that create money or check-ins need an `Idempotency-Key` header (UUID; same key within 24 h replays the response).

**Headers:** `X-App: admin | member | web | console` and `X-App-Version` (below the minimum → 426 APP_UPDATE_REQUIRED).

**Errors:** `{"error": {"code", "message", "details", "request_id"}}` — `message` is safe to show users.
"""

app = FastAPI(title="DUMBBEL.fit API", version="1.0.0", description=DESCRIPTION, openapi_tags=TAGS,
              lifespan=lifespan, docs_url="/docs", redoc_url="/redoc",
              swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True,
                                     "docExpansion": "none", "filter": True})

app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-Request-Id", "Retry-After", "Idempotent-Replayed"])

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(Exception, unhandled_handler)

VERSION_EXEMPT = ("/v1/config", "/v1/health", "/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = rid
    start = time.perf_counter()
    path = request.url.path
    x_app = (request.headers.get("x-app") or "").lower()
    ver = request.headers.get("x-app-version")
    mins = {"admin": settings.min_admin_version, "member": settings.min_member_version}
    if x_app in mins and ver and not path.startswith(VERSION_EXEMPT) and version_tuple(ver) < version_tuple(mins[x_app]):
        resp = JSONResponse({"error": {"code": "APP_UPDATE_REQUIRED", "message": DEFAULT_MESSAGES["APP_UPDATE_REQUIRED"],
                                       "details": {"min_version": mins[x_app]}, "request_id": rid}}, status_code=426)
    elif settings.maintenance and not path.startswith(VERSION_EXEMPT) and not path.startswith("/v1/internal"):
        resp = JSONResponse({"error": {"code": "MAINTENANCE", "message": "We’re doing quick maintenance. Back shortly!",
                                       "details": {}, "request_id": rid}}, status_code=503)
    else:
        try:
            resp = await call_next(request)
        except Exception as e:  # pragma: no cover
            resp = await unhandled_handler(request, e)
    resp.headers["X-Request-Id"] = rid
    log.info("%s %s %s %.0fms app=%s rid=%s", request.method, path, resp.status_code,
             (time.perf_counter() - start) * 1000, x_app or "-", rid)
    return resp


from app.routers import auth, checkins, gyms, imports, internal, me, member_app, members, payments, staff  # noqa: E402

P = settings.api_prefix
for r in (auth.router, me.public, me.router, member_app.router, gyms.router, members.router, payments.router,
          checkins.router, imports.public, imports.router, staff.router, internal.router, internal.uploads):
    app.include_router(r, prefix=P)


if not settings.is_prod:
    from app.routers import devtools  # noqa: E402
    app.include_router(devtools.router)


@app.get("/", include_in_schema=False)
def root():
    return {"name": "DUMBBEL.fit API", "version": "1.0.0", "docs": "/docs", "base": P}
