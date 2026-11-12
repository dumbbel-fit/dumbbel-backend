"""§8 — members & memberships (assign/renew, freeze, unfreeze, cancel)."""
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, Idem, current_user, gym_ctx, idem_header, load_gym_ctx
from app.errors import ApiError, validation
from app.models import (Checkin, GymMember, GymStaff, Invite, Membership, MembershipFreeze, Payment, Plan, User)
from app.schemas import CancelIn, FreezeIn, MemberCreate, MemberPatch, MembershipCreate
from app.services.audit import audit, diff
from app.services.cache import cache
from app.services.core import (add_payment, check_duplicate_reference, create_membership, default_start,
                               get_or_create_user, gym_settings, next_member_code, ser_member_basic,
                               ser_membership, ser_payment, user_names, validate_paid_at, validate_reference)
from app.services.rules import LIVE, freezes_of, load_states, member_state, membership_due, membership_status, \
    pick_current
from app.utils import (dstr, iso, local_midnight_utc, month_bounds, normalize_phone, paginate_list, rupees,
                       today_in, utcnow)

router = APIRouter(tags=["Members & memberships"])

StatusFilter = Literal["all", "active", "expiring", "expired", "frozen", "upcoming", "cancelled", "no_plan",
                       "dues", "away", "not_on_app", "archived"]


def get_member(db: Session, ctx: GymCtx, member_id: str) -> tuple[GymMember, User]:
    m = db.get(GymMember, member_id)
    if not m or m.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    return m, db.get(User, m.user_id)


# ---------------- list ----------------
@router.get("/gyms/{gymId}/members", summary="List / search members",
            description="`q` matches name, phone digits or member code. `status` tabs: active (=active+expiring), "
                        "expiring, expired, frozen, upcoming, cancelled, no_plan, dues*, away (live plan, no visit 14 days), "
                        "not_on_app, archived. `due_paise` and dues counts only with payments permission.")
