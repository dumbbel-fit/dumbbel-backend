"""§10 — check-ins. Primary flow (v1.3): every branch shows its own QR; the member scans it with their phone
(POST /me/checkin). Staff scanning the member's QR and manual check-in remain as fallbacks."""
import hmac
import math
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.deps import GymCtx, Idem, current_user, gym_ctx, idem_header, perm_ctx
from app.errors import ApiError
from app.models import Checkin, Gym, GymMember, GymStaff, Membership, User
from app.schemas import ManualCheckinIn, ScanIn
from app.security import gym_static_sig, parse_gym_display_token, parse_qr_token
from app.services import storage
from app.services.audit import audit
from app.services.cache import cache, rate_limit
from app.services.core import gym_settings, user_names
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.rules import LIVE, PLAN_DENIALS, days_left, load_states, member_state, streaks, visit_days
from app.utils import iso, local_date_of, local_midnight_utc, today_in, tz, utcnow
from app.responses import DocRoute, ERRORS, CheckinFeedOut, CheckinResultOut, CheckinStatsOut, GymQrOut

router = APIRouter(tags=["Check-ins & QR"], route_class=DocRoute, responses=ERRORS)
me_router = APIRouter(prefix="/me", tags=["Check-ins & QR"], route_class=DocRoute, responses=ERRORS)

MESSAGES = {
    "success": "Checked in. Have a great session!",
    "duplicate": "Already checked in a little while ago.",
    "expired": "Membership has expired. Renew to check in.",
    "frozen": "Membership is frozen right now.",
    "suspended": "Membership is suspended. Please talk to the front desk.",
    "cancelled": "Membership was cancelled.",
    "not_started": "Membership hasn’t started yet.",
    "not_member": "Not a member of this gym.",
    "invalid_token": "This QR isn’t valid any more. Scan the gym’s current QR.",
    "too_far": "You need to be at the gym to check in.",
    "location_required": "Allow location access so we can confirm you’re at the gym.",
    "gym_inactive": "QR check-in unlocks once the gym is approved. Use manual check-in.",
}
STATUS_TO_RESULT = {"cancelled": "cancelled", "frozen": "frozen", "suspended": "suspended", "upcoming": "not_started",
                    "expired": "expired", "no_plan": "expired"}


