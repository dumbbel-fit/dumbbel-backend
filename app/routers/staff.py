"""§12 — staff management, home summary & insights, reminders, activity log."""
from collections import Counter
from datetime import timedelta
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, gym_ctx, owner_ctx
from app.errors import ApiError
from app.models import AuditLog, Checkin, GymMember, GymStaff, Invite, Payment, Plan, Session as DbSession, User
from app.schemas import StaffCreate, StaffPatch
from app.services.audit import audit
from app.services.core import get_or_create_user
from app.services.notify import queue_invite
from app.services.rules import LIVE, days_left, hour_label, load_states
from app.utils import (clamp_limit, decode_cursor, encode_cursor, first_name, iso, local_midnight_utc,
                       normalize_phone, rupees, today_in, tz, utcnow)

router = APIRouter(tags=["Staff, insights & activity"])


# ---------------- staff ----------------
def ser_staff(st: GymStaff, u: User, last_action: AuditLog | None = None) -> dict:
    return {"id": st.id, "user": {"id": u.id, "name": u.name, "phone": u.phone}, "role": st.role,
            "can_record_payments": st.role == "owner" or st.can_record_payments, "status": st.status,
            "invited_at": iso(st.invited_at), "joined_at": iso(st.joined_at), "deactivated_at": iso(st.deactivated_at),
            "last_active_at": iso(max([t for t in (u.last_login_at, last_action.at if last_action else None) if t],
                                      default=None)),
            "last_action": {"at": iso(last_action.at), "summary": last_action.summary} if last_action else None}


@router.get("/gyms/{gymId}/staff", summary="List staff (owner)")
def list_staff(ctx: GymCtx = Depends(owner_ctx), db: Session = Depends(get_db)):
    rows = db.execute(select(GymStaff, User).join(User, User.id == GymStaff.user_id)
                      .where(GymStaff.gym_id == ctx.gym.id).order_by(GymStaff.role, GymStaff.created_at)).all()
    last_ids = dict(db.execute(select(AuditLog.actor_user_id, func.max(AuditLog.id)).where(
        AuditLog.gym_id == ctx.gym.id, AuditLog.actor_user_id.in_([u.id for _, u in rows] or [""]))
        .group_by(AuditLog.actor_user_id)).all())
    logs = {a.actor_user_id: a for a in db.scalars(select(AuditLog).where(AuditLog.id.in_(list(last_ids.values()) or [0])))}
    return {"items": [ser_staff(st, u, logs.get(u.id)) for st, u in rows]}


@router.post("/gyms/{gymId}/staff", status_code=201, summary="Invite staff (owner). Sends SMS",
             description="409 ALREADY_STAFF if already invited/active here.")
