"""Staff at work: staff scan the gym QR to start / end their shift, with location (taken only at those two moments), and what
each person did at the gym (members added, payments collected, renewals, check-ins handled, last active).
The owner sees everyone (staff.manage); every staff member sees and records only their own shift."""
import hmac
from datetime import timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, gym_ctx, perm_ctx
from app.errors import ApiError
from app.models import AuditLog, Checkin, GymStaff, Payment, StaffShift, User
from app.responses import DocRoute, ERRORS
from app.routers.checkins import haversine_m, parse_gym_code
from app.security import gym_static_sig, parse_gym_display_token
from app.services.cache import rate_limit
from app.services.audit import audit
from app.services.core import gym_settings
from app.utils import dstr, iso, local_midnight_utc, today_in, utcnow

router = APIRouter(tags=["Staff at work"], route_class=DocRoute, responses=ERRORS)
RANGES = {"today": 1, "7d": 7, "30d": 30}
Range = Literal["today", "7d", "30d"]
FORGOT_AFTER_H = 16  # an open shift older than this is treated as "forgot to check out"


class Where(BaseModel):
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0)


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _where(gym, w: Where | None) -> dict:
    """lat/lng as given, and how far from the gym (when both are known)."""
    if not w or w.lat is None or w.lng is None:
        return {"lat": None, "lng": None, "acc": None, "dist": None}
    dist = None
    if gym.lat is not None and gym.lng is not None:
        dist = int(round(haversine_m(float(gym.lat), float(gym.lng), w.lat, w.lng)))
    return {"lat": w.lat, "lng": w.lng, "acc": int(w.accuracy_m) if w.accuracy_m is not None else None, "dist": dist}


def _place(gym, dist: int | None, acc: int | None, has_point: bool) -> str:
    """at_gym / away / unknown (gym location not set) / not_shared."""
    if not has_point:
        return "not_shared"
    if dist is None:
        return "unknown"
    radius = int(gym_settings(gym).get("checkin_radius_m", 150))
    return "at_gym" if dist <= radius + min(acc or 0, 100) else "away"