def process_checkin(db: Session, ctx, method: str, *, token: str | None = None,
                    member_id: str | None = None, device_id: str | None = None, note: str | None = None,
                    code_ok: bool = True, location: dict | None = None) -> dict:
    """Shared by staff QR scan ("qr"), manual ("manual") and member scanning the gym QR ("self").
    For "self": code_ok = gym QR verified by the caller; location = {lat, lng, accuracy_m} from the phone."""
    gym = ctx.gym
    staff_user_id = None if method == "self" else ctx.user.id
    distance = None
    now = utcnow()
    local = local_date_of(now, gym.timezone)
    member = membership = None
    jti = None
    extra: dict = {}

    def finish(result: str) -> dict:
        c = Checkin(gym_id=gym.id, member_id=member.id if member else None,
                    membership_id=membership.id if membership else None, at=now, local_date=local, method=method,
                    result=result, staff_user_id=staff_user_id, token_jti=jti, device_id=device_id, note=note,
                    distance_m=distance)
        db.add(c)
        db.flush()
        out = {"result": result, "message": MESSAGES[result], "checkin_id": c.id, "at": iso(now), "method": method,
               "gym": {"id": gym.id, "name": gym.name}}
        if distance is not None:
            out["distance_m"] = distance
        if member:
            u = db.get(User, member.user_id)
            out["member"] = {"id": member.id, "name": member.display_name, "member_code": member.member_code,
                             "photo_url": storage.public_url(u.photo_url)}
        if membership:
            out["membership"] = {"id": membership.id, "plan_name": membership.plan_name,
                                 "end_date": membership.end_date.isoformat(),
                                 "days_left": days_left(membership, extra.get("status", "active"), local),
                                 "status": extra.get("status")}
        out.update({k: v for k, v in extra.items() if k != "status"})
        return out

    # 1. gym active (pending gyms: manual only)
    if gym.status != "active" and not (method == "manual" and gym.status == "pending"):
        return finish("gym_inactive")
    if method == "qr":
        # 2. signature / version / expiry
        payload = parse_qr_token(token or "")
        if not payload:
            return finish("invalid_token")
        jti = payload["j"]
        # 3. replay (screenshot reuse)
        if not cache.set(f"qrjti:{jti}", "1", ex=120, nx=True):
            return finish("invalid_token")
        member_id = payload["m"]
        if payload["g"] != gym.id:
            member = None
            return finish("not_member")
    # 4. member belongs to this gym
    member = db.get(GymMember, member_id) if member_id else None
    if not member or member.gym_id != gym.id or member.status == "archived" or member.claim_disputed_at:
        if member and member.gym_id != gym.id:
            member = None
        return finish("not_member")
    if method == "self":
        # 4b. the gym QR the member scanned must be genuine & current
        if not code_ok:
            return finish("invalid_token")
        # 4c. they must be at the gym (when the gym requires it and has set its location)
        s = gym_settings(gym)
        if s.get("checkin_require_location", True) and gym.lat is not None and gym.lng is not None:
            if not location or location.get("lat") is None or location.get("lng") is None:
                return finish("location_required")
            distance = int(round(haversine_m(float(gym.lat), float(gym.lng), location["lat"], location["lng"])))
            slack = min(float(location.get("accuracy_m") or 0), 100.0)
            if distance > int(s.get("checkin_radius_m", 150)) + slack:
                return finish("too_far")
    # 5. membership status
    st = member_state(db, gym, member.id)
    membership = st.current
    extra["status"] = st.status
    if ctx.can_pay:
        extra["due_paise"] = st.due_paise
    if st.status in STATUS_TO_RESULT:
        return finish(STATUS_TO_RESULT[st.status])
    # 6. cooldown
    cooldown = int(gym_settings(gym)["checkin_cooldown_min"])
    prev = db.scalar(select(Checkin).where(Checkin.member_id == member.id, Checkin.result == "success",
                                           Checkin.at >= now - timedelta(minutes=cooldown))
                     .order_by(Checkin.at.desc()).limit(1))
    if prev:
        extra["previous_at"] = iso(prev.at)
        return finish("duplicate")
    # 7. success
    member.last_checkin_at = now
    days = visit_days(db, member.id)
    if local not in days:
        days = sorted(days + [local])
    cur, best = streaks(days, local)
    member.best_streak = max(member.best_streak or 0, best)
    extra["streak"] = cur
    out = finish("success")
    try:  # engagement: XP / level / newly earned badges for the celebration screen
        from app.services.progress import sync_badges
        u = db.get(User, member.user_id)
        _, new_badges, lvl = sync_badges(db, member, gym, u.weekly_goal if u else 4)
        out["level"] = {k: lvl[k] for k in ("level", "name", "xp", "next_level_xp", "next_name", "progress")}
        out["new_badges"] = [{"key": b["key"], "name": b["name"], "description": b["description"], "icon": b["icon"]}
                             for b in new_badges]
    except Exception:  # never block a check-in on gamification
        pass
    if method != "self":  # the member is looking at the result on their own phone in that case
        enqueue("push.send", send_push, member.user_id, "member", "Checked in",
                f"Checked in at {gym.name} · day {cur}", {"type": "checkin_success", "gym_id": gym.id})
    return out


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@router.post("/gyms/{gymId}/checkins/scan", response_model=CheckinResultOut, summary="Scan member QR",
             description="Requires `Idempotency-Key`. Always 200 for a processed scan — `result` drives the screen: "
                         "success | duplicate | expired | frozen | cancelled | not_started | not_member | invalid_token | gym_inactive. "
                         "Rate limit 60/min per staff device.")
