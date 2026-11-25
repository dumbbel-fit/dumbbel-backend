"""§12 — staff management, home summary & insights, reminders, activity log."""
from collections import Counter
from datetime import timedelta
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, gym_ctx, owner_ctx, perm_ctx
from app.errors import ApiError, validation
from app.models import UpiPaymentRequest, AuditLog, Checkin, Gym, GymMember, GymStaff, Membership, MembershipFreeze, Payment, Plan, Session as DbSession, User
from app.schemas import StaffCreate, StaffPatch
from app.services.audit import audit
from app.services.storage import public_url
from app.services.core import get_or_create_user
from app.services.cache import rate_limit
from app.permissions import PERMISSIONS, ROLE_PRESETS, effective_permissions
from app.services.rules import LIVE, PLAN_DENIALS, days_left, hour_label, load_states
from app.utils import (clamp_limit, decode_cursor, encode_cursor, first_name, iso, local_midnight_utc,
                       normalize_phone, rupees, today_in, tz, utcnow)

from app.responses import DocRoute, ERRORS, ActivityOut, RemindersOut, RolesOut, StaffListOut, StaffOut, SummaryOut

router = APIRouter(tags=["Staff, insights & activity"], route_class=DocRoute, responses=ERRORS)


# ---------------- staff ----------------
def ser_staff(st: GymStaff, u: User, last_action: AuditLog | None = None, branches: list | None = None) -> dict:
    perms = sorted(effective_permissions(st.role, st.staff_role, bool(st.can_record_payments), st.permissions))
    d = {"id": st.id, "user": {"id": u.id, "name": u.name, "phone": u.phone}, "role": st.role,
         "staff_role": "owner" if st.role == "owner" else (st.staff_role or "staff"), "permissions": perms,
         "can_record_payments": "payments.record" in perms, "status": st.status,
         "logged_in": st.joined_at is not None or st.status == "active",
         "invited_at": iso(st.invited_at), "joined_at": iso(st.joined_at), "deactivated_at": iso(st.deactivated_at),
         "last_active_at": iso(max([t for t in (u.last_login_at, last_action.at if last_action else None) if t],
                                   default=None)),
         "last_action": {"at": iso(last_action.at), "summary": last_action.summary} if last_action else None}
    if branches is not None:
        d["branches"] = branches
    return d


def _validate_perms(perms: list[str] | None):
    if perms:
        bad = [p for p in perms if p not in PERMISSIONS]
        if bad:
            raise validation({"permissions": f"Unknown permission(s): {', '.join(bad)}"})


@router.get("/gyms/{gymId}/roles", response_model=RolesOut, summary="Roles and what each can do")
def roles(ctx: GymCtx = Depends(gym_ctx)):
    return {"roles": {"owner": sorted(PERMISSIONS), **{k: sorted(v) for k, v in ROLE_PRESETS.items()}},
            "permissions": PERMISSIONS}


@router.get("/gyms/{gymId}/staff", response_model=StaffListOut, summary="List staff (staff.manage)")
def list_staff(ctx: GymCtx = Depends(perm_ctx("staff.manage")), db: Session = Depends(get_db)):
    rows = db.execute(select(GymStaff, User).join(User, User.id == GymStaff.user_id)
                      .where(GymStaff.gym_id == ctx.gym.id).order_by(GymStaff.role, GymStaff.created_at)).all()
    last_ids = dict(db.execute(select(AuditLog.actor_user_id, func.max(AuditLog.id)).where(
        AuditLog.gym_id == ctx.gym.id, AuditLog.actor_user_id.in_([u.id for _, u in rows] or [""]))
        .group_by(AuditLog.actor_user_id)).all())
    logs = {a.actor_user_id: a for a in db.scalars(select(AuditLog).where(AuditLog.id.in_(list(last_ids.values()) or [0])))}
    branches = {}
    if ctx.gym.org_id:
        for st2, g2 in db.execute(select(GymStaff, Gym).join(Gym, Gym.id == GymStaff.gym_id).where(
                Gym.org_id == ctx.gym.org_id, GymStaff.user_id.in_([u.id for _, u in rows] or [""]),
                GymStaff.status != "deactivated")).all():
            branches.setdefault(st2.user_id, []).append({"gym_id": g2.id, "name": g2.name})
    return {"items": [ser_staff(st, u, logs.get(u.id), branches.get(u.id)) for st, u in rows]}


