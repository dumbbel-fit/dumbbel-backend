"""§10 — QR scan, manual check-in, feed, stats. Validation order per spec §4."""
from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, Idem, gym_ctx, idem_header
from app.errors import ApiError
from app.models import Checkin, GymMember, Membership, User
from app.schemas import ManualCheckinIn, ScanIn
from app.security import parse_qr_token
from app.services import storage
from app.services.audit import audit
from app.services.cache import cache, rate_limit
from app.services.core import gym_settings, user_names
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.rules import days_left, member_state, streaks, visit_days
from app.utils import iso, local_date_of, local_midnight_utc, today_in, tz, utcnow

router = APIRouter(tags=["Check-ins & QR"])

MESSAGES = {
    "success": "Checked in. Have a great session!",
    "duplicate": "Already checked in a little while ago.",
    "expired": "Membership has expired. Renew to check in.",
    "frozen": "Membership is frozen right now.",
    "cancelled": "Membership was cancelled.",
    "not_started": "Membership hasn’t started yet.",
    "not_member": "Not a member of this gym.",
    "invalid_token": "This QR isn’t valid. Ask the member to refresh it.",
    "gym_inactive": "QR check-in unlocks once the gym is approved. Use manual check-in.",
}
STATUS_TO_RESULT = {"cancelled": "cancelled", "frozen": "frozen", "upcoming": "not_started",
                    "expired": "expired", "no_plan": "expired"}


def process_checkin(db: Session, ctx: GymCtx, method: str, *, token: str | None = None,
                    member_id: str | None = None, device_id: str | None = None, note: str | None = None) -> dict:
    gym = ctx.gym
    now = utcnow()
    local = local_date_of(now, gym.timezone)
    member = membership = None
    jti = None
    extra: dict = {}

    def finish(result: str) -> dict:
        c = Checkin(gym_id=gym.id, member_id=member.id if member else None,
                    membership_id=membership.id if membership else None, at=now, local_date=local, method=method,
                    result=result, staff_user_id=ctx.user.id, token_jti=jti, device_id=device_id, note=note)
        db.add(c)
        db.flush()
        out = {"result": result, "message": MESSAGES[result], "checkin_id": c.id, "at": iso(now), "method": method}
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
    enqueue("push.send", send_push, member.user_id, "member", "Checked in",
            f"Checked in at {gym.name} · day {cur}", {"type": "checkin_success", "gym_id": gym.id})
    return out


@router.post("/gyms/{gymId}/checkins/scan", summary="Scan member QR",
             description="Requires `Idempotency-Key`. Always 200 for a processed scan — `result` drives the screen: "
                         "success | duplicate | expired | frozen | cancelled | not_started | not_member | invalid_token | gym_inactive. "
                         "Rate limit 60/min per staff device.")
def scan(body: ScanIn, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db),
         idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/checkins/scan")
    if idem.replay:
        return idem.replay
    rate_limit(f"scan:{ctx.user.id}:{body.device_id or 'default'}", 60, 60)
    out = process_checkin(db, ctx, "qr", token=body.token, device_id=body.device_id)
    return idem.respond(200, out)


@router.post("/gyms/{gymId}/checkins/manual", summary="Manual check-in (fallback; works for pending gyms)",
             description="Requires `Idempotency-Key`. Same response shape as scan with method `manual`.")
def manual(body: ManualCheckinIn, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db),
           idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/checkins/manual")
    if idem.replay:
        return idem.replay
    out = process_checkin(db, ctx, "manual", member_id=body.member_id, note=body.note)
    name = (out.get("member") or {}).get("name", "unknown member")
    audit(db, ctx.gym.id, ctx.user.id, "checkin.manual", "checkin", out["checkin_id"],
          f"Manual check-in for {name}: {out['result']}")
    return idem.respond(200, out)


@router.get("/gyms/{gymId}/checkins", summary="Check-in feed (poll every 10 s with `since`)")
def feed(date: str | None = Query(None, examples=["2026-11-12"]), result: Literal["success", "denied"] | None = None,
         since: datetime | None = Query(None, description="Only items after this timestamp (polling)"),
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
    if since:
        conds.append(Checkin.at > (since if since.tzinfo else since.replace(tzinfo=tz("UTC"))))
    rows = db.scalars(select(Checkin).where(*conds).order_by(Checkin.at.desc()).limit(limit)).all()
    members = {m.id: m for m in db.scalars(select(GymMember).where(GymMember.id.in_([r.member_id for r in rows if r.member_id] or [""])))}
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in members.values()] or [""])))}
    plans = {m.id: m.plan_name for m in db.scalars(select(Membership).where(Membership.id.in_([r.membership_id for r in rows if r.membership_id] or [""])))}
    staff = user_names(db, [r.staff_user_id for r in rows])
    items = []
    for r in rows:
        m = members.get(r.member_id)
        u = users.get(m.user_id) if m else None
        items.append({"id": r.id, "at": iso(r.at), "method": r.method, "result": r.result,
                      "member": {"id": m.id, "name": m.display_name, "member_code": m.member_code,
                                 "photo_url": storage.public_url(u.photo_url) if u else None} if m else None,
                      "plan_name": plans.get(r.membership_id), "staff_name": staff.get(r.staff_user_id)})
    return {"date": d.isoformat(), "items": items,
            "counts": {"success": int(counts.get(1, 0) or counts.get(True, 0)),
                       "denied": int(counts.get(0, 0) or counts.get(False, 0))},
            "server_time": iso(utcnow())}


@router.get("/gyms/{gymId}/checkins/stats", summary="Check-in stats: totals, by hour, by day, peak hour")
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
    return {"range": range, "from": start.isoformat(), "to": today.isoformat(), "total": len(ok),
            "unique_members": len({r.member_id for r in ok}), "denied": len(rows) - len(ok),
            "by_hour": by_hour, "by_day": by_day, "peak_hour": peak}