def scan(body: ScanIn, ctx: GymCtx = Depends(perm_ctx("checkins.manage")), db: Session = Depends(get_db),
         idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/checkins/scan")
    if idem.replay:
        return idem.replay
    rate_limit(f"scan:{ctx.user.id}:{body.device_id or 'default'}", 60, 60)
    out = process_checkin(db, ctx, "qr", token=body.token, device_id=body.device_id)
    return idem.respond(200, out)


@router.post("/gyms/{gymId}/checkins/manual", response_model=CheckinResultOut, summary="Manual check-in (fallback; works for pending gyms)",
             description="Requires `Idempotency-Key`. Same response shape as scan with method `manual`.")
def manual(body: ManualCheckinIn, ctx: GymCtx = Depends(perm_ctx("checkins.manage")), db: Session = Depends(get_db),
           idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/checkins/manual")
    if idem.replay:
        return idem.replay
    out = process_checkin(db, ctx, "manual", member_id=body.member_id, note=body.note)
    name = (out.get("member") or {}).get("name", "unknown member")
    audit(db, ctx.gym.id, ctx.user.id, "checkin.manual", "checkin", out["checkin_id"],
          f"Manual check-in for {name}: {out['result']}")
    return idem.respond(200, out)


@router.get("/gyms/{gymId}/checkins", response_model=CheckinFeedOut, summary="Check-in feed (poll every 10 s with `since`)")
def feed(date: str | None = Query(None, examples=["2026-11-12"]), result: Literal["success", "denied", "plan"] | None = None,
         since: datetime | None = Query(None, description="Only items after this timestamp (polling)"),
         order: Literal["latest", "earliest"] = Query("latest", description="earliest = the day in arrival order (first in at the top)"),
         limit: int = Query(100, ge=1, le=200), ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    from datetime import date as _date
    d = _date.fromisoformat(date) if date else today_in(gym.timezone)
    conds = [Checkin.gym_id == gym.id, Checkin.local_date == d]
    counts = dict(db.execute(select(Checkin.result == "success", func.count()).where(*conds)
                             .group_by(Checkin.result == "success")).all())
    if result == "success":
        conds.append(Checkin.result == "success")
    elif result == "denied":
        conds.append(Checkin.result != "success")
    elif result == "plan":
        conds.append(Checkin.result.in_(PLAN_DENIALS))
    if since:
        conds.append(Checkin.at > (since if since.tzinfo else since.replace(tzinfo=tz("UTC"))))
    rows = db.scalars(select(Checkin).where(*conds).order_by(Checkin.at.asc() if order == "earliest" else Checkin.at.desc())
                      .limit(limit)).all()
    member_ids = list({r.member_id for r in rows if r.member_id})
    members = {m.id: m for m in db.scalars(select(GymMember).where(GymMember.id.in_(member_ids or [""])))}
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in members.values()] or [""])))}
    mss = {m.id: m for m in db.scalars(select(Membership).where(Membership.id.in_([r.membership_id for r in rows if r.membership_id] or [""])))}
    staff = user_names(db, [r.staff_user_id for r in rows])
    # each member's very first successful check-in here, and the first successful one of this day
    first_ever = dict(db.execute(select(Checkin.member_id, func.min(Checkin.at)).where(
        Checkin.gym_id == gym.id, Checkin.result == "success", Checkin.member_id.in_(member_ids or [""]))
        .group_by(Checkin.member_id)).all())
    first_today = db.scalar(select(func.min(Checkin.at)).where(
        Checkin.gym_id == gym.id, Checkin.local_date == d, Checkin.result == "success"))
    counts_plan = db.scalar(select(func.count()).select_from(Checkin).where(
        Checkin.gym_id == gym.id, Checkin.local_date == d, Checkin.result.in_(PLAN_DENIALS))) or 0
    items = []
    for r in rows:
        m = members.get(r.member_id)
        u = users.get(m.user_id) if m else None
        ms = mss.get(r.membership_id)
        it = {"id": r.id, "at": iso(r.at), "method": r.method, "result": r.result,
              "message": MESSAGES.get(r.result),
              "member": {"id": m.id, "name": m.display_name, "member_code": m.member_code,
                         "phone": u.phone if u else None,
                         "photo_url": storage.public_url(u.photo_url) if u else None} if m else None,
              "plan_name": ms.plan_name if ms else None, "staff_name": staff.get(r.staff_user_id),
              "first_visit": r.result == "success" and first_ever.get(r.member_id) == r.at,
              "first_in_today": r.result == "success" and first_today is not None and r.at == first_today}
        if ms:
            it["plan_end_date"] = ms.end_date.isoformat()
            if r.result == "expired":
                it["days_since_expiry"] = (r.local_date - ms.end_date).days
        items.append(it)
    return {"date": d.isoformat(), "items": items,
            "counts": {"success": int(counts.get(1, 0) or counts.get(True, 0)),
                       "denied": int(counts.get(0, 0) or counts.get(False, 0)), "plan_denied": int(counts_plan)},
            "first_in": iso(first_today), "server_time": iso(utcnow())}