@router.post("/gyms/{gymId}/staff", response_model=StaffOut, status_code=201,
             summary="Add staff with a role (staff.manage)",
             description="No SMS is sent. The person simply logs in to the Admin app with this phone number "
                         "(Firebase OTP) and is linked automatically; `status` goes `invited` → `active` on first login. "
                         "Roles: manager · receptionist · trainer · staff (see GET /roles). `permissions` overrides the "
                         "preset. `branch_ids` adds access to other branches of the same business. 409 ALREADY_STAFF.")
def add_staff(body: StaffCreate, ctx: GymCtx = Depends(perm_ctx("staff.manage")), db: Session = Depends(get_db)):
    _validate_perms(body.permissions)
    phone = normalize_phone(body.phone)
    user, _ = get_or_create_user(db, phone, body.name.strip())
    gym_ids = [ctx.gym.id]
    for bid in body.branch_ids or []:
        g2 = db.get(Gym, bid)
        if not g2 or not ctx.gym.org_id or g2.org_id != ctx.gym.org_id:
            raise validation({"branch_ids": "Only branches of this business can be added."})
        if bid not in gym_ids:
            gym_ids.append(bid)
    now = utcnow()
    main = None
    for gid in gym_ids:
        st = db.scalar(select(GymStaff).where(GymStaff.gym_id == gid, GymStaff.user_id == user.id))
        if st and st.status != "deactivated":
            if gid == ctx.gym.id:
                raise ApiError(409, "ALREADY_STAFF")
            continue
        if st:
            st.status, st.invited_by, st.invited_at, st.deactivated_at = "invited", ctx.user.id, now, None
        else:
            st = GymStaff(gym_id=gid, user_id=user.id, role="staff", status="invited", invited_by=ctx.user.id,
                          invited_at=now)
            db.add(st)
        st.staff_role, st.permissions, st.can_record_payments = body.staff_role, body.permissions, body.can_record_payments
        db.flush()
        if gid == ctx.gym.id:
            main = st
    audit(db, ctx.gym.id, ctx.user.id, "staff.add", "staff", main.id,
          f"Added {user.name or phone} as {body.staff_role}" + (f" (+{len(gym_ids) - 1} branches)" if len(gym_ids) > 1 else ""))
    db.commit()
    return ser_staff(main, user)


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


@router.patch("/gyms/{gymId}/staff/{staffId}", response_model=StaffOut,
              summary="Change role / permissions / payments access / (de)activate (staff.manage)")