def add_staff(body: StaffCreate, ctx: GymCtx = Depends(owner_ctx), db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    user, _ = get_or_create_user(db, phone, body.name.strip())
    st = db.scalar(select(GymStaff).where(GymStaff.gym_id == ctx.gym.id, GymStaff.user_id == user.id))
    now = utcnow()
    if st and st.status != "deactivated":
        raise ApiError(409, "ALREADY_STAFF")
    if st:
        st.status, st.can_record_payments, st.invited_by, st.invited_at, st.deactivated_at = \
            "invited", body.can_record_payments, ctx.user.id, now, None
    else:
        st = GymStaff(gym_id=ctx.gym.id, user_id=user.id, role="staff", can_record_payments=body.can_record_payments,
                      status="invited", invited_by=ctx.user.id, invited_at=now)
        db.add(st)
    db.flush()
    inv = Invite(gym_id=ctx.gym.id, staff_id=st.id, kind="staff", phone=phone, status="queued")
    db.add(inv)
    db.flush()
    audit(db, ctx.gym.id, ctx.user.id, "staff.invite", "staff", st.id,
          f"Invited {user.name or phone} as staff" + (" with payments access" if body.can_record_payments else ""))
    db.commit()
    queue_invite(inv.id)
    return ser_staff(st, user)


def _get_staff(db: Session, ctx: GymCtx, staff_id: str) -> tuple[GymStaff, User]:
    st = db.get(GymStaff, staff_id)
    if not st or st.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That staff member wasn’t found.")
    if st.role == "owner":
        raise ApiError(403, "FORBIDDEN", "The owner can’t be changed here.")
    return st, db.get(User, st.user_id)


def _revoke_admin_sessions(db: Session, user_id: str):
    still_staff = db.scalar(select(GymStaff.id).where(GymStaff.user_id == user_id,
                                                      GymStaff.status.in_(("invited", "active"))).limit(1))
    if not still_staff:
        db.execute(update(DbSession).where(DbSession.user_id == user_id, DbSession.app.in_(("admin", "web")),
                                           DbSession.revoked_at.is_(None)).values(revoked_at=utcnow()))


@router.patch("/gyms/{gymId}/staff/{staffId}", summary="Change payments access / (de)activate (owner)")
def patch_staff(staffId: str, body: StaffPatch, ctx: GymCtx = Depends(owner_ctx), db: Session = Depends(get_db)):
    st, u = _get_staff(db, ctx, staffId)
    before = {"can_record_payments": st.can_record_payments, "status": st.status}
    if body.can_record_payments is not None:
        st.can_record_payments = body.can_record_payments
    if body.status == "deactivated" and st.status != "deactivated":
        st.status, st.deactivated_at = "deactivated", utcnow()
        db.flush()
        _revoke_admin_sessions(db, u.id)
    elif body.status == "active" and st.status == "deactivated":
        st.status, st.deactivated_at = ("active" if st.joined_at else "invited"), None
    audit(db, ctx.gym.id, ctx.user.id, "staff.update", "staff", st.id, f"Updated {u.name or u.phone}’s access",
          before, {"can_record_payments": st.can_record_payments, "status": st.status})
    db.commit()
    return ser_staff(st, u)


@router.delete("/gyms/{gymId}/staff/{staffId}", status_code=204, summary="Remove staff (deactivate + revoke sessions)")
def remove_staff(staffId: str, ctx: GymCtx = Depends(owner_ctx), db: Session = Depends(get_db)):
    st, u = _get_staff(db, ctx, staffId)
    st.status, st.deactivated_at = "deactivated", utcnow()
    db.flush()
    _revoke_admin_sessions(db, u.id)
    audit(db, ctx.gym.id, ctx.user.id, "staff.remove", "staff", st.id, f"Removed {u.name or u.phone} from staff")
    db.commit()
    return Response(status_code=204)


# ---------------- summary & insights ----------------
def _visits_last(db: Session, gym_id: str, since, member_ids=None) -> Counter:
    q = select(Checkin.member_id, func.count(func.distinct(Checkin.local_date))).where(
        Checkin.gym_id == gym_id, Checkin.result == "success", Checkin.local_date >= since)
    if member_ids is not None:
        q = q.where(Checkin.member_id.in_(member_ids or [""]))
    return Counter(dict(db.execute(q.group_by(Checkin.member_id)).all()))


def build_insights(db: Session, ctx: GymCtx) -> dict:
    gym = ctx.gym
    today = today_in(gym.timezone)
    rows = db.execute(select(GymMember, User).join(User, User.id == GymMember.user_id)
                      .where(GymMember.gym_id == gym.id, GymMember.status == "active")).all()
    states = load_states(db, gym, [m.id for m, _ in rows])
    away_cut = local_midnight_utc(today - timedelta(days=14), gym.timezone)
    live = [(m, u, states[m.id]) for m, u in rows if states[m.id].status in LIVE]

    renew = [(m, u, s) for m, u, s in live if s.current and today <= s.current.end_date <= today + timedelta(days=7)]
    v7 = _visits_last(db, gym.id, today - timedelta(days=6), [m.id for m, _, _ in renew])
    likely = sum(1 for m, _, _ in renew if v7.get(m.id, 0) >= 3)
    plan_price = {p.id: p.price_paise for p in db.scalars(select(Plan).where(Plan.gym_id == gym.id))}
    renew_value = sum(plan_price.get(s.current.plan_id, s.current.price_paise + s.current.discount_paise)
                      for _, _, s in renew)

    dues = [(m, u, states[m.id]) for m, u in rows if states[m.id].due_paise > 0]
    oldest = 0
    for _, _, s in dues:
        for ms, _, d in s.memberships:
            if d > 0:
                oldest = max(oldest, (today - ms.start_date).days)
    away = [(m, u, s) for m, u, s in live if m.last_checkin_at is None or m.last_checkin_at < away_cut]
    not_on_app = [(m, u, s) for m, u, s in live if u.first_login_at is None]

    # peak hour: same weekday, last 4 weeks
    days = [today - timedelta(days=7 * i) for i in range(1, 5)]
    ats = db.scalars(select(Checkin.at).where(Checkin.gym_id == gym.id, Checkin.result == "success",
                                              Checkin.local_date.in_(days))).all()
    z = tz(gym.timezone)
    hours = Counter(a.astimezone(z).hour for a in ats)
    insights = []
    if renew:
        it = {"key": "renewals_due", "title": f"{len(renew)} renewal{'s' if len(renew) != 1 else ''} due this week",
              "detail": f"{likely} likely to renew", "count": len(renew),
              "rule": "Memberships ending in the next 7 days. “Likely” = 3+ visits in the last 7 days.",
              "action": {"type": "open_members", "filter": "expiring"}}
        if ctx.can_pay:
            it["value_paise"] = renew_value
            it["detail"] += f" · {rupees(renew_value)} at stake"
        insights.append(it)
    if ctx.can_pay and dues:
        insights.append({"key": "dues", "title": f"{len(dues)} member{'s' if len(dues) != 1 else ''} with dues",
                         "detail": f"{rupees(sum(s.due_paise for *_, s in dues))} pending · oldest {oldest} days",
                         "count": len(dues), "value_paise": sum(s.due_paise for *_, s in dues),
                         "rule": "Plan price minus payments recorded against it. Oldest = days since that membership started.",
                         "action": {"type": "open_members", "filter": "dues"}})
    if away:
        insights.append({"key": "away", "title": f"{len(away)} active member{'s' if len(away) != 1 else ''} haven’t come in 2 weeks",
                         "detail": "A friendly nudge often brings them back", "count": len(away),
                         "rule": "Active membership and no check-in in the last 14 days.",
                         "action": {"type": "open_members", "filter": "away"}})
    if not_on_app:
        insights.append({"key": "not_on_app", "title": f"{len(not_on_app)} member{'s' if len(not_on_app) != 1 else ''} not on the app",
                         "detail": "Invite them to get their QR", "count": len(not_on_app),
                         "rule": "Active members who have never logged in to the member app.",
                         "action": {"type": "send_invites", "filter": "not_on_app"}})
    if hours:
        h, n = hours.most_common(1)[0]
        insights.append({"key": "peak_hour", "title": f"Busiest around {hour_label(h)} on {today.strftime('%A')}s",
                         "detail": f"~{round(n / 4)} check-ins in that hour", "count": round(n / 4), "hour": h,
                         "rule": f"Busiest hour on the same weekday over the last 4 weeks.",
                         "action": {"type": "open_checkins"}})
    return {"rows": rows, "states": states, "live": live, "dues": dues, "insights": insights, "renew": renew,
            "away": away, "today": today}


@router.get("/gyms/{gymId}/summary", summary="Home numbers, “Needs attention” insights, expiring list",
            description="Money fields only with payments permission. Imported payments are excluded from collected_month_paise.")
def summary(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    data = build_insights(db, ctx)
    today = data["today"]
    checkins_today = db.scalar(select(func.count(func.distinct(Checkin.member_id))).where(
        Checkin.gym_id == gym.id, Checkin.local_date == today, Checkin.result == "success")) or 0
    numbers = {"active_members": len(data["live"]), "total_members": len(data["rows"]), "checkins_today": checkins_today}
    if ctx.can_pay:
        m_start = local_midnight_utc(today.replace(day=1), gym.timezone)
        numbers["collected_month_paise"] = int(db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
            Payment.gym_id == gym.id, Payment.status == "recorded", Payment.source != "import",
            Payment.paid_at >= m_start)) or 0)
        numbers["dues_paise"] = sum(s.due_paise for *_, s in data["dues"])
        numbers["dues_members"] = len(data["dues"])
    expiring = []
    for m, u in data["rows"]:
        s = data["states"][m.id]
        c = s.current
        if c and s.status != "cancelled" and today - timedelta(days=10) <= c.end_date <= today + timedelta(days=7):
            expiring.append({"member_id": m.id, "name": m.display_name, "phone": u.phone, "plan_name": c.plan_name,
                             "end_date": c.end_date.isoformat(), "days_left": days_left(c, s.status, today),
                             "status": s.status})
    expiring.sort(key=lambda x: x["end_date"])
    return {"date": today.isoformat(), "gym": {"id": gym.id, "name": gym.name, "status": gym.status},
            "numbers": numbers, "insights": data["insights"], "expiring": expiring[:20]}