def _range_start(range: str, today):
    return {"today": today, "week": today - timedelta(days=6), "month": today.replace(day=1),
            "30d": today - timedelta(days=29)}[range]


@router.get("/gyms/{gymId}/checkins/denied",
            summary="People who tried to check in but were turned away (expired plan etc.), one row per member",
            description="`range`: today · week (last 7 days) · month (1st→today) · 30d. `kind`: plan (default — expired, "
                        "cancelled, frozen, suspended, not started) or all (every refusal except ‘already checked in’). "
                        "Each row: attempts, what they hit, first/last attempt, the plan that ran out and how many days "
                        "ago, their status now (`renewed` = they have a live plan now) and dues (payments.view).")
def denied_members(range: Literal["today", "week", "month", "30d"] = "week", kind: Literal["plan", "all"] = "plan",
                   ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    today = today_in(gym.timezone)
    start = _range_start(range, today)
    res = Checkin.result.in_(PLAN_DENIALS) if kind == "plan" else Checkin.result.notin_(("success", "duplicate"))
    rows = db.scalars(select(Checkin).where(Checkin.gym_id == gym.id, Checkin.local_date >= start,
                                            Checkin.local_date <= today, res).order_by(Checkin.at)).all()
    unknown = sum(1 for r in rows if not r.member_id)
    by_member: dict[str, list] = {}
    for r in rows:
        if r.member_id:
            by_member.setdefault(r.member_id, []).append(r)
    ids = list(by_member)
    members = {m.id: m for m in db.scalars(select(GymMember).where(GymMember.id.in_(ids or [""])))}
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in members.values()] or [""])))}
    mss = {m.id: m for m in db.scalars(select(Membership).where(
        Membership.id.in_([r.membership_id for r in rows if r.membership_id] or [""])))}
    states = load_states(db, gym, [i for i in ids if i in members])
    got_in = dict(db.execute(select(Checkin.member_id, func.count()).where(
        Checkin.gym_id == gym.id, Checkin.local_date >= start, Checkin.result == "success",
        Checkin.member_id.in_(ids or [""])).group_by(Checkin.member_id)).all())
    out = []
    for mid, rs in by_member.items():
        m = members.get(mid)
        if not m:
            continue
        u = users.get(m.user_id)
        st = states.get(mid)
        last = rs[-1]
        ms = mss.get(last.membership_id) or (st.current if st else None)
        it = {"member": {"id": m.id, "name": m.display_name, "member_code": m.member_code, "phone": u.phone if u else None,
                         "photo_url": storage.public_url(u.photo_url) if u else None},
              "attempts": len(rs), "days": len({r.local_date for r in rs}),
              "results": dict(Counter(r.result for r in rs)), "last_result": last.result,
              "first_at": iso(rs[0].at), "last_at": iso(last.at),
              "methods": sorted({r.method for r in rs}),
              "plan_name": ms.plan_name if ms else None, "plan_end_date": ms.end_date.isoformat() if ms else None,
              "days_since_expiry": (today - ms.end_date).days if ms and ms.end_date < today else None,
              "status_now": st.status if st else None, "renewed": bool(st and st.status in LIVE),
              "successful_checkins_in_range": int(got_in.get(mid, 0))}
        if ctx.can_pay and st:
            it["due_paise"] = st.due_paise
        out.append(it)
    # still not renewed first, newest attempt first within each group
    out = sorted([x for x in out if not x["renewed"]], key=lambda x: x["last_at"], reverse=True) + \
        sorted([x for x in out if x["renewed"]], key=lambda x: x["last_at"], reverse=True)
    totals = {"members": len(out), "attempts": sum(x["attempts"] for x in out),
              "not_renewed": sum(1 for x in out if not x["renewed"]), "renewed": sum(1 for x in out if x["renewed"]),
              "unknown_attempts": unknown,
              "by_result": dict(Counter(r.result for r in rows))}
    if ctx.can_pay:
        totals["due_paise"] = sum(x.get("due_paise", 0) for x in out if not x["renewed"])
    return {"range": range, "kind": kind, "from": start.isoformat(), "to": today.isoformat(), "items": out, "totals": totals}