def patch_staff(staffId: str, body: StaffPatch, ctx: GymCtx = Depends(perm_ctx("staff.manage")), db: Session = Depends(get_db)):
    st, u = _get_staff(db, ctx, staffId)
    _validate_perms(body.permissions)
    before = {"staff_role": st.staff_role, "permissions": st.permissions, "can_record_payments": st.can_record_payments,
              "status": st.status}
    data = body.model_dump(exclude_unset=True)
    if "staff_role" in data and data["staff_role"] != st.staff_role and "permissions" not in data:
        # changing the role applies that role's preset (old custom permissions would otherwise override it)
        st.permissions = None
        if "can_record_payments" not in data:
            st.can_record_payments = False
    for k in ("staff_role", "permissions", "can_record_payments"):
        if k in data:
            setattr(st, k, data[k])
    if data.get("phone"):
        new_phone = normalize_phone(body.phone)
        if new_phone != u.phone:
            if u.first_login_at is not None:
                raise ApiError(409, "PHONE_LOCKED_AFTER_LOGIN",
                               "This person has already logged in, so their number can’t be changed. Remove them and add the new number.")
            other = db.scalar(select(User).where(User.phone == new_phone))
            if other:
                if db.scalar(select(GymStaff.id).where(GymStaff.gym_id == ctx.gym.id, GymStaff.user_id == other.id)):
                    raise ApiError(409, "ALREADY_STAFF")
                st.user_id = other.id
            else:
                shared = db.scalar(select(GymMember.id).where(GymMember.user_id == u.id).limit(1)) \
                    or db.scalar(select(func.count()).select_from(GymStaff).where(GymStaff.user_id == u.id)) > 1
                if shared:
                    nu = User(phone=new_phone, name=u.name)
                    db.add(nu)
                    db.flush()
                    st.user_id = nu.id
                else:
                    u.phone = new_phone
            db.flush()
            u = db.get(User, st.user_id)
    if data.get("name"):
        u.name = data["name"].strip()
    if body.status == "deactivated" and st.status != "deactivated":
        st.status, st.deactivated_at = "deactivated", utcnow()
        db.flush()
        _revoke_admin_sessions(db, u.id)
    elif body.status == "active" and st.status == "deactivated":
        st.status, st.deactivated_at = ("active" if st.joined_at else "invited"), None
    audit(db, ctx.gym.id, ctx.user.id, "staff.update", "staff", st.id, f"Updated {u.name or u.phone}’s access", before,
          {"staff_role": st.staff_role, "permissions": st.permissions, "can_record_payments": st.can_record_payments,
           "status": st.status})
    db.commit()
    return ser_staff(st, u)


@router.delete("/gyms/{gymId}/staff/{staffId}", status_code=204, summary="Remove staff (deactivate + revoke sessions)")
def remove_staff(staffId: str, ctx: GymCtx = Depends(perm_ctx("staff.manage")), db: Session = Depends(get_db)):
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
                         "detail": "They get their QR by logging in to the member app with their number",
                         "count": len(not_on_app),
                         "rule": "Active members who have never logged in to the member app.",
                         "action": {"type": "open_members", "filter": "not_on_app"}})
    # people who tried to check in but their plan had run out (or was frozen / cancelled) — last 7 days
    tried = db.execute(select(Checkin.member_id, func.count()).where(
        Checkin.gym_id == gym.id, Checkin.local_date >= today - timedelta(days=6), Checkin.result.in_(PLAN_DENIALS),
        Checkin.member_id.isnot(None)).group_by(Checkin.member_id)).all()
    tried_ids = [mid for mid, _ in tried]
    tried_states = load_states(db, gym, tried_ids) if tried_ids else {}
    turned_away = [mid for mid in tried_ids if mid in tried_states and tried_states[mid].status not in LIVE]
    if turned_away:
        k = len(turned_away)
        insights.insert(0, {"key": "expired_checkins",
                            "title": f"{k} {'person' if k == 1 else 'people'} tried to check in on an expired plan",
                            "detail": "In the last 7 days, and still haven’t renewed", "count": k,
                            "rule": "A check-in was refused in the last 7 days because the plan had expired, was "
                                    "cancelled, frozen, suspended or not started — and they still have no live plan.",
                            "action": {"type": "open_denied_checkins", "range": "week"}})
    if hours:
        h, n = hours.most_common(1)[0]
        insights.append({"key": "peak_hour", "title": f"Busiest around {hour_label(h)} on {today.strftime('%A')}s",
                         "detail": f"~{round(n / 4)} check-ins in that hour", "count": round(n / 4), "hour": h,
                         "rule": f"Busiest hour on the same weekday over the last 4 weeks.",
                         "action": {"type": "open_checkins"}})
    return {"rows": rows, "states": states, "live": live, "dues": dues, "insights": insights, "renew": renew,
            "away": away, "today": today, "turned_away_7d": turned_away}


INSIDE_WINDOW_MIN = 90