TEMPLATES = {
    "expiring": "Hi {first}, your {plan} at {gym} ends on {end}. Renew at the front desk or reply here to keep your streak going! 💪",
    "expired": "Hi {first}, your {plan} at {gym} ended on {end}. We miss you — renew anytime at the front desk.",
    "dues": "Hi {first}, a quick reminder from {gym}: {due} is pending on your membership. You can pay by UPI or cash at the desk. Thanks!",
    "away": "Hi {first}, we haven’t seen you at {gym} in a while. Your membership is active till {end} — come say hi this week!",
}


@router.get("/gyms/{gymId}/reminders", summary="WhatsApp reminder links (server-side templates)")
def reminders(type: Literal["expiring", "dues", "away"] = "expiring", ctx: GymCtx = Depends(gym_ctx),
              db: Session = Depends(get_db)):
    if type == "dues":
        ctx.require_payments()
    gym = ctx.gym
    data = build_insights(db, ctx)
    today = data["today"]
    if type == "expiring":
        targets = []
        for m, u in data["rows"]:
            s = data["states"][m.id]
            if s.current and s.status != "cancelled" and today - timedelta(days=10) <= s.current.end_date <= today + timedelta(days=7):
                targets.append((m, u, s))
    elif type == "dues":
        targets = data["dues"]
    else:
        targets = data["away"]
    items = []
    for m, u, s in targets:
        c = s.current
        key = "expired" if type == "expiring" and c and c.end_date < today else type
        msg = TEMPLATES[key].format(first=first_name(m.display_name) or "there", gym=gym.name,
                                    plan=c.plan_name if c else "membership",
                                    end=c.end_date.strftime("%d %b") if c else "", due=rupees(s.due_paise))
        digits = u.phone.lstrip("+")
        it = {"member_id": m.id, "name": m.display_name, "phone": u.phone, "message": msg,
              "whatsapp_url": f"https://wa.me/{digits}?text={quote(msg)}"}
        if ctx.can_pay:
            it["due_paise"] = s.due_paise
        items.append(it)
    return {"type": type, "items": items}


@router.get("/gyms/{gymId}/activity", summary="Activity log (owner)")
def activity(actor_id: str | None = None, entity_type: str | None = None, limit: int = Query(50, ge=1, le=100),
             cursor: str | None = None, ctx: GymCtx = Depends(owner_ctx), db: Session = Depends(get_db)):
    q = select(AuditLog).where(AuditLog.gym_id == ctx.gym.id)
    if actor_id:
        q = q.where(AuditLog.actor_user_id == actor_id)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    c = decode_cursor(cursor)
    if c:
        q = q.where(AuditLog.id < int(c["id"]))
    lim = clamp_limit(limit)
    rows = db.scalars(q.order_by(AuditLog.id.desc()).limit(lim + 1)).all()
    nxt = encode_cursor({"id": rows[lim - 1].id}) if len(rows) > lim else None
    rows = rows[:lim]
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_([r.actor_user_id for r in rows] or [""])))}
    return {"items": [{"id": r.id, "at": iso(r.at), "actor": {"id": r.actor_user_id, "name": names.get(r.actor_user_id)},
                       "action": r.action, "summary": r.summary, "entity_type": r.entity_type, "entity_id": r.entity_id,
                       "before": r.before, "after": r.after} for r in rows], "next_cursor": nxt}