@router.get("/gyms/{gymId}/checkins/stats", response_model=CheckinStatsOut,
            summary="Check-in stats: totals, unique members, by result, by hour, by day, peak hour",
            description="`range`: today · week (Mon→today) · month (1st→today). `by_day` has one entry per day in the range.")
def stats(range: Literal["today", "week", "month"] = "today", ctx: GymCtx = Depends(gym_ctx),
          db: Session = Depends(get_db)):
    gym = ctx.gym
    today = today_in(gym.timezone)
    start = {"today": today, "week": today - timedelta(days=today.weekday()), "month": today.replace(day=1)}[range]
    rows = db.execute(select(Checkin.at, Checkin.local_date, Checkin.member_id, Checkin.result).where(
        Checkin.gym_id == gym.id, Checkin.local_date >= start, Checkin.local_date <= today)).all()
    ok = [r for r in rows if r.result == "success"]
    z = tz(gym.timezone)
    by_hour = [0] * 24
    for r in ok:
        by_hour[r.at.astimezone(z).hour] += 1
    per_day = Counter(r.local_date for r in ok)
    by_day, d = [], start
    while d <= today:
        by_day.append({"date": d.isoformat(), "count": per_day.get(d, 0)})
        d += timedelta(days=1)
    peak = max(range_ := list(enumerate(by_hour)), key=lambda x: x[1])[0] if ok else None
    by_result = {k: 0 for k in ("success", "duplicate", "expired", "frozen", "suspended", "cancelled", "not_started",
                                "not_member", "invalid_token", "gym_inactive", "too_far", "location_required")}
    for r in rows:
        by_result[r.result] = by_result.get(r.result, 0) + 1
    return {"range": range, "from": start.isoformat(), "to": today.isoformat(), "total": len(ok),
            "unique_members": len({r.member_id for r in ok}), "denied": len(rows) - len(ok), "by_result": by_result,
            "by_hour": by_hour, "by_day": by_day, "peak_hour": peak}


# ---------------- gym QR (member scans the gym) ----------------
def _ensure_secret(db: Session, gym: Gym) -> str:
    if not gym.checkin_secret:
        gym.checkin_secret = secrets.token_urlsafe(24)
        db.commit()
    return gym.checkin_secret


def gym_qr_payload(db: Session, gym: Gym) -> dict:
    secret = _ensure_secret(db, gym)
    s = gym_settings(gym)
    url = f"{settings.checkin_url_base}?g={gym.id}&k={gym_static_sig(gym.id, secret)}"
    # One standard QR per branch: the same code on screen and on paper. It never changes by itself —
    # only when the owner presses "Reset QR" (POST /checkin-qr/rotate) because a copy leaked.
    return {"gym": {"id": gym.id, "name": gym.name, "status": gym.status},
            "qr": {"url": url, "standard": True},
            "display": {"url": url, "token": None, "expires_at": None, "refresh_in_sec": None},
            "poster": {"url": url, "enabled": True},
            "settings": {"require_location": bool(s.get("checkin_require_location", True)),
                         "radius_m": int(s.get("checkin_radius_m", 150)),
                         "static_qr": True},
            "location": {"set": gym.lat is not None and gym.lng is not None,
                         "lat": float(gym.lat) if gym.lat is not None else None,
                         "lng": float(gym.lng) if gym.lng is not None else None}}


@router.get("/gyms/{gymId}/checkin-qr", response_model=GymQrOut,
            summary="This branch’s standard check-in QR (checkins.manage)",
            description="`qr.url` is this branch’s one fixed check-in QR — print it or show it on a screen; it does not "
                        "change over time (`display.url` and `poster.url` are the same URL, kept for older apps). "
                        "It’s protected by the location check and only changes when the owner calls "
                        "POST /checkin-qr/rotate. Scanning it with a phone camera opens dumbbel.fit/checkin.")
def get_gym_qr(ctx: GymCtx = Depends(perm_ctx("checkins.manage")), db: Session = Depends(get_db)):
    return gym_qr_payload(db, ctx.gym)


@router.post("/gyms/{gymId}/checkin-qr/rotate", response_model=GymQrOut,
             summary="Reset this branch’s QR — old prints stop working (settings.manage)")