def ser_shift(gym, s: StaffShift) -> dict:
    now = utcnow()
    in_at, out_at = _utc(s.in_at), _utc(s.out_at)
    forgot = out_at is None and (now - in_at) > timedelta(hours=FORGOT_AFTER_H)
    end = out_at or (None if forgot else now)
    mins = int((end - in_at).total_seconds() // 60) if end else None
    pt = lambda lat, lng: {"lat": float(lat), "lng": float(lng), "map_url": f"https://maps.google.com/?q={float(lat)},{float(lng)}"} \
        if lat is not None and lng is not None else None  # noqa: E731
    return {"id": s.id, "staff_id": s.staff_id, "date": dstr(s.local_date), "in_at": iso(in_at), "out_at": iso(out_at),
            "open": out_at is None and not forgot, "forgot_checkout": forgot, "minutes": mins,
            "in": {"place": _place(gym, s.in_distance_m, s.in_accuracy_m, s.in_lat is not None), "distance_m": s.in_distance_m,
                   "accuracy_m": s.in_accuracy_m, "point": pt(s.in_lat, s.in_lng)},
            "out": {"place": _place(gym, s.out_distance_m, s.out_accuracy_m, s.out_lat is not None), "distance_m": s.out_distance_m,
                    "accuracy_m": s.out_accuracy_m, "point": pt(s.out_lat, s.out_lng)} if out_at else None}


def _open_shift(db: Session, ctx: GymCtx) -> StaffShift | None:
    s = db.scalar(select(StaffShift).where(StaffShift.staff_id == ctx.staff.id, StaffShift.out_at.is_(None))
                  .order_by(StaffShift.in_at.desc()).limit(1))
    if s and utcnow() - _utc(s.in_at) > timedelta(hours=FORGOT_AFTER_H):
        return None  # left open from an earlier day: it stays as "forgot to check out"
    return s


def _gym_place_info(gym) -> dict:
    return {"gym_location_set": gym.lat is not None and gym.lng is not None,
            "radius_m": int(gym_settings(gym).get("checkin_radius_m", 150))}


@router.get("/gyms/{gymId}/staff/me/shift", summary="My shift today (any staff member)")
def my_shift(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    today = today_in(gym.timezone)
    rows = db.scalars(select(StaffShift).where(StaffShift.staff_id == ctx.staff.id, StaffShift.local_date == today)
                      .order_by(StaffShift.in_at)).all()
    open_s = _open_shift(db, ctx)
    shifts = [ser_shift(gym, s) for s in rows]
    return {"on_shift": open_s is not None, "current": ser_shift(gym, open_s) if open_s else None, "today": shifts,
            "minutes_today": sum(s["minutes"] or 0 for s in shifts), **_gym_place_info(gym)}


class ScanIn(BaseModel):
    code: str = Field(..., description="The scanned gym QR: the full https://dumbbel.fit/checkin?... URL (or its query)")
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0)


JUST_STARTED_MIN = 2  # a second scan within this many minutes is a double scan, not "end my shift"


@router.post("/gyms/{gymId}/staff/me/scan", summary="Staff attendance: scan the gym QR to start, scan again to end the shift",
             description="The only way staff mark attendance. Needs the genuine, current QR of this branch and the phone's "
                         "location inside the check-in radius. First scan starts the shift, the next one ends it. "
                         "Errors: WRONG_QR (not this branch's current QR), LOCATION_REQUIRED, TOO_FAR, "
                         "GYM_LOCATION_NOT_SET (owner must set it first), JUST_STARTED (scanned twice).")
def scan(body: ScanIn, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    rate_limit(f"staffscan:{ctx.user.id}", 12, 60)
    gym = ctx.gym
    gym_id, token, sig = parse_gym_code(body.code)
    ok = False
    if gym_id == gym.id:
        if token:
            data = parse_gym_display_token(token)
            ok = bool(data and data["g"] == gym.id)
        elif sig and gym.checkin_secret:
            ok = hmac.compare_digest(gym_static_sig(gym.id, gym.checkin_secret), sig)
    if not ok:
        raise ApiError(422, "WRONG_QR", "This isn’t the current check-in QR of this branch. Scan the QR poster at the entrance.")
    if gym.lat is None or gym.lng is None:
        raise ApiError(409, "GYM_LOCATION_NOT_SET", "The gym’s location isn’t set yet, so attendance can’t be checked. "
                                                     "Ask the owner to set it under Check-in QR → Rules & location.")
    if body.lat is None or body.lng is None:
        raise ApiError(422, "LOCATION_REQUIRED", "Allow location for this site and scan again. It confirms you’re at the gym.")
    w = _where(gym, Where(lat=body.lat, lng=body.lng, accuracy_m=body.accuracy_m))
    radius = int(gym_settings(gym).get("checkin_radius_m", 150))
    if w["dist"] > radius + min(w["acc"] or 0, 100):
        raise ApiError(422, "TOO_FAR", f"You’re {w['dist']} m from the gym. Scan from inside the gym.", {"distance_m": w["dist"]})
    s = _open_shift(db, ctx)
    if s and utcnow() - _utc(s.in_at) < timedelta(minutes=JUST_STARTED_MIN):
        raise ApiError(409, "JUST_STARTED", "Your shift has just started. Scan again when you leave to end it.")
    if s:
        s.out_at, s.out_lat, s.out_lng, s.out_accuracy_m, s.out_distance_m = utcnow(), w["lat"], w["lng"], w["acc"], w["dist"]
        out = ser_shift(gym, s)
        h, m = divmod(out["minutes"] or 0, 60)
        audit(db, gym.id, ctx.user.id, "staff.check_out", "staff", ctx.staff.id, f"{ctx.user.name or 'Staff'} ended shift after {h}h {m}m (QR)")
        action = "out"
    else:
        s = StaffShift(gym_id=gym.id, staff_id=ctx.staff.id, user_id=ctx.user.id, local_date=today_in(gym.timezone), in_at=utcnow(),
                       in_lat=w["lat"], in_lng=w["lng"], in_accuracy_m=w["acc"], in_distance_m=w["dist"])
        db.add(s)
        db.flush()
        out = ser_shift(gym, s)
        audit(db, gym.id, ctx.user.id, "staff.check_in", "staff", ctx.staff.id, f"{ctx.user.name or 'Staff'} started shift at the gym (QR)")
        action = "in"
    db.commit()
    return {"action": action, "shift": out, "gym": {"id": gym.id, "name": gym.name}}


def _since(gym, rng: str):
    today = today_in(gym.timezone)
    start = today - timedelta(days=RANGES[rng] - 1)
    return today, start, local_midnight_utc(start, gym.timezone)


@router.get("/gyms/{gymId}/staff/work", summary="Every staff member at work: attendance, where they checked in, what they did (staff.manage)")
def staff_work(rng: Range = Query("7d", alias="range"), ctx: GymCtx = Depends(perm_ctx("staff.manage")), db: Session = Depends(get_db)):
    gym = ctx.gym
    today, start, since = _since(gym, rng)
    staff = db.execute(select(GymStaff, User).join(User, User.id == GymStaff.user_id)
                       .where(GymStaff.gym_id == gym.id, GymStaff.status != "deactivated")
                       .order_by(GymStaff.role.desc(), User.name)).all()
    uids = [u.id for _, u in staff]
    shifts: dict[str, list] = {}
    for s in db.scalars(select(StaffShift).where(StaffShift.gym_id == gym.id, StaffShift.local_date >= start).order_by(StaffShift.in_at)):
        shifts.setdefault(s.staff_id, []).append(ser_shift(gym, s))
    acts: dict[str, dict] = {}
    for uid, action, n, last in db.execute(select(AuditLog.actor_user_id, AuditLog.action, func.count(), func.max(AuditLog.at))
                                           .where(AuditLog.gym_id == gym.id, AuditLog.at >= since, AuditLog.actor_user_id.in_(uids or [""]))
                                           .group_by(AuditLog.actor_user_id, AuditLog.action)):
        a = acts.setdefault(uid, {"by": {}, "total": 0, "last": None})
        a["by"][action] = n
        if not action.startswith("staff.check_"):
            a["total"] += n
            a["last"] = max(filter(None, [a["last"], last]), default=None)
    pays = {uid: (n, int(v or 0)) for uid, n, v in db.execute(
        select(Payment.recorded_by, func.count(), func.sum(Payment.amount_paise))
        .where(Payment.gym_id == gym.id, Payment.status == "recorded", Payment.source == "manual", Payment.paid_at >= since,
               Payment.recorded_by.in_(uids or [""])).group_by(Payment.recorded_by))}
    cis = dict(db.execute(select(Checkin.staff_user_id, func.count()).where(
        Checkin.gym_id == gym.id, Checkin.result == "success", Checkin.local_date >= start,
        Checkin.staff_user_id.in_(uids or [""])).group_by(Checkin.staff_user_id)).all())
    out = []
    for st, u in staff:
        sh = shifts.get(st.id, [])
        a = acts.get(u.id, {"by": {}, "total": 0, "last": None})
        by = a["by"]
        today_sh = [x for x in sh if x["date"] == dstr(today)]
        last_active = max(filter(None, [_utc(u.last_login_at), _utc(a["last"])]), default=None)
        out.append({
            "staff_id": st.id, "user_id": u.id, "name": u.name, "phone": u.phone,
            "role": "owner" if st.role == "owner" else (st.staff_role or "staff"), "status": st.status,
            "on_shift": any(x["open"] for x in sh),
            "today": {"in_at": today_sh[0]["in_at"] if today_sh else None,
                      "out_at": today_sh[-1]["out_at"] if today_sh and not today_sh[-1]["open"] else None,
                      "minutes": sum(x["minutes"] or 0 for x in today_sh),
                      "place": today_sh[0]["in"]["place"] if today_sh else None},
            "days_present": len({x["date"] for x in sh}), "minutes": sum(x["minutes"] or 0 for x in sh),
            "checkins_away": sum(1 for x in sh if x["in"]["place"] == "away"),
            "checkins_no_location": sum(1 for x in sh if x["in"]["place"] == "not_shared"),
            "forgot_checkout": sum(1 for x in sh if x["forgot_checkout"]),
            "work": {"members_added": by.get("member.create", 0), "plans_assigned": by.get("membership.assign", 0),
                     "renewals": by.get("membership.renew", 0), "payments": pays.get(u.id, (0, 0))[0],
                     "collected_paise": pays.get(u.id, (0, 0))[1], "checkins_handled": cis.get(u.id, 0),
                     "actions": a["total"]},
            "last_active_at": iso(last_active), "last_login_at": iso(_utc(u.last_login_at)),
        })
    return {"range": rng, "from": dstr(start), "to": dstr(today), "items": out,
            "on_shift_now": sum(1 for r in out if r["on_shift"]), **_gym_place_info(gym)}


@router.get("/gyms/{gymId}/staff/{staffId}/shifts", summary="One staff member's check-ins and check-outs with location (staff.manage)")
def staff_shifts(staffId: str, rng: Range = Query("30d", alias="range"), ctx: GymCtx = Depends(perm_ctx("staff.manage")),
                 db: Session = Depends(get_db)):
    gym = ctx.gym
    st = db.get(GymStaff, staffId)
    if not st or st.gym_id != gym.id:
        raise ApiError(404, "NOT_FOUND", "That staff member wasn’t found.")
    _, start, _ = _since(gym, rng)
    rows = db.scalars(select(StaffShift).where(StaffShift.staff_id == st.id, StaffShift.local_date >= start)
                      .order_by(StaffShift.in_at.desc())).all()
    u = db.get(User, st.user_id)
    return {"staff": {"id": st.id, "name": u.name if u else None, "phone": u.phone if u else None},
            "items": [ser_shift(gym, s) for s in rows], **_gym_place_info(gym)}