def list_members(q: str | None = None, status: StatusFilter = "all",
                 sort: Literal["name", "expiry", "last_visit"] = "name",
                 limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                 ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    if status == "dues":
        ctx.require_payments()
    gym = ctx.gym
    today = today_in(gym.timezone)
    rows = db.execute(select(GymMember, User).join(User, User.id == GymMember.user_id)
                      .where(GymMember.gym_id == gym.id)).all()
    states = load_states(db, gym, [m.id for m, _ in rows])
    away_cut = local_midnight_utc(today - timedelta(days=14), gym.timezone)

    if q:
        ql = q.strip().lower()
        digits = "".join(c for c in ql if c.isdigit())
        rows = [(m, u) for m, u in rows if ql in (m.display_name or "").lower() or ql in m.member_code.lower()
                or (digits and len(digits) >= 3 and digits in u.phone)]

    enriched = []
    for m, u in rows:
        s = states[m.id]
        live = s.status in LIVE
        tags = {"archived"} if m.status == "archived" else {s.status, "all"}
        if m.status != "archived":
            if s.status in ("active", "expiring"):
                tags.add("active")
            if s.due_paise > 0:
                tags.add("dues")
            if live and (m.last_checkin_at is None or m.last_checkin_at < away_cut):
                tags.add("away")
            if live and u.first_login_at is None:
                tags.add("not_on_app")
        enriched.append((m, u, s, tags))

    keys = ["all", "active", "expiring", "expired", "frozen", "upcoming", "cancelled", "no_plan", "away",
            "not_on_app", "archived"] + (["dues"] if ctx.can_pay else [])
    counts = {k: sum(1 for *_, t in enriched if k in t) for k in keys}

    sel = [e for e in enriched if status in e[3]]
    if sort == "expiry":
        sel.sort(key=lambda e: (e[2].current is None, e[2].current.end_date if e[2].current else today, e[0].display_name.lower()))
    elif sort == "last_visit":
        sel.sort(key=lambda e: (e[0].last_checkin_at is None, -(e[0].last_checkin_at.timestamp()) if e[0].last_checkin_at else 0))
    else:
        sel.sort(key=lambda e: (e[0].display_name or "").lower())

    page, nxt = paginate_list(sel, limit, cursor)
    items = []
    for m, u, s, tags in page:
        it = {"id": m.id, "member_code": m.member_code, "name": m.display_name, "phone": u.phone,
              "photo_url": ser_member_basic(m, u)["photo_url"], "on_app": u.first_login_at is not None,
              "status": "archived" if m.status == "archived" else s.status,
              "last_checkin_at": iso(m.last_checkin_at), "current": s.current_dict(today),
              "has_dues": s.due_paise > 0}
        if ctx.can_pay:
            it["due_paise"] = s.due_paise
        items.append(it)
    return {"items": items, "next_cursor": nxt, "counts": counts}


# ---------------- create ----------------
@router.post("/gyms/{gymId}/members", status_code=201, summary="Add member (+ optional membership & payment)",
             description="Requires `Idempotency-Key`. 409 MEMBER_EXISTS if this phone is already a member here.")
def create_member(body: MemberCreate, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db),
                  idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/members")
    if idem.replay:
        return idem.replay
    if body.payment:
        ctx.require_payments()
    gym = ctx.gym
    phone = normalize_phone(body.phone)
    user, _ = get_or_create_user(db, phone, body.name)
    existing = db.scalar(select(GymMember).where(GymMember.gym_id == gym.id, GymMember.user_id == user.id))
    if existing:
        raise ApiError(409, "MEMBER_EXISTS", None, {"member_id": existing.id, "name": existing.display_name})
    if user.first_login_at is None:  # don't overwrite a member's own app profile
        for f in ("email", "dob", "gender"):
            v = getattr(body, f)
            if v is not None:
                setattr(user, f, v)
        if body.photo_key:
            user.photo_url = body.photo_key
    today = today_in(gym.timezone)
    member = GymMember(gym_id=gym.id, user_id=user.id, member_code=next_member_code(db, gym.id),
                       display_name=body.name.strip(), source="manual", emergency_contact=body.emergency_contact,
                       notes=body.notes, status="active", joined_on=today)
    db.add(member)
    db.flush()
    audit(db, gym.id, ctx.user.id, "member.create", "member", member.id, f"Added member {member.display_name}")

    ms = pay = None
    if body.membership:
        plan = db.get(Plan, body.membership.plan_id)
        if not plan or plan.gym_id != gym.id:
            raise validation({"membership.plan_id": "Pick a plan from this gym."})
        start = body.membership.start_date or today
        ms = create_membership(db, gym, ctx.user.id, member, plan=plan, start=start,
                               discount_paise=body.membership.discount_paise)
        audit(db, gym.id, ctx.user.id, "membership.assign", "membership", ms.id,
              f"Assigned {ms.plan_name} to {member.display_name} ({dstr(ms.start_date)} → {dstr(ms.end_date)})")
    if body.payment:
        p = body.payment
        ref = validate_reference(p.method, p.reference)
        check_duplicate_reference(db, gym.id, p.method, ref)
        if ms and p.amount_paise > ms.price_paise:
            raise ApiError(422, "OVERPAYMENT", None, {"due_paise": ms.price_paise})
        pay = add_payment(db, gym, ctx.user.id, member, ms, p.amount_paise, p.method, ref,
                          validate_paid_at(gym, p.paid_at))
        audit(db, gym.id, ctx.user.id, "payment.record", "payment", pay.id,
              f"Recorded {rupees(pay.amount_paise)} {pay.method.upper()} from {member.display_name}")
    body_out = {"member": ser_member_basic(member, user),
                "membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay,
                                             due=membership_due(db, ms)) if ms else None,
                "payment": ser_payment(pay, member.display_name, ms.plan_name if ms else None, ctx.user.name) if pay else None}
    return idem.respond(201, body_out)


