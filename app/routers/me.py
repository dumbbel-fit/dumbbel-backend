"""§6 — /me profile, account deletion, push devices; /health, /config."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import Auth, current_user, get_auth
from app.errors import ApiError
from app.models import DeviceToken, GymStaff, Session as DbSession, User
from app.routers.auth import staff_of
from app.schemas import DeviceTokenIn, MePatch
from app.services.core import ser_user
from app.utils import today_in, utcnow

router = APIRouter(tags=["Me"])
public = APIRouter(tags=["Common"])


@router.get("/me", summary="Current user, gyms they staff, weekly goal")
def get_me(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    u = auth.user
    return {"user": ser_user(u), "staff_of": staff_of(db, u.id), "weekly_goal": u.weekly_goal}


@router.patch("/me", summary="Update profile / weekly goal")
def patch_me(body: MePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if "photo_key" in data:
        user.photo_url = data.pop("photo_key")
    for k, v in data.items():
        setattr(user, k, v)
    db.commit()
    return {"user": ser_user(user)}


@router.delete("/me", status_code=202, summary="Request account deletion (processed within 30 days)",
               description="Staff must be removed from every gym first (409 STAFF_MUST_BE_REMOVED).")
def delete_me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    active_staff = db.scalar(select(GymStaff).where(GymStaff.user_id == user.id,
                                                    GymStaff.status.in_(("invited", "active"))).limit(1))
    if active_staff:
        raise ApiError(409, "STAFF_MUST_BE_REMOVED",
                       "You’re still staff at a gym. Ask the owner to remove you first.")
    now = utcnow()
    user.deletion_requested_at = now
    db.execute(update(DbSession).where(DbSession.user_id == user.id, DbSession.revoked_at.is_(None))
               .values(revoked_at=now))
    db.commit()
    return {"deletion_scheduled_for": (today_in(None) + timedelta(days=30)).isoformat()}


@router.post("/me/devices", status_code=204, summary="Register Expo push token")
def add_device(body: DeviceTokenIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(DeviceToken).where(DeviceToken.expo_push_token == body.expo_push_token))
    if row:
        row.user_id, row.app, row.platform, row.last_seen_at = user.id, body.app, body.platform, utcnow()
    else:
        db.add(DeviceToken(user_id=user.id, app=body.app, platform=body.platform,
                           expo_push_token=body.expo_push_token, last_seen_at=utcnow()))
    db.commit()
    return Response(status_code=204)


@router.delete("/me/devices/{token}", status_code=204, summary="Remove push token")
def remove_device(token: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.query(DeviceToken).filter(DeviceToken.expo_push_token == token, DeviceToken.user_id == user.id).delete()
    db.commit()
    return Response(status_code=204)


@public.get("/health", summary="Uptime check")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        ok = True
    except Exception:
        ok = False
    return {"status": "ok" if ok else "degraded", "db": ok, "time": utcnow().isoformat()}


@public.get("/config", summary="Min app versions, support phone, maintenance flag, login provider",
            description="`auth_provider`: `firebase` → use Firebase SDK then POST /auth/firebase; `2factor` → /auth/otp/send + /auth/otp/verify.")
def config():
    return {"min_app_version": {"admin": settings.min_admin_version, "member": settings.min_member_version},
            "support_phone": settings.support_phone, "maintenance": settings.maintenance,
            "auth_provider": settings.auth_provider.lower(),
            "firebase_project_id": settings.firebase_project_id}
