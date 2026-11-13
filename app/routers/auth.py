"""§5 / §6 — OTP login via 2Factor, tokens, refresh, logout."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import Auth, get_auth
from app.errors import ApiError
from app.models import Gym, GymMember, GymStaff, Invite, OtpCode, Session as DbSession, User
from app.schemas import FirebaseLoginIn, OtpSendIn, OtpVerifyIn, RefreshIn
from app.permissions import effective_permissions
from app.security import gen_otp, hash_otp, hash_token, make_access_token, make_refresh_token, verify_otp
from app.services import sms
from app.services.cache import cache, rate_limit
from app.services.core import ser_user
from app.utils import iso, normalize_phone, utcnow
from app.responses import DocRoute, ERRORS, LoginOut, OtpSendOut, TokensOut

router = APIRouter(prefix="/auth", tags=["Auth"], route_class=DocRoute, responses=ERRORS)

LOCK_SEC = 15 * 60


def _check_lock(phone: str):
    ttl = cache.ttl(f"lock:{phone}")
    if cache.get(f"lock:{phone}") and ttl != -2:
        raise ApiError(423, "PHONE_LOCKED", None, {"retry_after_sec": max(1, ttl)})


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


@router.post("/otp/send", response_model=OtpSendOut, summary="Send login OTP (2Factor SMS)",
             description="Only when AUTH_PROVIDER=2factor (else 409 USE_FIREBASE). "
                         "Public. Rate limits: 1/30 s and 5/hour per phone, 20/hour per IP. "
                         "Never reveals whether the phone is registered.")
def otp_send(body: OtpSendIn, request: Request, db: Session = Depends(get_db)):
    if settings.auth_provider.lower() == "firebase":
        raise ApiError(409, "USE_FIREBASE", "Phone login uses Firebase. Send the code with the Firebase SDK, "
                       "then call POST /v1/auth/firebase.", {"auth_provider": "firebase",
                                                            "endpoint": "/v1/auth/firebase"})
    phone = normalize_phone(body.phone)
    _check_lock(phone)
    ip = _client_ip(request)
    rate_limit(f"otp30:{phone}", 1, 30, "OTP_RATE_LIMITED")
    rate_limit(f"otph:{phone}", 5, 3600, "OTP_RATE_LIMITED")
    rate_limit(f"otpip:{ip}", 20, 3600, "OTP_RATE_LIMITED")

    dev = bool(settings.otp_dev_code) and not settings.is_prod
    code = settings.otp_dev_code if dev else gen_otp()
    otp = OtpCode(phone=phone, code_hash=hash_otp(code), app=body.app,
                  expires_at=utcnow() + timedelta(seconds=settings.otp_ttl_sec), ip=ip,
                  user_agent=(request.headers.get("user-agent") or "")[:500])
    otp.provider_ref = "dev" if dev else sms.send_otp(phone, code)  # raises UPSTREAM_SMS
    db.add(otp)
    db.commit()
    return {"request_id": otp.id, "resend_after_sec": 30, "expires_in_sec": settings.otp_ttl_sec}


def _fail_attempt(db: Session, otp: OtpCode):
    otp.attempts += 1
    db.commit()
    if otp.attempts == 1:  # first wrong try on this code => counts as a failed code
        n = cache.incr(f"otpfail:{otp.phone}", LOCK_SEC)
        if n >= 3:
            cache.set(f"lock:{otp.phone}", "1", ex=LOCK_SEC)
            cache.delete(f"otpfail:{otp.phone}")
            raise ApiError(423, "PHONE_LOCKED", None, {"retry_after_sec": LOCK_SEC})
    left = max(0, 5 - otp.attempts)
    if left == 0:
        raise ApiError(410, "OTP_EXPIRED", "Too many wrong tries. Request a new code.")
    raise ApiError(400, "OTP_INVALID", None, {"attempts_left": left})


def staff_of(db: Session, user_id: str, activate: bool = False) -> list[dict]:
    rows = db.execute(select(GymStaff, Gym).join(Gym, Gym.id == GymStaff.gym_id)
                      .where(GymStaff.user_id == user_id, GymStaff.status.in_(("invited", "active")))).all()
    out = []
    for st, g in rows:
        if activate and st.status == "invited":
            st.status, st.joined_at = "active", utcnow()
        perms = effective_permissions(st.role, st.staff_role, bool(st.can_record_payments), st.permissions)
        out.append({"gym_id": g.id, "gym_name": g.name, "gym_status": g.status, "status_reason": g.status_reason,
                    "org_id": g.org_id or g.id, "role": st.role,
                    "staff_role": "owner" if st.role == "owner" else (st.staff_role or "staff"),
                    "can_record_payments": "payments.record" in perms, "permissions": sorted(perms)})
    return out


def create_session(db: Session, user: User, app: str, device_name=None, platform=None) -> dict:
    sess = DbSession(user_id=user.id, app=app, refresh_token_hash="pending", device_name=device_name,
                     platform=platform, expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days),
                     last_used_at=utcnow())
    db.add(sess)
    db.flush()
    refresh = make_refresh_token(sess.id)
    sess.refresh_token_hash = hash_token(refresh)
    return {"access_token": make_access_token(user.id, app, sess.id), "refresh_token": refresh,
            "expires_in_sec": settings.access_token_ttl_sec}


@router.post("/otp/verify", response_model=LoginOut, summary="Verify OTP and log in",
             description="Max 5 wrong attempts per code; 3 failed codes in 15 min locks the phone for 15 min. "
                         "Admin app: returns `staff_of` (403 NOT_GYM_STAFF if none). "
                         "Member app: returns `memberships_summary` + `new_claims` (auto-claim).")
def otp_verify(body: OtpVerifyIn, db: Session = Depends(get_db)):
    otp = db.get(OtpCode, body.request_id)
    if not otp:
        raise ApiError(410, "OTP_EXPIRED")
    _check_lock(otp.phone)
    if otp.consumed_at or otp.expires_at < utcnow() or otp.attempts >= 5:
        raise ApiError(410, "OTP_EXPIRED")
    if not verify_otp(otp.code_hash, body.code.strip()):
        _fail_attempt(db, otp)
    otp.consumed_at = utcnow()
    cache.delete(f"otpfail:{otp.phone}")
    return complete_login(db, otp.phone, otp.app, body.device)


def complete_login(db: Session, phone: str, app: str, device) -> dict:
    """Shared by OTP and Firebase login: upsert user, app context, session, claims."""
    user = db.scalar(select(User).where(User.phone == phone))
    is_new = user is None
    if is_new:
        user = User(phone=phone)
        db.add(user)
        db.flush()

    if app == "console" and not user.is_platform_admin:
        db.commit()
        raise ApiError(403, "FORBIDDEN", "This number doesn’t have console access.")

    gyms = staff_of(db, user.id, activate=app in ("admin", "web"))
    if app == "admin" and not gyms:
        db.commit()
        raise ApiError(403, "NOT_GYM_STAFF")

    now = utcnow()
    first_login = user.first_login_at is None
    if first_login:
        user.first_login_at = now
    user.last_login_at = now
    if user.deletion_requested_at:  # logging back in cancels a pending deletion
        user.deletion_requested_at = None
    # invites become "joined" once the person logs in
    for inv in db.scalars(select(Invite).where(Invite.phone == user.phone, Invite.status.in_(("queued", "sent")))):
        inv.status, inv.joined_at = "joined", now

    tokens = create_session(db, user, app, device.name if device else None, device.platform if device else None)
    resp = {**tokens, "user": {**ser_user(user), "is_new": is_new or not user.name}}

    if app in ("admin", "web", "console"):
        resp["staff_of"] = gyms
    if app in ("member", "web"):
        from app.routers.member_app import member_cards
        resp["memberships_summary"] = member_cards(db, user)
        claims = []
        rows = db.execute(select(GymMember, Gym).join(Gym, Gym.id == GymMember.gym_id)
                          .where(GymMember.user_id == user.id, GymMember.claimed_at.is_(None),
                                 GymMember.claim_disputed_at.is_(None))).all()
        for gm, g in rows:
            cur = next((c for c in resp["memberships_summary"] if c["member_id"] == gm.id), None)
            c = (cur or {}).get("current") or {}
            claims.append({"member_id": gm.id, "gym_id": g.id, "gym_name": g.name, "member_code": gm.member_code,
                           "plan_name": c.get("plan_name"), "start_date": c.get("start_date"),
                           "end_date": c.get("end_date")})
            gm.claimed_at = now
        resp["new_claims"] = claims
    db.commit()
    return resp


@router.post("/refresh", response_model=TokensOut, summary="Rotate refresh token",
             description="Refresh tokens rotate on every use. Re-using an old refresh token revokes the whole session.")
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    sid = body.refresh_token.split(".", 1)[0]
    sess = db.get(DbSession, sid)
    if not sess or sess.revoked_at or sess.expires_at < utcnow():
        raise ApiError(401, "SESSION_EXPIRED")
    if sess.refresh_token_hash != hash_token(body.refresh_token):
        sess.revoked_at = utcnow()  # token reuse => compromise; kill session
        db.commit()
        raise ApiError(401, "SESSION_EXPIRED")
    user = db.get(User, sess.user_id)
    if not user or user.deleted_at:
        raise ApiError(401, "SESSION_EXPIRED")
    new = make_refresh_token(sess.id)
    sess.refresh_token_hash = hash_token(new)
    sess.last_used_at = utcnow()
    db.commit()
    return {"access_token": make_access_token(user.id, sess.app, sess.id), "refresh_token": new,
            "expires_in_sec": settings.access_token_ttl_sec}


@router.post("/logout", status_code=204, summary="Log out (revoke this session)")
def logout(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    s = db.get(DbSession, auth.session.id)
    s.revoked_at = utcnow()
    db.commit()
    return Response(status_code=204)


@router.post("/firebase", response_model=LoginOut, summary="Log in with Firebase Phone Auth",
             description="The app verifies the phone with Firebase (SMS sent by Google), then sends the Firebase "
                         "`id_token` here. Response is identical to /auth/otp/verify.")
def firebase_login(body: FirebaseLoginIn, db: Session = Depends(get_db)):
    from app.services.firebase import verify_phone_token
    phone = normalize_phone(verify_phone_token(body.id_token))
    _check_lock(phone)
    return complete_login(db, phone, body.app, body.device)