def _sum_paid(db: Session, gym_id: str, start, end=None, exclude_import: bool = False) -> int:
    q = select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
        Payment.gym_id == gym_id, Payment.status == "recorded", Payment.paid_at >= start)
    if end is not None:
        q = q.where(Payment.paid_at < end)
    if exclude_import:
        q = q.where(Payment.source != "import")
    return int(db.scalar(q) or 0)


@router.get("/gyms/{gymId}/summary", response_model=SummaryOut,
            summary="Home: numbers, “Needs attention” insights, expiring list",
            description="Money fields (`*_paise`, dues) only with payments.view. "
                        "`collected_month_paise` includes imported payments; `collected_month_excl_import_paise` doesn’t. "
                        f"`inside_now` = members with a successful check-in in the last {INSIDE_WINDOW_MIN} min.")
def summary(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    data = build_insights(db, ctx)
    today = data["today"]
    now = utcnow()
    states = data["states"]
    ok_today = [Checkin.gym_id == gym.id, Checkin.local_date == today, Checkin.result == "success"]
    checkins_today = db.scalar(select(func.count(func.distinct(Checkin.member_id))).where(*ok_today)) or 0
    visits_today = db.scalar(select(func.count()).select_from(Checkin).where(*ok_today)) or 0
    plan_denied_today = db.scalar(select(func.count(func.distinct(Checkin.member_id))).where(
        Checkin.gym_id == gym.id, Checkin.local_date == today, Checkin.result.in_(PLAN_DENIALS))) or 0
    first_in = db.scalar(select(Checkin).where(*ok_today).order_by(Checkin.at).limit(1))
    inside_now = db.scalar(select(func.count(func.distinct(Checkin.member_id))).where(
        Checkin.gym_id == gym.id, Checkin.result == "success",
        Checkin.at >= now - timedelta(minutes=INSIDE_WINDOW_MIN))) or 0
    m_first = today.replace(day=1)
    status_counts = Counter(states[m.id].status for m, _ in data["rows"])
    expiring_7d = sum(1 for m, _ in data["rows"] if states[m.id].current and states[m.id].status in LIVE
                      and (states[m.id].current.end_date - today).days <= 7)
    month_start_utc = local_midnight_utc(m_first, gym.timezone)
    new_members_month = db.scalar(select(func.count()).select_from(GymMember).where(
        GymMember.gym_id == gym.id, GymMember.created_at >= month_start_utc, GymMember.source != "import")) or 0
    month_ms = db.execute(select(Membership.member_id, Membership.created_at).where(
        Membership.gym_id == gym.id, Membership.created_at >= month_start_utc, Membership.source == "manual",
        Membership.state != "cancelled")).all()
    renewals_month = 0
    for mid, created in month_ms:
        if db.scalar(select(Membership.id).where(Membership.member_id == mid, Membership.created_at < created,
                                                 Membership.state != "cancelled").limit(1)):
            renewals_month += 1
    numbers = {"active_members": len(data["live"]), "total_members": len(data["rows"]),
               "checkins_today": checkins_today, "visits_today": visits_today, "inside_now": inside_now,
               "inside_now_window_min": INSIDE_WINDOW_MIN,
               "new_members_month": new_members_month, "renewals_month": renewals_month,
               "expiring_7d": expiring_7d, "expired_count": status_counts.get("expired", 0),
               "frozen_count": status_counts.get("frozen", 0), "suspended_count": status_counts.get("suspended", 0),
               "upcoming_count": status_counts.get("upcoming", 0), "no_plan_count": status_counts.get("no_plan", 0),
               "dues_count": len(data["dues"]), "dues_members": len(data["dues"]),
               "plan_denied_today": plan_denied_today, "expired_tried_7d": len(data["turned_away_7d"]),
               "pt_members": sum(1 for m, _ in data["rows"] if m.pt_enabled),
               "pt_active_members": sum(1 for m, _ in data["rows"] if m.pt_enabled and states[m.id].status in LIVE)}
    if ctx.can_pay:
        day_start = local_midnight_utc(today, gym.timezone)
        lw_start = local_midnight_utc(today - timedelta(days=7), gym.timezone)
        numbers.update({
            "collected_month_paise": _sum_paid(db, gym.id, month_start_utc),
            "collected_month_excl_import_paise": _sum_paid(db, gym.id, month_start_utc, exclude_import=True),
            "today_collected_paise": _sum_paid(db, gym.id, day_start),
            "last_week_same_day_paise": _sum_paid(db, gym.id, lw_start, lw_start + timedelta(days=1)),
            "last_week_same_time_paise": _sum_paid(db, gym.id, lw_start, now - timedelta(days=7)),
            "dues_paise": sum(s.due_paise for *_, s in data["dues"]),
            "pt_fees_paise": sum(m.pt_fee_paise or 0 for m, _ in data["rows"] if m.pt_enabled and states[m.id].status in LIVE),
            "upi_to_verify": db.scalar(select(func.count()).select_from(UpiPaymentRequest).where(
                UpiPaymentRequest.gym_id == gym.id, or_(UpiPaymentRequest.status == "claimed", and_(
                    UpiPaymentRequest.auto_confirmed.is_(True), UpiPaymentRequest.status == "verified",
                    UpiPaymentRequest.checked_at.is_(None))))) or 0,
        })
    expiring = []
    for m, u in data["rows"]:
        st = states[m.id]
        c = st.current
        if c and st.status != "cancelled" and today - timedelta(days=10) <= c.end_date <= today + timedelta(days=7):
            it = {"member_id": m.id, "name": m.display_name, "phone": u.phone, "member_code": m.member_code,
                  "plan_name": c.plan_name, "end_date": c.end_date.isoformat(), "days_left": days_left(c, st.status, today),
                  "status": st.status, "photo_url": public_url(u.photo_url)}
            if ctx.can_pay:
                it["due_paise"] = st.due_paise
            expiring.append(it)
    expiring.sort(key=lambda x: x["end_date"])
    first = None
    if first_in:
        fm = db.get(GymMember, first_in.member_id) if first_in.member_id else None
        first = {"at": iso(first_in.at), "member_id": fm.id if fm else None, "name": fm.display_name if fm else None}
    return {"date": today.isoformat(), "gym": {"id": gym.id, "name": gym.name, "status": gym.status},
            "numbers": numbers, "first_in_today": first, "insights": data["insights"], "expiring": expiring[:20]}


TEMPLATES = {
    "expiring": "Hi {first}, your {plan} at {gym} ends on {end}. Renew at the front desk or reply here to keep your streak going!",
    "expired": "Hi {first}, your {plan} at {gym} ended on {end}. We miss you — renew anytime at the front desk.",
    "dues": "Hi {first}, a quick reminder from {gym}: {due} is pending on your membership. You can pay by UPI or cash at the desk. Thanks!",
    "away": "Hi {first}, we haven’t seen you at {gym} in a while. Your membership is active till {end} — come say hi this week!",
}


@router.get("/gyms/{gymId}/reminders", response_model=RemindersOut, summary="WhatsApp reminder links (server-side templates)")
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


@router.get("/gyms/{gymId}/activity", response_model=ActivityOut, summary="Activity log (owner)")
def activity(actor_id: str | None = None, entity_type: str | None = None, limit: int = Query(50, ge=1, le=100),
             cursor: str | None = None, ctx: GymCtx = Depends(perm_ctx("activity.view")), db: Session = Depends(get_db)):
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


# ---------------- getting started (setup checklist on the Overview) ----------------
SETUP_FLAGS = ("qr_poster_at", "setup_dismissed_at")


@router.get("/gyms/{gymId}/setup", summary="Setup checklist: what this gym has done and what's next (owner)",
            description="Each step is worked out from the gym's own data, so it ticks itself as soon as it's done.")
def setup_checklist(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    from app.models import GymUpiAccount, GymWhatsApp, StaffShift
    gym = ctx.gym
    s = gym.settings or {}
    n = lambda q: db.scalar(q) or 0  # noqa: E731
    plans = n(select(func.count()).select_from(Plan).where(Plan.gym_id == gym.id, Plan.active.is_(True)))
    members = n(select(func.count()).select_from(GymMember).where(GymMember.gym_id == gym.id, GymMember.status == "active"))
    with_plan = n(select(func.count(func.distinct(Membership.member_id))).where(Membership.gym_id == gym.id, Membership.state != "cancelled"))
    self_scans = n(select(func.count()).select_from(Checkin).where(Checkin.gym_id == gym.id, Checkin.method == "self"))
    shifts = n(select(func.count()).select_from(StaffShift).where(StaffShift.gym_id == gym.id))
    staff = n(select(func.count()).select_from(GymStaff).where(GymStaff.gym_id == gym.id, GymStaff.role != "owner", GymStaff.status != "deactivated"))
    wa = db.get(GymWhatsApp, gym.id)
    upi = n(select(func.count()).select_from(GymUpiAccount).where(GymUpiAccount.gym_id == gym.id, GymUpiAccount.active.is_(True)))
    steps = [
        {"key": "plans", "title": "Add your membership plans", "why": "Monthly, Quarterly, Yearly… Members need a plan to check in.",
         "done": plans > 0, "count": plans, "go": "/admin/plans", "action": "Create a plan"},
        {"key": "members", "title": "Add or import your members", "why": "Upload your Excel sheet, or add people one by one (or a family together).",
         "done": members > 0, "count": members, "go": "/admin/members", "action": "Add members"},
        {"key": "memberships", "title": "Give members their plan", "why": "Assign a plan and record what they paid. Dues are worked out for you.",
         "done": with_plan > 0, "count": with_plan, "go": "/admin/members", "action": "Assign a plan"},
        {"key": "location", "title": "Set your gym’s location", "why": "Shows your gym on the map and how far staff were when they scanned. Members never need location to check in.",
         "done": gym.lat is not None and gym.lng is not None, "go": "/admin/checkin-qr?tab=settings", "action": "Set location", "optional": True},
        {"key": "qr", "title": "Print the check-in QR poster", "why": "Stick it at the entrance. Members scan it to check in; staff scan it for attendance.",
         "done": bool(s.get("qr_poster_at")) or self_scans > 0 or shifts > 0, "go": "/admin/checkin-qr", "action": "Get the poster"},
        {"key": "whatsapp", "title": "Link your WhatsApp", "why": "Welcome messages, expiry reminders and receipts go out by themselves.",
         "done": bool(wa and wa.status == "connected"), "go": "/admin/whatsapp", "action": "Link WhatsApp"},
        {"key": "staff", "title": "Add your staff", "why": "Front desk, trainers and managers each sign in with their own number.",
         "done": staff > 0, "count": staff, "go": "/admin/staff", "action": "Add staff", "optional": True},
        {"key": "upi", "title": "Add your UPI ID", "why": "Members can pay dues and renew by UPI straight to your account.",
         "done": upi > 0, "go": "/admin/upi", "action": "Add UPI ID", "optional": True},
    ]
    need = [x for x in steps if not x.get("optional")]
    return {"steps": steps, "done": sum(1 for x in steps if x["done"]), "total": len(steps),
            "required_done": sum(1 for x in need if x["done"]), "required_total": len(need),
            "complete": all(x["done"] for x in need), "dismissed": bool(s.get("setup_dismissed_at"))}


@router.post("/gyms/{gymId}/setup/{flag}", status_code=204, summary="Mark a setup step (poster printed) or hide the checklist")
def setup_flag(flag: Literal["qr_poster", "dismiss", "show"], ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    s = dict(ctx.gym.settings or {})
    if flag == "qr_poster":
        s["qr_poster_at"] = utcnow().isoformat()
    elif flag == "dismiss":
        s["setup_dismissed_at"] = utcnow().isoformat()
    else:
        s.pop("setup_dismissed_at", None)
    ctx.gym.settings = s
    db.commit()
    return Response(status_code=204)