def rotate_gym_qr(ctx: GymCtx = Depends(perm_ctx("settings.manage")), db: Session = Depends(get_db)):
    ctx.gym.checkin_secret = secrets.token_urlsafe(24)
    audit(db, ctx.gym.id, ctx.user.id, "checkin.qr_rotate", "gym", ctx.gym.id, "Replaced the printed check-in QR")
    db.commit()
    return gym_qr_payload(db, ctx.gym)


class SelfCheckinIn(BaseModel):
    code: str = Field(..., description="The scanned gym QR: the full https://dumbbel.fit/checkin?... URL (or its query)")
    member_id: str | None = Field(None, description="Optional; found automatically from the gym in the QR")
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0)
    device_id: str | None = None
    as_member: bool = Field(False, description="Staff of this gym who came to work out: check in as a member, not a shift")


def parse_gym_code(code: str) -> tuple[str | None, str | None, str | None]:
    """-> (gym_id, display_token, poster_sig) from a scanned URL / query string / bare token."""
    code = (code or "").strip()
    if code.startswith("DBG2."):
        data = parse_gym_display_token(code)
        return (data["g"] if data else None), code, None
    q = parse_qs(urlparse(code).query if "://" in code else code.lstrip("?"))
    return (q.get("g") or [None])[0], (q.get("t") or [None])[0], (q.get("k") or [None])[0]


@me_router.post("/checkin", response_model=CheckinResultOut,
                summary="Check in by scanning the gym’s QR (member app)",
                description="Send the scanned code and the phone’s location. Always 200 when processed — `result` is "
                            "success | duplicate | expired | frozen | suspended | cancelled | not_started | not_member | "
                            "invalid_token (old/forged QR) | too_far | location_required | gym_inactive. "
                            "404 INVALID_QR only when the code isn’t a DUMBBEL gym QR at all.")
def self_checkin(body: SelfCheckinIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"selfcheckin:{user.id}", 12, 60)
    gym_id, token, sig = parse_gym_code(body.code)
    gym = db.get(Gym, gym_id) if gym_id else None
    if not gym:
        raise ApiError(404, "INVALID_QR", "This isn’t a DUMBBEL gym check-in QR.")
    ok = False
    if token:
        data = parse_gym_display_token(token)
        ok = bool(data and data["g"] == gym.id)
    elif sig and gym.checkin_secret:
        ok = hmac.compare_digest(gym_static_sig(gym.id, gym.checkin_secret), sig)
    # Staff of this gym scanning its QR: that's their attendance (start / end of shift), however they signed in.
    staff = db.scalar(select(GymStaff).where(GymStaff.gym_id == gym.id, GymStaff.user_id == user.id,
                                             GymStaff.status.in_(("invited", "active"))))
    if staff and not body.as_member:
        from app.routers.staff_work import do_staff_scan
        base = {"checkin_id": "", "at": iso(utcnow()), "method": "staff", "gym": {"id": gym.id, "name": gym.name},
                "member": None, "can_check_in_as_member": bool(db.scalar(select(GymMember.id).where(
                    GymMember.gym_id == gym.id, GymMember.user_id == user.id, GymMember.status == "active")))}
        try:
            r = do_staff_scan(db, gym, staff, user, body.code, body.lat, body.lng, body.accuracy_m)
        except ApiError as e:
            db.rollback()
            return {**base, "result": "staff_not_marked", "message": e.message, "staff_error": e.code}
        return {**base, "result": "staff_shift", "message": "Shift started" if r["action"] == "in" else "Shift ended",
                "staff_shift": {"action": r["action"], "shift": r["shift"]}}
    member = db.scalar(select(GymMember).where(GymMember.gym_id == gym.id, GymMember.user_id == user.id))
    if body.member_id and member and member.id != body.member_id:
        raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
    ctx = SimpleNamespace(gym=gym, user=user, can_pay=False)
    loc = {"lat": body.lat, "lng": body.lng, "accuracy_m": body.accuracy_m} if body.lat is not None else None
    out = process_checkin(db, ctx, "self", member_id=member.id if member else None, device_id=body.device_id,
                          code_ok=ok, location=loc)
    if member and out["result"] == "success":
        st = member_state(db, gym, member.id)
        out["due_paise"] = st.due_paise  # members always see their own dues
    db.commit()
    return out
