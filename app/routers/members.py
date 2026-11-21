"""§8 — members & memberships (assign/renew, freeze, unfreeze, cancel)."""
import csv
import io
import re
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, Idem, current_user, gym_ctx, idem_header, load_gym_ctx, perm_ctx
from app.errors import ApiError, validation
from app.models import (Checkin, GymMember, GymStaff, MemberNote, Membership, MembershipFreeze, Payment, Plan,
                        User)
from app.schemas import CancelIn, FreezeIn, GroupMembershipIn, MemberCreate, MemberPatch, MembershipCreate, NoteIn, SuspendIn
from app.services.storage import public_url
from app.services.audit import audit, diff
from app.services.cache import cache
from app.services.core import (pt_out, add_payment, check_duplicate_reference, create_membership, default_start,
                               get_or_create_user, gym_settings, next_member_code, ser_member_basic, settle_credit,
                               ser_membership, ser_payment, user_names, validate_paid_at, validate_reference)
from app.services.rules import LIVE, days_left, freezes_of, load_states, member_state, membership_due, membership_status, \
    pick_current
from app.utils import (uuid7, dstr, iso, local_midnight_utc, month_bounds, normalize_phone, paginate_list, rupees,
                       today_in, tz, utcnow)

from app.responses import DocRoute, ERRORS, NoteListOut, NoteOut, MemberCheckinsOut, MemberCreateOut, MemberDetailOut, MemberListOut, MemberWrapOut, MembershipCreateOut, MembershipWrapOut

router = APIRouter(tags=["Members & memberships"], route_class=DocRoute, responses=ERRORS)

StatusFilter = Literal["all", "active", "expiring", "expired", "frozen", "suspended", "upcoming", "cancelled",
                       "no_plan", "dues", "away", "not_on_app", "archived", "new_month", "renewed_month", "pt"]
TAB_KEYS = ["all", "active", "expiring", "expired", "frozen", "suspended", "upcoming", "cancelled", "no_plan", "away",
            "not_on_app", "archived", "pt"]


def get_member(db: Session, ctx: GymCtx, member_id: str) -> tuple[GymMember, User]:
    m = db.get(GymMember, member_id)
    if not m or m.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    return m, db.get(User, m.user_id)


