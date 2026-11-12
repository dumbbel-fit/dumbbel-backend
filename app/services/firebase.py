"""Verify Firebase Phone Auth ID tokens (signature, audience = our project, expiry, phone claim)."""
import logging
import threading

from app.config import settings
from app.errors import ApiError

log = logging.getLogger("dumbbel.firebase")
_lock = threading.Lock()
_app = None


def _get_app():
    global _app
    with _lock:
        if _app is None:
            import firebase_admin
            from firebase_admin import credentials
            if not settings.firebase_project_id:
                raise ApiError(503, "UPSTREAM_SMS", "Firebase login isn’t configured on the server (FIREBASE_PROJECT_ID).")
            cred = credentials.Certificate(settings.firebase_credentials_file) if settings.firebase_credentials_file else None
            _app = firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id}, name="dumbbel")
        return _app


def verify_phone_token(id_token: str) -> str:
    """Returns the verified phone number (E.164) or raises 401."""
    from firebase_admin import auth
    try:
        claims = auth.verify_id_token(id_token, app=_get_app(), check_revoked=False)
    except ApiError:
        raise
    except Exception as e:
        log.info("firebase token rejected: %s", type(e).__name__)
        raise ApiError(401, "UNAUTHENTICATED", "Phone verification expired. Please try again.")
    phone = claims.get("phone_number")
    if not phone or claims.get("firebase", {}).get("sign_in_provider") != "phone":
        raise ApiError(401, "UNAUTHENTICATED", "Please sign in with your phone number.")
    return phone
