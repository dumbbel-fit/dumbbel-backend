"""Per-request context (request id) usable from services and background logging."""
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
# scheme://host this request came in on (Cloud Run sets X-Forwarded-Proto) — used to build file links
# when PUBLIC_BASE_URL isn't configured, so links never point at localhost in production.
request_base_var: ContextVar[str | None] = ContextVar("request_base", default=None)


def current_request_id() -> str | None:
    return request_id_var.get()