def phone_digits(q: str) -> str:
    """'+91 98480-11234' / '098480 11234' / '919848011234' -> '9848011234' (partial digits kept)."""
    d = re.sub(r"\D", "", q)
    if len(d) > 10 and d.startswith("91"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d


def matches(q: str, m: GymMember, u: User) -> bool:
    ql = " ".join(q.lower().split())
    if not ql:
        return True
    name = " ".join((m.display_name or "").lower().split())
    if ql in name or all(part in name for part in ql.split()):
        return True
    if ql.replace(" ", "").replace("-", "") in m.member_code.lower().replace("-", ""):
        return True
    d = phone_digits(q)
    return len(d) >= 3 and re.sub(r"\D", "", q).strip() != "" and d in (u.phone or "")[-10:]


def current_out(st, today, can_pay: bool) -> dict | None:
    cur = st.current_dict(today)
    if cur is None:
        return None
    cur["id"] = cur["membership_id"]
    if can_pay:
        cur["due_paise"] = next((d for m, _, d in st.memberships if m.id == st.current.id), 0)
    return cur


def enrich_members(db: Session, ctx: GymCtx, rows, today):
    """[(member, user, state, tags)] with the tab tags used for filters & counts."""
    gym = ctx.gym
    states = load_states(db, gym, [m.id for m, _ in rows])
    away_cut = local_midnight_utc(today - timedelta(days=14), gym.timezone)
    out = []
    for m, u in rows:
        st = states[m.id]
        live = st.status in LIVE
        if m.status == "archived":
            tags = {"archived"}
        else:
            tags = {st.status, "all"}
            if st.status in ("active", "expiring"):
                tags.add("active")
            if st.due_paise > 0:
                tags.add("dues")
            if live and (m.last_checkin_at is None or m.last_checkin_at < away_cut):
                tags.add("away")
            if live and u.first_login_at is None:
                tags.add("not_on_app")
            if m.pt_enabled:
                tags.add("pt")
        out.append((m, u, st, tags))
    return out


def list_item(m: GymMember, u: User, st, today, can_pay: bool) -> dict:
    cur = current_out(st, today, can_pay)
    it = {"id": m.id, "member_code": m.member_code, "name": m.display_name, "phone": u.phone,
          "photo_url": public_url(u.photo_url), "on_app": u.first_login_at is not None,
          "status": "archived" if m.status == "archived" else st.status, "source": m.source,
          "joined_at": dstr(m.joined_on), "last_checkin_at": iso(m.last_checkin_at), "last_visit_at": iso(m.last_checkin_at),
          "current": cur, "current_membership": cur, "has_dues": st.due_paise > 0, "pt": pt_out(m)}
    if can_pay:
        it["due_paise"] = st.due_paise
    return it


def month_member_ids(db: Session, gym, today, which: str) -> set[str]:
    """Same rules as the home screen's “New this month” / “Renewals” numbers."""
    start = local_midnight_utc(today.replace(day=1), gym.timezone)
    if which == "new_month":
        return set(db.scalars(select(GymMember.id).where(GymMember.gym_id == gym.id, GymMember.created_at >= start,
                                                         GymMember.source != "import")))
    out = set()
    for mid, created in db.execute(select(Membership.member_id, Membership.created_at).where(
            Membership.gym_id == gym.id, Membership.created_at >= start, Membership.source == "manual",
            Membership.state != "cancelled")).all():
        if db.scalar(select(Membership.id).where(Membership.member_id == mid, Membership.created_at < created,
                                                 Membership.state != "cancelled").limit(1)):
            out.add(mid)
    return out


def _filtered(db: Session, ctx: GymCtx, q, status, sort):
    gym = ctx.gym
    today = today_in(gym.timezone)
    rows = db.execute(select(GymMember, User).join(User, User.id == GymMember.user_id)
                      .where(GymMember.gym_id == gym.id)).all()
    enriched = enrich_members(db, ctx, rows, today)
    if q and q.strip():
        enriched = [e for e in enriched if matches(q, e[0], e[1])]
    keys = TAB_KEYS + (["dues"] if ctx.can_pay else [])
    counts = {k: sum(1 for *_, t in enriched if k in t) for k in keys}
    if status in ("new_month", "renewed_month"):
        ids = month_member_ids(db, gym, today, status)
        sel = [e for e in enriched if e[0].id in ids]
    else:
        sel = [e for e in enriched if status in e[3]]
    if sort == "expiry":
        sel.sort(key=lambda e: (e[2].current is None, e[2].current.end_date if e[2].current else today,
                                e[0].display_name.lower()))
    elif sort == "last_visit":
        sel.sort(key=lambda e: (e[0].last_checkin_at is None,
                                -(e[0].last_checkin_at.timestamp()) if e[0].last_checkin_at else 0))
    elif sort == "joined":
        sel.sort(key=lambda e: e[0].created_at, reverse=True)
    else:
        sel.sort(key=lambda e: (e[0].display_name or "").lower())
    return sel, counts, len(enriched), today


@router.get("/gyms/{gymId}/members", response_model=MemberListOut, summary="List / search members",
            description="`q` matches name (case-insensitive, partial, any word order), member code, or phone "
                        "(spaces, dashes, +91 and a leading 0 are ignored). `status` tabs: all · active (=active+expiring) · "
                        "expiring · expired · frozen · suspended · upcoming · cancelled · no_plan · dues* · away "
                        "(live plan, no visit 14 days) · not_on_app · archived. `counts` (every tab) and `total` "
                        "come back on every call. *money only with payments.view.")
def list_members(q: str | None = None, status: StatusFilter = "all",
                 sort: Literal["name", "expiry", "last_visit", "joined"] = "name",
                 limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                 ctx: GymCtx = Depends(perm_ctx("members.view")), db: Session = Depends(get_db)):
    if status == "dues":
        ctx.require("payments.view")
    sel, counts, total, today = _filtered(db, ctx, q, status, sort)
    page, nxt = paginate_list(sel, limit, cursor)
    return {"items": [list_item(m, u, st, today, ctx.can_pay) for m, u, st, _ in page], "next_cursor": nxt,
            "counts": counts, "total": total, "filtered_total": len(sel)}


@router.get("/gyms/{gymId}/members/export.csv", summary="Export members CSV (reports.export)",
            description="Same filters as the list (`q`, `status`, `sort`); exports every matching row, not just a page.",
            responses={200: {"content": {"text/csv": {}}}})
def export_members(q: str | None = None, status: StatusFilter = "all",
                   sort: Literal["name", "expiry", "last_visit", "joined"] = "name",
                   ctx: GymCtx = Depends(perm_ctx("reports.export")), db: Session = Depends(get_db)):
    if status == "dues":
        ctx.require("payments.view")
    sel, _, _, today = _filtered(db, ctx, q, status, sort)
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["Member code", "Name", "Mobile", "Email", "Gender", "Status", "Plan", "Start", "End", "Days left",
            "Joined", "Last visit", "On app", "Source", "Personal training", "PT fee (₹)", "Trainer"]
    if ctx.can_pay:
        head.append("Due (₹)")
    w.writerow(head)
    for m, u, st, _ in sel:
        c = st.current
        row = [m.member_code, m.display_name, u.phone, u.email or "", u.gender or "",
               "archived" if m.status == "archived" else st.status, c.plan_name if c else "",
               dstr(c.start_date) if c else "", dstr(c.end_date) if c else "",
               days_left(c, st.status, today) if c else "", dstr(m.joined_on),
               m.last_checkin_at.astimezone(tz(ctx.gym.timezone)).strftime("%Y-%m-%d %H:%M") if m.last_checkin_at else "",
               "yes" if u.first_login_at else "no", m.source, "yes" if m.pt_enabled else "no",
               f"{m.pt_fee_paise / 100:.2f}" if m.pt_fee_paise else "", m.pt_trainer or ""]
        if ctx.can_pay:
            row.append(f"{st.due_paise / 100:.2f}")
        w.writerow(row)
    audit(db, ctx.gym.id, ctx.user.id, "export.members", None, None, f"Exported {len(sel)} members ({status})")
    db.commit()
    name = f"{ctx.gym.code_prefix}-members-{status}-{today.isoformat()}.csv"
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------- create ----------------
@router.post("/gyms/{gymId}/members", response_model=MemberCreateOut, status_code=201, summary="Add member (+ optional membership & payment)",
             description="Requires `Idempotency-Key`. 409 MEMBER_EXISTS if this phone is already a member here.")
def create_member(body: MemberCreate, ctx: GymCtx = Depends(perm_ctx("members.edit")), db: Session = Depends(get_db),
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
    if body.photo_key:  # a photo the desk just took or picked is always saved (Edit member does the same)
        user.photo_url = body.photo_key
    today = today_in(gym.timezone)
    member = GymMember(gym_id=gym.id, user_id=user.id, member_code=next_member_code(db, gym.id),
                       display_name=body.name.strip(), source="manual", emergency_contact=body.emergency_contact,
                       notes=body.notes, status="active", joined_on=today,
                       pt_enabled=bool(body.pt_enabled or body.pt_fee_paise or body.pt_trainer),
                       pt_fee_paise=body.pt_fee_paise, pt_trainer=(body.pt_trainer or "").strip() or None)
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
    settle_credit(db, gym, member, ctx.user.id, ms.id if ms else None)
    body_out = {"member": ser_member_basic(member, user),
                "membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay,
                                             due=membership_due(db, ms)) if ms else None,
                "payment": ser_payment(pay, member.display_name, ms.plan_name if ms else None, ctx.user.name) if pay else None}
    resp = idem.respond(201, body_out)
    from app.services.whatsapp import fire_later
    # straight away, once per member ever: "your gym is on DUMBBEL.fit" with the app link
    fire_later(gym.id, "gym_live", member.id, ms.id if ms else None, dedupe=f"live:{member.id}")
    if ms:
        fire_later(gym.id, "welcome", member.id, ms.id, dedupe=f"welcome:{member.id}")
        if pay:
            fire_later(gym.id, "payment_received", member.id, ms.id, {"amount": rupees(pay.amount_paise)}, f"paid:{pay.id}")
    return resp


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


@router.get("/gyms/{gymId}/members/{memberId}", response_model=MemberDetailOut, summary="Member detail: memberships, payments*, stats, insights, notes")
def member_detail(memberId: str, ctx: GymCtx = Depends(perm_ctx("members.view")), db: Session = Depends(get_db)):
    gym = ctx.gym
    m, u = get_member(db, ctx, memberId)
    today = today_in(gym.timezone)
    st = member_state(db, gym, m.id)
    fz = {}
    for f in db.scalars(select(MembershipFreeze).where(MembershipFreeze.membership_id.in_(
            [x.id for x, _, _ in st.memberships] or [""]))):
        fz.setdefault(f.membership_id, []).append(f)
    memberships = [ser_membership(x, gym, db, status=s, due=d, can_pay=ctx.can_pay, freezes=fz.get(x.id, []))
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
    if u.first_login_at:
        app_status = {"key": "app_status", "value": "on_app", "since": iso(u.first_login_at),
                      "rule": "Logged in to the member app"}
    else:
        app_status = {"key": "app_status", "value": "not_on_app",
                      "rule": "Hasn’t logged in to the member app yet — they just log in with this phone number"}
    insights.append(app_status)

    member_out = ser_member_basic(m, u)
    member_out["current_membership"] = current_out(st, today, ctx.can_pay)
    notes = db.execute(select(MemberNote, User).outerjoin(User, User.id == MemberNote.author_id).where(
        MemberNote.member_id == m.id, MemberNote.deleted_at.is_(None)).order_by(MemberNote.created_at.desc()).limit(20)).all()
    out = {"member": member_out, "notes_history": [ser_note(n, a) for n, a in notes],
           "user": {"on_app": u.first_login_at is not None,
                                                     "first_login_at": iso(u.first_login_at)},
           "current_membership": current, "memberships": memberships,
           "stats": {"visits_total": len(days), "visits_30d": v30, "visit_rate_8w": rate, "gym_avg_rate_8w": avg,
                     "first_visit": dstr(days[0]) if days else None, "last_visit": dstr(days[-1]) if days else None},
           "insights": insights}
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
@router.patch("/gyms/{gymId}/members/{memberId}", response_model=MemberWrapOut, summary="Edit member / archive",
              description="Phone change allowed only if the member never logged in (else 409 PHONE_LOCKED_AFTER_LOGIN). "
                          "Archiving (status) is owner only.")
def patch_member(memberId: str, body: MemberPatch, ctx: GymCtx = Depends(perm_ctx("members.edit")), db: Session = Depends(get_db)):
    gym = ctx.gym
    m, u = get_member(db, ctx, memberId)
    data = body.model_dump(exclude_unset=True)
    before = ser_member_basic(m, u)
    if "status" in data and data["status"] != m.status:
        ctx.require("members.archive")
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
    for f in ("emergency_contact", "notes", "pt_fee_paise"):
        if f in data:
            setattr(m, f, data[f])
    if "pt_enabled" in data:
        m.pt_enabled = bool(data["pt_enabled"])
    if "pt_trainer" in data:
        m.pt_trainer = (data["pt_trainer"] or "").strip() or None
    on_app = u.first_login_at is not None
    for f in ("email", "dob", "gender"):
        if f in data and not (on_app and data[f] is None):
            setattr(u, f, data[f])
    if "photo_key" in data:  # a new key sets the photo, null removes it
        u.photo_url = data["photo_key"] or None
    after = ser_member_basic(m, u)
    b, a = diff(before, after)
    action = "member.archive" if a.get("status") == "archived" else "member.update"
    audit(db, gym.id, ctx.user.id, action, "member", m.id,
          f"{'Archived' if action == 'member.archive' else 'Updated'} {m.display_name}", b, a)
    db.commit()
    return {"member": after}


@router.get("/gyms/{gymId}/members/{memberId}/checkins", response_model=MemberCheckinsOut, summary="Member check-in calendar for a month")
def member_checkins(memberId: str, month: str | None = Query(None, examples=["2026-11"]),
                    ctx: GymCtx = Depends(perm_ctx("members.view")), db: Session = Depends(get_db)):
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
@router.post("/gyms/{gymId}/members/{memberId}/memberships", response_model=MembershipCreateOut, status_code=201, summary="Assign or renew membership",
             description="Requires `Idempotency-Key`. Default start: day after current end date, or today if none/ended. "
                         "Staff without payments permission can assign but not attach a payment (403 PAYMENT_NOT_ALLOWED).")
def assign_membership(memberId: str, body: MembershipCreate, ctx: GymCtx = Depends(perm_ctx("memberships.manage")),
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
    # anyone who ever had a plan (even a cancelled one) gets the "membership confirmed" message, not a second welcome:
    # the welcome goes once per member, so it would be skipped and they'd only get the payment receipt
    ever_had = had_any or db.scalar(select(Membership.id).where(Membership.member_id == m.id).limit(1))
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
    settle_credit(db, gym, m, ctx.user.id, ms.id)  # use any advance/credit the member already has
    due = membership_due(db, ms)
    out = {"membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay, due=due),
           "payment": ser_payment(pay, m.display_name, ms.plan_name, ctx.user.name) if pay else None}
    if ctx.can_pay:
        out["due_paise"] = due
    resp = idem.respond(201, out)
    from app.services.whatsapp import fire_later
    fire_later(gym.id, "gym_live", m.id, ms.id, dedupe=f"live:{m.id}")  # skipped if this member already had it
    if ever_had:
        fire_later(gym.id, "renewal_confirmed", m.id, ms.id, dedupe=f"renewed:{ms.id}")
    else:
        fire_later(gym.id, "welcome", m.id, ms.id, dedupe=f"welcome:{m.id}")
    if pay:
        fire_later(gym.id, "payment_received", m.id, ms.id, {"amount": rupees(pay.amount_paise)}, f"paid:{pay.id}")
    return resp


@router.post("/gyms/{gymId}/group-memberships", status_code=201, summary="Group plan: several people join together",
             description="Requires `Idempotency-Key`. Each person gets their own membership at the plan's per-person price, "
                         "linked by a shared `group_id`. New people (name + phone) are added as members; a phone that is "
                         "already a member here is reused. The optional payment is the group's total, split equally.")
def group_membership(body: GroupMembershipIn, ctx: GymCtx = Depends(perm_ctx("memberships.manage")),
                     db: Session = Depends(get_db), idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/group-memberships")
    if idem.replay:
        return idem.replay
    if body.payment:
        ctx.require_payments()
    gym = ctx.gym
    plan = db.get(Plan, body.plan_id)
    if not plan or plan.gym_id != gym.id:
        raise validation({"plan_id": "Pick a plan from this gym."})
    if plan.group_size and len(body.people) != plan.group_size:
        raise validation({"people": f"{plan.name} is for {plan.group_size} people. Add exactly {plan.group_size}."})
    today = today_in(gym.timezone)
    people: list[tuple[GymMember, bool]] = []  # (member, is_new)
    seen: set[str] = set()
    for i, gp in enumerate(body.people):
        if gp.member_id:
            m, _ = get_member(db, ctx, gp.member_id)
            new = False
        else:
            if not (gp.name or "").strip() or not gp.phone:
                raise validation({f"people.{i}": "Add a name and phone number for each person."})
            phone = normalize_phone(gp.phone, f"people.{i}.phone")
            user, _ = get_or_create_user(db, phone, gp.name.strip())
            m = db.scalar(select(GymMember).where(GymMember.gym_id == gym.id, GymMember.user_id == user.id))
            new = m is None
            if new:
                m = GymMember(gym_id=gym.id, user_id=user.id, member_code=next_member_code(db, gym.id),
                              display_name=gp.name.strip(), source="manual", status="active", joined_on=today)
                db.add(m)
                db.flush()
                audit(db, gym.id, ctx.user.id, "member.create", "member", m.id, f"Added member {m.display_name}")
        if m.id in seen:
            raise validation({f"people.{i}": f"{m.display_name} is in the group twice."})
        seen.add(m.id)
        people.append((m, new))
    gid = uuid7()
    names = ", ".join(m.display_name for m, _ in people)
    rows = []
    for m, new in people:
        ever_had = not new and db.scalar(select(Membership.id).where(Membership.member_id == m.id).limit(1))
        start = body.start_date or default_start(db, gym, m.id)
        ms = create_membership(db, gym, ctx.user.id, m, plan=plan, start=start)
        ms.group_id = gid
        audit(db, gym.id, ctx.user.id, "membership.assign", "membership", ms.id,
              f"Group plan {ms.plan_name} for {m.display_name} with {names} ({dstr(ms.start_date)} → {dstr(ms.end_date)})")
        rows.append([m, ms, None, ever_had])
    if body.payment:
        p = body.payment
        ref = validate_reference(p.method, p.reference)
        check_duplicate_reference(db, gym.id, p.method, ref)
        total_price = sum(ms.price_paise for _, ms, _, _ in rows)
        if p.amount_paise > total_price:
            raise ApiError(422, "OVERPAYMENT", None, {"due_paise": total_price})
        share, extra = divmod(p.amount_paise, len(rows))
        paid_at = validate_paid_at(gym, p.paid_at)
        left = p.amount_paise
        for k, r in enumerate(rows):
            amt = min(r[1].price_paise, share + (1 if k < extra else 0), left)
            if k == len(rows) - 1:
                amt = min(r[1].price_paise, left)
            if amt <= 0:
                continue
            note = f"Group payment of {rupees(p.amount_paise)} for {names}" + (f" (ref {ref})" if ref and k else "")
            r[2] = add_payment(db, gym, ctx.user.id, r[0], r[1], amt, p.method, ref if k == 0 else None, paid_at, note=note)
            left -= amt
            audit(db, gym.id, ctx.user.id, "payment.record", "payment", r[2].id,
                  f"Recorded {rupees(amt)} {p.method.upper()} from {r[0].display_name} (group)")
        if left > 0:  # shares capped by a cheaper membership: put the rest on whoever still owes
            for r in rows:
                room = r[1].price_paise - sum(x.amount_paise for x in [r[2]] if x)
                if left > 0 and room > 0 and r[2]:
                    add = min(room, left)
                    r[2].amount_paise += add
                    left -= add
    for m, ms, _, _ in rows:
        settle_credit(db, gym, m, ctx.user.id, ms.id)
    out = {"group_id": gid, "plan": {"id": plan.id, "name": plan.name, "group_size": plan.group_size},
           "members": [{"member": ser_member_basic(m, db.get(User, m.user_id)),
                        "membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay, due=membership_due(db, ms)),
                        "payment": ser_payment(pay, m.display_name, ms.plan_name, ctx.user.name) if pay else None}
                       for m, ms, pay, _ in rows]}
    if ctx.can_pay:
        out["due_paise"] = sum(membership_due(db, ms) for _, ms, _, _ in rows)
    resp = idem.respond(201, out)
    from app.services.whatsapp import fire_later
    for m, ms, pay, ever_had in rows:
        fire_later(gym.id, "gym_live", m.id, ms.id, dedupe=f"live:{m.id}")
        if ever_had:
            fire_later(gym.id, "renewal_confirmed", m.id, ms.id, dedupe=f"renewed:{ms.id}")
        else:
            fire_later(gym.id, "welcome", m.id, ms.id, dedupe=f"welcome:{m.id}")
        if pay:
            fire_later(gym.id, "payment_received", m.id, ms.id, {"amount": rupees(pay.amount_paise)}, f"paid:{pay.id}")
    return resp


# ---------------- freeze / unfreeze / cancel ----------------
def _membership_ctx(db: Session, user: User, membership_id: str) -> tuple[Membership, GymCtx, GymMember]:
    ms = db.get(Membership, membership_id)
    if not ms:
        raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
    ctx = load_gym_ctx(db, user, ms.gym_id)
    return ms, ctx, db.get(GymMember, ms.member_id)


@router.post("/memberships/{id}/freeze", response_model=MembershipWrapOut, summary="Freeze membership (moves end date forward)",
             description="422 FREEZE_LIMIT when total freeze days would exceed the gym’s max_freeze_days. "
                         "409 NOT_ACTIVE if not active. Omit `days` for an open-ended freeze (closed by unfreeze).")
def freeze(id: str, body: FreezeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require("memberships.manage")
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
    plan = db.get(Plan, ms.plan_id) if ms.plan_id else None
    if plan and not plan.freeze_allowed:
        raise ApiError(422, "FREEZE_NOT_ALLOWED", f"The {plan.name} plan can’t be frozen.")
    max_days = int(plan.max_freeze_days if plan and plan.max_freeze_days is not None
                   else gym_settings(gym)["max_freeze_days"])
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
    from app.services.whatsapp import fire_later
    fire_later(gym.id, "membership_frozen", member.id, ms.id, dedupe=f"frozen:{f.id}")
    return {"membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay)}


@router.post("/memberships/{id}/unfreeze", response_model=MembershipWrapOut, summary="End freeze today; unused days are given back")
def unfreeze(id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require("memberships.manage")
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
    from app.services.whatsapp import fire_later
    fire_later(gym.id, "membership_resumed", member.id, ms.id, dedupe=f"resumed:{f.id}")
    return {"membership": ser_membership(ms, gym, db, can_pay=ctx.can_pay)}


def _void_membership_payments(db: Session, ctx: GymCtx, user: User, ms: Membership, member: GymMember,
                               why: str) -> tuple[int, int]:
    """Void every recorded payment of this membership (kept, shown as voided) so collections stop counting it.
    Returns (count, paise). Caller commits."""
    pays = db.scalars(select(Payment).where(Payment.membership_id == ms.id, Payment.status == "recorded")).all()
    now = utcnow()
    for p in pays:
        p.status, p.void_reason, p.voided_by, p.voided_at = "voided", why[:500], user.id, now
        audit(db, ctx.gym.id, user.id, "payment.void", "payment", p.id,
              f"Voided {rupees(p.amount_paise)} {p.method.upper()} from {member.display_name}: {why}",
              {"status": "recorded"}, {"status": "voided"})
    if pays:
        db.flush()
        settle_credit(db, ctx.gym, member, user.id)  # if voided money was credit already used, undo that use
    return len(pays), sum(p.amount_paise for p in pays)


def _paid_on(db: Session, ms: Membership) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
        Payment.membership_id == ms.id, Payment.status == "recorded")) or 0)


@router.post("/memberships/{id}/cancel", response_model=MembershipWrapOut, summary="Cancel membership (owner only)",
             description="Refunds are recorded outside the app in v1; `refund_note` is kept for reference. "
                         "`remove_payments` voids this membership's payments so they leave today's / the month's "
                         "collections (default: yes, when you may void payments). Nothing is deleted.")
def cancel(id: str, body: CancelIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require("memberships.cancel")
    if ms.state == "cancelled":
        raise ApiError(409, "NOT_ACTIVE", "This membership is already cancelled.")
    remove = body.remove_payments
    if remove is None:
        remove = ctx.has("payments.void")
    elif remove:
        ctx.require("payments.void")
    now = utcnow()
    for f in freezes_of(db, ms.id):
        if f.ended_at is None:
            f.ended_at, f.ended_by = now, user.id
    before = {"state": ms.state}
    ms.state, ms.cancelled_at, ms.cancel_reason, ms.refund_note = "cancelled", now, body.reason, body.refund_note
    audit(db, ctx.gym.id, user.id, "membership.cancel", "membership", ms.id,
          f"Cancelled {member.display_name}’s {ms.plan_name}: {body.reason}", before, {"state": "cancelled"})
    voided = (0, 0)
    if remove:
        voided = _void_membership_payments(db, ctx, user, ms, member, f"Membership cancelled: {body.reason}")
    db.commit()
    from app.services.whatsapp import fire_later
    fire_later(ctx.gym.id, "membership_cancelled", member.id, ms.id, dedupe=f"cancelled:{ms.id}")
    return {"membership": ser_membership(ms, ctx.gym, db, can_pay=ctx.can_pay),
            "payments_voided": voided[0], "voided_paise": voided[1]}


@router.post("/memberships/{id}/remove-payments", response_model=MembershipWrapOut,
             summary="Cancelled membership: take its payments out of collections (payments.void)",
             description="For memberships cancelled before cancelling did this automatically. Voids the payments "
                         "recorded for this cancelled membership; they stay visible as voided. Nothing is deleted.")
def remove_cancelled_payments(id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require("payments.void")
    if ms.state != "cancelled":
        raise ApiError(409, "NOT_ACTIVE", "Only a cancelled membership’s payments can be removed this way. "
                                          "Void a single payment from Payments instead.")
    n, paise = _void_membership_payments(db, ctx, user, ms, member,
                                         f"Membership cancelled: {ms.cancel_reason or 'cancelled'}")
    db.commit()
    return {"membership": ser_membership(ms, ctx.gym, db, can_pay=ctx.can_pay), "payments_voided": n, "voided_paise": paise}


# ---------------- suspend / resume ----------------
@router.post("/memberships/{id}/suspend", response_model=MembershipWrapOut,
             summary="Suspend: pause access, days keep running (memberships.manage)",
             description="Unlike freeze, the end date does NOT move. Check-ins return `suspended` until resumed.")
def suspend(id: str, body: SuspendIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require("memberships.manage")
    st = membership_status(ms, today_in(ctx.gym.timezone), freezes_of(db, ms.id))
    if st in ("cancelled", "expired", "suspended"):
        raise ApiError(409, "NOT_ACTIVE", f"This membership is {st}.")
    ms.suspended_at, ms.suspended_by, ms.suspend_reason = utcnow(), user.id, body.reason
    audit(db, ctx.gym.id, user.id, "membership.suspend", "membership", ms.id,
          f"Suspended {member.display_name}’s {ms.plan_name}: {body.reason}")
    db.commit()
    return {"membership": ser_membership(ms, ctx.gym, db, can_pay=ctx.can_pay)}


@router.post("/memberships/{id}/resume", response_model=MembershipWrapOut, summary="Resume a suspended membership")
def resume(id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms, ctx, member = _membership_ctx(db, user, id)
    ctx.require("memberships.manage")
    if not ms.suspended_at:
        raise ApiError(409, "NOT_ACTIVE", "This membership isn’t suspended.")
    ms.suspended_at = ms.suspended_by = ms.suspend_reason = None
    audit(db, ctx.gym.id, user.id, "membership.resume", "membership", ms.id,
          f"Resumed {member.display_name}’s {ms.plan_name}")
    db.commit()
    return {"membership": ser_membership(ms, ctx.gym, db, can_pay=ctx.can_pay)}


# ---------------- notes history ----------------
def ser_note(n: MemberNote, author: User | None) -> dict:
    return {"id": n.id, "body": n.body, "created_at": iso(n.created_at),
            "author": {"id": author.id, "name": author.name} if author else None}


@router.get("/gyms/{gymId}/members/{memberId}/notes", response_model=NoteListOut,
            summary="Member notes, newest first (staff only — never shown to the member)")
def list_notes(memberId: str, ctx: GymCtx = Depends(perm_ctx("members.view")), db: Session = Depends(get_db)):
    m, _ = get_member(db, ctx, memberId)
    rows = db.execute(select(MemberNote, User).outerjoin(User, User.id == MemberNote.author_id).where(
        MemberNote.member_id == m.id, MemberNote.deleted_at.is_(None)).order_by(MemberNote.created_at.desc())).all()
    return {"items": [ser_note(n, a) for n, a in rows]}


@router.post("/gyms/{gymId}/members/{memberId}/notes", response_model=NoteOut, status_code=201,
             summary="Add a dated note (members.edit)")
def add_note(memberId: str, body: NoteIn, ctx: GymCtx = Depends(perm_ctx("members.edit")), db: Session = Depends(get_db)):
    m, _ = get_member(db, ctx, memberId)
    n = MemberNote(gym_id=ctx.gym.id, member_id=m.id, author_id=ctx.user.id, body=body.body.strip())
    db.add(n)
    db.flush()
    audit(db, ctx.gym.id, ctx.user.id, "member.note", "member", m.id, f"Added a note on {m.display_name}")
    db.commit()
    return ser_note(n, ctx.user)


@router.delete("/gyms/{gymId}/members/{memberId}/notes/{noteId}", status_code=204,
               summary="Delete a note (author, or members.archive)")
def delete_note(memberId: str, noteId: str, ctx: GymCtx = Depends(perm_ctx("members.edit")), db: Session = Depends(get_db)):
    m, _ = get_member(db, ctx, memberId)
    n = db.get(MemberNote, noteId)
    if not n or n.member_id != m.id or n.deleted_at:
        raise ApiError(404, "NOT_FOUND", "That note wasn’t found.")
    if n.author_id != ctx.user.id:
        ctx.require("members.archive")
    n.deleted_at = utcnow()
    audit(db, ctx.gym.id, ctx.user.id, "member.note_delete", "member", m.id, f"Deleted a note on {m.display_name}")
    db.commit()
    return Response(status_code=204)
