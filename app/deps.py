"""Request dependencies: current user, per-gym permission context, idempotency."""
from dataclasses import dataclass
from datetime import timedelta

import jwt
from fastapi import Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import ApiError
from app.models import Gym, GymStaff, IdempotencyKey, Session as DbSession, User
from app.security import decode_access_token
from app.services.cache import rate_limit
from app.utils import utcnow

bearer = HTTPBearer(auto_error=False, description="Access token from /auth/otp/verify or /auth/refresh")


@dataclass
class Auth:
    user: User
    session: DbSession
    app: str


def get_auth(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
             db: Session = Depends(get_db)) -> Auth:
    if not creds or creds.scheme.lower() != "bearer":
        raise ApiError(401, "UNAUTHENTICATED")
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "TOKEN_EXPIRED")
    except jwt.PyJWTError:
        raise ApiError(401, "UNAUTHENTICATED")
    sess = db.get(DbSession, claims["sid"])
    if not sess or sess.revoked_at or sess.expires_at < utcnow() or sess.user_id != claims["sub"]:
        raise ApiError(401, "SESSION_EXPIRED")
    user = db.get(User, claims["sub"])
    if not user or user.deleted_at:
        raise ApiError(401, "SESSION_EXPIRED")
    if request.method in ("GET", "HEAD", "OPTIONS"):
        rate_limit(f"user:r:{user.id}", 600, 60)  # dashboards fan out many reads
    else:
        rate_limit(f"user:w:{user.id}", 120, 60)
    request.state.user_id = user.id
    return Auth(user=user, session=sess, app=claims.get("app", sess.app))


def current_user(auth: Auth = Depends(get_auth)) -> User:
    return auth.user


def platform_admin(auth: Auth = Depends(get_auth)) -> User:
    if not auth.user.is_platform_admin:
        raise ApiError(403, "NOT_PLATFORM_ADMIN", "This account isn’t part of the DUMBBEL team console.")
    return auth.user


@dataclass
class GymCtx:
    gym: Gym
    staff: GymStaff
    user: User

    @property
    def is_owner(self) -> bool:
        return self.staff.role == "owner"

    @property
    def perms(self) -> frozenset:
        from app.permissions import effective_permissions
        return effective_permissions(self.staff.role, self.staff.staff_role, bool(self.staff.can_record_payments),
                                     self.staff.permissions)

    @property
    def role_name(self) -> str:
        return "owner" if self.is_owner else (self.staff.staff_role or "staff")

    def has(self, perm: str) -> bool:
        return perm in self.perms

    def require(self, perm: str):
        if not self.has(perm):
            code = "PAYMENT_NOT_ALLOWED" if perm.startswith("payments.") else "FORBIDDEN"
            raise ApiError(403, code, f"Your role doesn’t allow this ({perm}).", {"permission": perm})

    @property
    def can_pay(self) -> bool:
        """Money fields are only returned to roles with payments.view."""
        return self.has("payments.view")

    def require_owner(self):
        if not self.is_owner:
            raise ApiError(403, "OWNER_ONLY")

    def require_payments(self):
        self.require("payments.record")

    def require_approved(self):
        if self.gym.status != "active":
            raise ApiError(423, "GYM_PENDING")


def load_gym_ctx(db: Session, user: User, gym_id: str, allow_suspended: bool = False) -> GymCtx:
    gym = db.get(Gym, gym_id)
    if not gym:
        raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
    staff = db.scalar(select(GymStaff).where(GymStaff.gym_id == gym_id, GymStaff.user_id == user.id))
    if not staff or staff.status == "deactivated":
        raise ApiError(403, "NOT_GYM_STAFF", "You’re not on the staff of this gym.")
    if staff.status == "invited":  # first authenticated call activates an invite too
        staff.status, staff.joined_at = "active", utcnow()
        db.commit()
    if gym.status == "suspended" and not allow_suspended:
        raise ApiError(423, "GYM_SUSPENDED", f"This gym is suspended. {gym.status_reason or ''}".strip(),
                       {"reason": gym.status_reason})
    return GymCtx(gym=gym, staff=staff, user=user)


def gym_ctx(gymId: str = Path(..., description="Gym id"), user: User = Depends(current_user),
            db: Session = Depends(get_db)) -> GymCtx:
    return load_gym_ctx(db, user, gymId)


def owner_ctx(ctx: GymCtx = Depends(gym_ctx)) -> GymCtx:
    ctx.require_owner()
    return ctx


def perm_ctx(perm: str):
    """Dependency factory: gym context that requires one permission."""
    def dep(ctx: GymCtx = Depends(gym_ctx)) -> GymCtx:
        ctx.require(perm)
        return ctx
    dep.__name__ = f"require_{perm.replace('.', '_')}"
    return dep


def payments_ctx(ctx: GymCtx = Depends(gym_ctx)) -> GymCtx:
    ctx.require("payments.view")
    return ctx


# ---------------- idempotency ----------------
class Idem:
    """Usage:
        idem = Idem(db, user, key, "POST /gyms/x/payments")
        if idem.replay: return idem.replay
        ... do work, add rows ...
        return idem.respond(201, body)   # stores response in the same transaction and commits
    """

    def __init__(self, db: Session, user: User, key: str | None, route: str, required: bool = True):
        self.db, self.user, self.key, self.route = db, user, key, route
        self.replay = None
        if not key:
            if required:
                raise ApiError(400, "BAD_REQUEST", "Idempotency-Key header is required.", {"header": "Idempotency-Key"})
            return
        if len(key) > 100:
            raise ApiError(400, "BAD_REQUEST", "Idempotency-Key is too long.")
        row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user.id, IdempotencyKey.key == key))
        if row:
            if row.created_at > utcnow() - timedelta(hours=24):
                self.replay = JSONResponse(row.response, status_code=row.status_code,
                                           headers={"Idempotent-Replayed": "true"})
            else:
                db.delete(row)
                db.flush()

    def respond(self, status: int, body: dict):
        from fastapi.encoders import jsonable_encoder
        enc = jsonable_encoder(body)
        if self.key:
            self.db.add(IdempotencyKey(user_id=self.user.id, key=self.key, route=self.route,
                                       status_code=status, response=enc))
        try:
            self.db.commit()
        except IntegrityError as e:
            self.db.rollback()
            if self.key and "uq_idem_user_key" in str(e.orig):
                row = self.db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == self.user.id,
                                                                  IdempotencyKey.key == self.key))
                if row:
                    return JSONResponse(row.response, status_code=row.status_code)
            raise
        return JSONResponse(enc, status_code=status)


def idem_header(idempotency_key: str | None = Header(None, alias="Idempotency-Key",
                                                     description="UUID; same key within 24 h replays the response")):
    return idempotency_key