# ---------------- detail ----------------
def gym_avg_rate_8w(db: Session, ctx: GymCtx) -> float:
    key = f"gymavg:{ctx.gym.id}"
    hit = cache.get(key)
    if hit is not None:
        return float(hit)
    today = today_in(ctx.gym.timezone)
    since = today - timedelta(days=56)
    states = load_states(db, ctx.gym)
    active_ids = [mid for mid, s in states.items() if s.status in LIVE]
    if not active_ids:
        val = 0.0
    else:
        visits = db.scalar(select(func.count()).select_from(
            select(Checkin.member_id, Checkin.local_date).where(
                Checkin.gym_id == ctx.gym.id, Checkin.result == "success", Checkin.local_date > since,
                Checkin.member_id.in_(active_ids)).distinct().subquery())) or 0
        val = round(visits / len(active_ids) / 8, 1)
    cache.set(key, str(val), ex=3600)
    return val


@router.get("/gyms/{gymId}/members/{memberId}", summary="Member detail: memberships, payments*, stats, insights, invites")
def member_detail(memberId: str, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    m, u = get_member(db, ctx, memberId)
    today = today_in(gym.timezone)
    st = member_state(db, gym, m.id)
    fz = {}
    for f in db.scalars(select(MembershipFreeze).where(MembershipFreeze.membership_id.in_(
            [x.id for x, _, _ in st.memberships] or [""]))):
        fz.setdefault(f.membership_id, []).append(f)
    memberships = [ser_membership(x, gym, status=s, due=d, can_pay=ctx.can_pay, freezes=fz.get(x.id, []))
                   for x, s, d in sorted(st.memberships, key=lambda t: t[0].start_date, reverse=True)]
    current = next((x for x in memberships if st.current and x["id"] == st.current.id), None)

    days = sorted(set(db.scalars(select(Checkin.local_date).where(Checkin.member_id == m.id,
                                                                  Checkin.result == "success").distinct())))
    v30 = sum(1 for d in days if d > today - timedelta(days=30))
    v8w = sum(1 for d in days if d > today - timedelta(days=56))
    rate = round(v8w / 8, 1)
    avg = gym_avg_rate_8w(db, ctx)
    insights = []
    if v30 >= 8:
        insights.append({"key": "renewal_likelihood", "value": "likely", "rule": f"{v30} check-ins in the last 30 days"})
    elif v30 < 4 and st.status in LIVE:
        insights.append({"key": "renewal_likelihood", "value": "might_not",
                         "rule": f"Only {v30} check-in{'s' if v30 != 1 else ''} in the last 30 days"})
    insights.append({"key": "visit_rate", "value": rate, "gym_avg": avg,
                     "rule": f"{rate}/week over the last 8 weeks vs gym average {avg}/week"})
    invites = db.scalars(select(Invite).where(Invite.member_id == m.id).order_by(Invite.created_at.desc())).all()
    if u.first_login_at:
        app_status = {"key": "app_status", "value": "on_app", "since": iso(u.first_login_at),
                      "rule": "Logged in to the member app"}
    elif invites:
        app_status = {"key": "app_status", "value": "invited", "since": iso(invites[0].sent_at or invites[0].created_at),
                      "rule": "Invite sent, not logged in yet"}
    else:
        app_status = {"key": "app_status", "value": "not_invited", "rule": "Never invited to the app"}
    insights.append(app_status)

    out = {"member": ser_member_basic(m, u), "user": {"on_app": u.first_login_at is not None,
                                                     "first_login_at": iso(u.first_login_at)},
           "current_membership": current, "memberships": memberships,
           "stats": {"visits_total": len(days), "visits_30d": v30, "visit_rate_8w": rate, "gym_avg_rate_8w": avg,
                     "first_visit": dstr(days[0]) if days else None, "last_visit": dstr(days[-1]) if days else None},
           "insights": insights,
           "invites": [{"id": i.id, "sent_at": iso(i.sent_at), "status": i.status, "created_at": iso(i.created_at)}
                       for i in invites]}
    if ctx.can_pay:
        pays = db.scalars(select(Payment).where(Payment.member_id == m.id).order_by(Payment.paid_at.desc())).all()
        names = user_names(db, [p.recorded_by for p in pays])
        plan_names = {x.id: x.plan_name for x, _, _ in st.memberships}
        out["payments"] = [ser_payment(p, m.display_name, plan_names.get(p.membership_id), names.get(p.recorded_by))
                           for p in pays]
        out["due_paise"] = st.due_paise
        out["credit_paise"] = st.credit_paise
    return out


# ---------------- patch ----------------
@router.patch("/gyms/{gymId}/members/{memberId}", summary="Edit member / archive",
              description="Phone change allowed only if the member never logged in (else 409 PHONE_LOCKED_AFTER_LOGIN). "
                          "Archiving (status) is owner only.")
def patch_member(memberId: str, body: MemberPatch, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    m, u = get_member(db, ctx, memberId)
    data = body.model_dump(exclude_unset=True)
    before = ser_member_basic(m, u)
    if "status" in data and data["status"] != m.status:
        ctx.require_owner()
        m.status = data["status"]
    if "phone" in data and data["phone"] is not None:
        new_phone = normalize_phone(body.phone)
        if new_phone != u.phone:
            if u.first_login_at is not None:
                raise ApiError(409, "PHONE_LOCKED_AFTER_LOGIN")
            other = db.scalar(select(User).where(User.phone == new_phone))
            if other:
                if db.scalar(select(GymMember.id).where(GymMember.gym_id == gym.id, GymMember.user_id == other.id)):
                    raise ApiError(409, "MEMBER_EXISTS")
                m.user_id = other.id
            else:
                shared = db.scalar(select(func.count()).select_from(GymMember).where(GymMember.user_id == u.id)) > 1 \
                    or db.scalar(select(GymStaff.id).where(GymStaff.user_id == u.id).limit(1))
                if shared:
                    nu = User(phone=new_phone, name=u.name, email=u.email, dob=u.dob, gender=u.gender)
                    db.add(nu)
                    db.flush()
                    m.user_id = nu.id
                else:
                    u.phone = new_phone
            db.flush()
            u = db.get(User, m.user_id)
    if "name" in data and data["name"]:
        m.display_name = data["name"].strip()
    for f in ("emergency_contact", "notes"):
        if f in data:
            setattr(m, f, data[f])
    if u.first_login_at is None:
        for f in ("email", "dob", "gender"):
            if f in data:
                setattr(u, f, data[f])
        if "photo_key" in data:
            u.photo_url = data["photo_key"]
    after = ser_member_basic(m, u)
    b, a = diff(before, after)
    action = "member.archive" if a.get("status") == "archived" else "member.update"
    audit(db, gym.id, ctx.user.id, action, "member", m.id,
          f"{'Archived' if action == 'member.archive' else 'Updated'} {m.display_name}", b, a)
    db.commit()
    return {"member": after}


@router.get("/gyms/{gymId}/members/{memberId}/checkins", summary="Member check-in calendar for a month")
def member_checkins(memberId: str, month: str | None = Query(None, examples=["2026-11"]),
                    ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    m, _ = get_member(db, ctx, memberId)
    start, end = month_bounds(month, ctx.gym.timezone)
    rows = db.scalars(select(Checkin).where(Checkin.member_id == m.id, Checkin.local_date >= start,
                                            Checkin.local_date <= end).order_by(Checkin.at.desc())).all()
    names = user_names(db, [c.staff_user_id for c in rows])
    return {"month": start.strftime("%Y-%m"),
            "days": sorted({c.local_date.day for c in rows if c.result == "success"}),
            "items": [{"id": c.id, "at": iso(c.at), "method": c.method, "result": c.result,
                       "staff_name": names.get(c.staff_user_id)} for c in rows]}


# ---------------- assign / renew ----------------
@router.post("/gyms/{gymId}/members/{memberId}/memberships", status_code=201, summary="Assign or renew membership",
             description="Requires `Idempotency-Key`. Default start: day after current end date, or today if none/ended. "
                         "Staff without payments permission can assign but not attach a payment (403 PAYMENT_NOT_ALLOWED).")
def assign_membership(memberId: str, body: MembershipCreate, ctx: GymCtx = Depends(gym_ctx),
                      db: Session = Depends(get_db), idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/members/{memberId}/memberships")
    if idem.replay:
        return idem.replay
    if body.payment:
        ctx.require_payments()
    gym = ctx.gym
    m, _ = get_member(db, ctx, memberId)
    plan = db.get(Plan, body.plan_id)
    if not plan or plan.gym_id != gym.id:
        raise validation({"plan_id": "Pick a plan from this gym."})
    had_any = db.scalar(select(Membership.id).where(Membership.member_id == m.id, Membership.state != "cancelled").limit(1))
    start = body.start_date or default_start(db, gym, m.id)
    ms = create_membership(db, gym, ctx.user.id, m, plan=plan, start=start, discount_paise=body.discount_paise)
    action = "membership.renew" if had_any else "membership.assign"
    audit(db, gym.id, ctx.user.id, action, "membership", ms.id,
          f"{'Renewed' if had_any else 'Assigned'} {ms.plan_name} for {m.display_name} ({dstr(ms.start_date)} → {dstr(ms.end_date)})")
    pay = None
    if body.payment:
        p = body.payment
        ref = validate_reference(p.method, p.reference)
        check_duplicate_reference(db, gym.id, p.method, ref)
        if p.amount_paise > ms.price_paise:
            raise ApiError(422, "OVERPAYMENT", None, {"due_paise": ms.price_paise})
        pay = add_payment(db, gym, ctx.user.id, m, ms, p.amount_paise, p.method, ref, validate_paid_at(gym, p.paid_at))
        audit(db, gym.id, ctx.user.id, "payment.record", "payment", pay.id,
              f"Recorded {rupees(pay.amount_paise)} {pay.method.upper()} from {m.display_name}")
    due = membership_due(db, ms)
    out = {"membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay, due=due),
           "payment": ser_payment(pay, m.display_name, ms.plan_name, ctx.user.name) if pay else None}
    if ctx.can_pay:
        out["due_paise"] = due
    return idem.respond(201, out)


# ---------------- freeze / unfreeze / cancel ----------------
def _membership_ctx(db: Session, user: User, membership_id: str) -> tuple[Membership, GymCtx, GymMember]:
    ms = db.get(Membership, membership_id)
    if not ms:
        raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
    ctx = load_gym_ctx(db, user, ms.gym_id)
    return ms, ctx, db.get(GymMember, ms.member_id)


@router.post("/memberships/{id}/freeze", summary="Freeze membership (moves end date forward)",
             description="422 FREEZE_LIMIT when total freeze days would exceed the gym’s max_freeze_days. "
                         "409 NOT_ACTIVE if not active. Omit `days` for an open-ended freeze (closed by unfreeze).")
def freeze(id: str, body: FreezeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    gym = ctx.gym
    today = today_in(gym.timezone)
    frs = freezes_of(db, ms.id)
    st = membership_status(ms, today, frs)
    if st not in ("active", "expiring"):
        raise ApiError(409, "NOT_ACTIVE")
    if any(f.ended_at is None and (f.to_date is None or f.to_date >= today) for f in frs):
        raise ApiError(409, "NOT_ACTIVE", "This membership already has a freeze scheduled.")
    start = body.from_date or today
    if start < today or start > ms.end_date:
        raise validation({"from_date": "Freeze must start between today and the membership end date."})
    max_days = int(gym_settings(gym)["max_freeze_days"])
    used = sum(f.days for f in frs)
    if (body.days and used + body.days > max_days) or (not body.days and used >= max_days):
        raise ApiError(422, "FREEZE_LIMIT", None, {"max_days": max_days, "used_days": used,
                                                    "remaining_days": max(0, max_days - used)})
    before = {"end_date": ms.end_date, "state": ms.state}
    f = MembershipFreeze(membership_id=ms.id, from_date=start, reason=body.reason, note=body.note, created_by=user.id,
                         days=body.days or 0, to_date=start + timedelta(days=body.days - 1) if body.days else None)
    db.add(f)
    if body.days:
        ms.end_date = ms.end_date + timedelta(days=body.days)
    if start <= today:
        ms.state = "frozen"
    db.flush()
    audit(db, gym.id, user.id, "membership.freeze", "membership", ms.id,
          f"Froze {member.display_name}’s {ms.plan_name} from {dstr(start)}"
          + (f" for {body.days} days" if body.days else " (open-ended)"),
          before, {"end_date": ms.end_date, "state": ms.state})
    db.commit()
    return {"membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay)}


@router.post("/memberships/{id}/unfreeze", summary="End freeze today; unused days are given back")
def unfreeze(id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    gym = ctx.gym
    today = today_in(gym.timezone)
    f = next((x for x in freezes_of(db, ms.id) if x.ended_at is None and (x.to_date is None or x.to_date >= today)), None)
    if not f or ms.state == "cancelled":
        raise ApiError(409, "NOT_ACTIVE", "There’s no freeze to end.")
    before = {"end_date": ms.end_date, "state": ms.state}
    used = max(0, (today - f.from_date).days)
    if f.to_date is not None:  # fixed-length freeze: give back unused days
        ms.end_date = ms.end_date - timedelta(days=f.days - used)
    else:  # open-ended: extend by days actually frozen (capped)
        max_days = int(gym_settings(gym)["max_freeze_days"])
        other = sum(x.days for x in freezes_of(db, ms.id) if x.id != f.id)
        used = min(used, max(0, max_days - other))
        ms.end_date = ms.end_date + timedelta(days=used)
    f.days = used
    f.to_date = f.from_date + timedelta(days=used - 1) if used else f.from_date - timedelta(days=1)
    f.ended_at, f.ended_by = utcnow(), user.id
    ms.state = "scheduled" if ms.start_date > today else "active"
    audit(db, gym.id, user.id, "membership.unfreeze", "membership", ms.id,
          f"Unfroze {member.display_name}’s {ms.plan_name} ({used} days used)", before,
          {"end_date": ms.end_date, "state": ms.state})
    db.commit()
    return {"membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay)}


@router.post("/memberships/{id}/cancel", summary="Cancel membership (owner only)",
             description="Refunds are recorded outside the app in v1; `refund_note` is kept for reference.")
def cancel(id: str, body: CancelIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require_owner()
    if ms.state == "cancelled":
        raise ApiError(409, "NOT_ACTIVE", "This membership is already cancelled.")
    now = utcnow()
    for f in freezes_of(db, ms.id):
        if f.ended_at is None:
            f.ended_at, f.ended_by = now, user.id
    before = {"state": ms.state}
    ms.state, ms.cancelled_at, ms.cancel_reason, ms.refund_note = "cancelled", now, body.reason, body.refund_note
    audit(db, ctx.gym.id, user.id, "membership.cancel", "membership", ms.id,
          f"Cancelled {member.display_name}’s {ms.plan_name}: {body.reason}", before, {"state": "cancelled"})
    db.commit()
    return {"membership": ser_membership(ms, ctx.gym, db, can_pay=ctx.can_pay)}
