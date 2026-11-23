"""Shared domain operations + serializers used by several routers and the importer."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError, validation
from app.models import Gym, GymMember, GymStaff, Membership, MembershipFreeze, Payment, Plan, User
from app.services import storage
from app.services.rules import days_left, membership_status, freezes_of
from app.utils import dstr, financial_year, iso, local_date_of, today_in, utcnow

DEFAULT_SETTINGS = {"checkin_cooldown_min": 120, "max_freeze_days": 30, "receipt_footer": "Thank you for training with us!",
                    # member scans the gym's QR: printed poster allowed? location required? radius in metres
                    "checkin_static_qr": True, "checkin_require_location": True, "checkin_radius_m": 150}


def gym_settings(gym: Gym) -> dict:
    return {**DEFAULT_SETTINGS, **(gym.settings or {})}


def lock_gym(db: Session, gym_id: str) -> Gym:
    return db.scalar(select(Gym).where(Gym.id == gym_id).with_for_update()
                     .execution_options(populate_existing=True))


def next_member_code(db: Session, gym_id: str) -> str:
    g = lock_gym(db, gym_id)
    g.member_seq += 1
    yy = str(today_in(g.timezone).year)[-2:]
    return f"{g.code_prefix}-{yy}-{g.member_seq:04d}"


def next_receipt_no(db: Session, gym_id: str, paid_at: datetime) -> str:
    g = lock_gym(db, gym_id)
    g.receipt_seq += 1
    fy = financial_year(local_date_of(paid_at, g.timezone))
    return f"{g.code_prefix}/{fy}/{g.receipt_seq:06d}"


def get_or_create_user(db: Session, phone: str, name: str | None = None, **extra) -> tuple[User, bool]:
    u = db.scalar(select(User).where(User.phone == phone))
    if u:
        if not u.name and name:
            u.name = name
        return u, False
    u = User(phone=phone, name=name, **{k: v for k, v in extra.items() if v is not None})
    db.add(u)
    db.flush()
    return u, True


def overlapping(db: Session, member_id: str, start: date, end: date, exclude_id: str | None = None) -> Membership | None:
    q = select(Membership).where(Membership.member_id == member_id, Membership.state != "cancelled",
                                 Membership.start_date <= end, Membership.end_date >= start)
    if exclude_id:
        q = q.where(Membership.id != exclude_id)
    return db.scalar(q.limit(1))


def default_start(db: Session, gym: Gym, member_id: str) -> date:
    today = today_in(gym.timezone)
    latest = db.scalar(select(Membership).where(Membership.member_id == member_id, Membership.state != "cancelled")
                       .order_by(Membership.end_date.desc()).limit(1))
    if latest and latest.end_date >= today:
        return latest.end_date + timedelta(days=1)
    return today


def create_membership(db: Session, gym: Gym, actor_id: str | None, member: GymMember, *, plan: Plan | None,
                      start: date, end: date | None = None, discount_paise: int = 0, plan_name: str | None = None,
                      price_paise: int | None = None, source: str = "manual") -> Membership:
    joining_fee = gst = 0
    if plan:
        if not plan.active:
            raise ApiError(422, "PLAN_INACTIVE")
        base = plan_price(plan)
        if discount_paise < 0 or discount_paise > base:
            raise validation({"discount_paise": "Discount can’t be more than the plan price."})
        end = end or start + timedelta(days=plan.duration_days - 1)
        if plan.joining_fee_paise and source == "manual" and not db.scalar(
                select(Membership.id).where(Membership.member_id == member.id).limit(1)):
            joining_fee = plan.joining_fee_paise
        taxable = base - discount_paise + joining_fee
        if plan.gst_mode == "exclusive" and plan.gst_rate:
            gst = round(taxable * plan.gst_rate / 100)
        price = taxable + gst
        name = plan.name
    else:
        if not end:
            raise validation({"end_date": "End date is needed for a custom plan."})
        price = max(0, (price_paise or 0) - discount_paise)
        name = plan_name or "Custom plan"
    if end < start:
        raise validation({"end_date": "End date is before the start date."})
    clash = overlapping(db, member.id, start, end)
    if clash:
        raise ApiError(409, "OVERLAPPING_MEMBERSHIP", None,
                       {"membership_id": clash.id, "start_date": dstr(clash.start_date), "end_date": dstr(clash.end_date)})
    today = today_in(gym.timezone)
    m = Membership(gym_id=gym.id, member_id=member.id, plan_id=plan.id if plan else None, plan_name=name,
                   price_paise=price, discount_paise=discount_paise, joining_fee_paise=joining_fee, gst_paise=gst,
                   start_date=start, end_date=end,
                   state="scheduled" if start > today else "active", source=source, created_by=actor_id)
    db.add(m)
    db.flush()
    return m


def check_duplicate_reference(db: Session, gym_id: str, method: str, reference: str | None):
    if not reference:
        return
    dup = db.scalar(select(Payment).where(Payment.gym_id == gym_id, Payment.method == method,
                                          Payment.reference == reference, Payment.status == "recorded"))
    if dup:
        raise ApiError(409, "DUPLICATE_REFERENCE", None, {"payment_id": dup.id, "receipt_no": dup.receipt_no})


def validate_reference(method: str, reference: str | None) -> str | None:
    if reference is None:
        return None
    ref = reference.strip().replace(" ", "")
    if not ref:
        return None
    if method == "upi" and not (ref.isdigit() and len(ref) == 12):
        raise validation({"reference": "UPI reference (UTR) should be 12 digits."})
    if len(ref) > 100:
        raise validation({"reference": "Reference is too long."})
    return ref


def validate_paid_at(gym: Gym, paid_at: datetime | None) -> datetime:
    now = utcnow()
    if paid_at is None:
        return now
    if paid_at.tzinfo is None:
        from app.utils import tz
        paid_at = paid_at.replace(tzinfo=tz(gym.timezone))
    if paid_at > now + timedelta(minutes=5):
        raise validation({"paid_at": "Payment date can’t be in the future."})
    if paid_at < now - timedelta(days=30):
        raise validation({"paid_at": "Payments can be back-dated up to 30 days."})
    return paid_at


def paid_at_for_plan(gym: Gym, ms: Membership | None, paid_at: datetime | None) -> datetime:
    """Date of a payment taken together with a plan (add member / renew / assign / group).
    A date the desk typed wins; otherwise it is the plan's start date when the plan started earlier
    (same time of day as now), and now when the plan starts today or later."""
    if paid_at is not None or ms is None:
        return validate_paid_at(gym, paid_at)
    now, today = utcnow(), today_in(gym.timezone)
    if ms.start_date < today:
        return now - timedelta(days=(today - ms.start_date).days)
    return now


def add_payment(db: Session, gym: Gym, actor_id: str | None, member: GymMember, membership: Membership | None,
                amount_paise: int, method: str, reference: str | None = None, paid_at: datetime | None = None,
                note: str | None = None, source: str = "manual") -> Payment:
    if amount_paise <= 0:
        raise validation({"amount_paise": "Amount must be more than zero."})
    paid_at = paid_at or utcnow()
    p = Payment(gym_id=gym.id, member_id=member.id, membership_id=membership.id if membership else None,
                amount_paise=amount_paise, method=method, reference=reference, paid_at=paid_at, source=source,
                receipt_no=next_receipt_no(db, gym.id, paid_at), status="recorded", recorded_by=actor_id, note=note)
    db.add(p)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        if "uq_payment_reference" in str(e.orig):
            raise ApiError(409, "DUPLICATE_REFERENCE")
        raise
    return p


# ---------------- serializers ----------------
def ser_user(u: User) -> dict:
    return {"id": u.id, "name": u.name, "phone": u.phone, "email": u.email, "dob": dstr(u.dob), "gender": u.gender,
            "photo_url": storage.public_url(u.photo_url), "weekly_goal": u.weekly_goal,
            "first_login_at": iso(u.first_login_at), "deletion_requested_at": iso(u.deletion_requested_at),
            "is_platform_admin": bool(u.is_platform_admin), "show_on_leaderboard": bool(u.show_on_leaderboard)}


def ser_gym(g: Gym, full: bool = True) -> dict:
    d = {"id": g.id, "name": g.name, "code_prefix": g.code_prefix, "status": g.status,
         "status_reason": g.status_reason, "area": g.area, "city": g.city, "logo_url": storage.public_url(g.logo_url),
         "phone": g.phone, "org_id": g.org_id or g.id}
    if full:
        d.update({"owner_user_id": g.owner_user_id, "email": g.email, "address_line": g.address_line,
                  "state": g.state, "pincode": g.pincode, "lat": float(g.lat) if g.lat is not None else None,
                  "lng": float(g.lng) if g.lng is not None else None, "hours": g.hours, "timezone": g.timezone,
                  "gstin": g.gstin, "settings": gym_settings(g), "approved_at": iso(g.approved_at),
                  "created_at": iso(g.created_at)})
    return d


def plan_price(p: Plan) -> int:
    return p.offer_price_paise if p.offer_price_paise is not None else p.price_paise


def ser_plan(p: Plan, active_members: int | None = None) -> dict:
    d = {"id": p.id, "name": p.name, "duration_days": p.duration_days, "price_paise": p.price_paise,
         "offer_price_paise": p.offer_price_paise, "effective_price_paise": plan_price(p),
         "joining_fee_paise": p.joining_fee_paise, "gst_mode": p.gst_mode, "gst_rate": p.gst_rate,
         "facilities": p.facilities or [], "description": p.description, "freeze_allowed": p.freeze_allowed,
         "max_freeze_days": p.max_freeze_days, "cancellation_policy": p.cancellation_policy,
         "refund_policy": p.refund_policy, "active": p.active, "sort_order": p.sort_order}
    if active_members is not None:
        d["active_members"] = active_members
    return d


def ser_membership(m: Membership, gym: Gym, db: Session | None = None, status: str | None = None,
                   due: int | None = None, can_pay: bool = True, freezes: list | None = None) -> dict:
    today = today_in(gym.timezone)
    if freezes is None and db is not None:
        freezes = freezes_of(db, m.id)
    st = status or membership_status(m, today, freezes)
    d = {"id": m.id, "member_id": m.member_id, "plan_id": m.plan_id, "plan_name": m.plan_name,
         "start_date": dstr(m.start_date), "end_date": dstr(m.end_date), "state": m.state, "status": st,
         "days_left": days_left(m, st, today), "source": m.source, "cancelled_at": iso(m.cancelled_at),
         "cancel_reason": m.cancel_reason, "suspended_at": iso(m.suspended_at), "suspend_reason": m.suspend_reason,
         "created_at": iso(m.created_at), "group_id": m.group_id}
    if m.group_id and db is not None:  # the other people this plan was assigned together with
        d["group"] = [{"member_id": mid, "name": nm} for mid, nm in db.execute(
            select(GymMember.id, GymMember.display_name).join(Membership, Membership.member_id == GymMember.id)
            .where(Membership.group_id == m.group_id, Membership.id != m.id)).all()]
    if freezes is not None:
        d["freezes"] = [{"id": f.id, "from_date": dstr(f.from_date), "to_date": dstr(f.to_date), "days": f.days,
                         "reason": f.reason, "note": f.note, "ended_at": iso(f.ended_at)} for f in freezes]
    if can_pay:
        d["price_paise"] = m.price_paise
        d["discount_paise"] = m.discount_paise
        d["joining_fee_paise"] = m.joining_fee_paise
        d["gst_paise"] = m.gst_paise
        if due is not None:
            d["due_paise"] = due
    return d


def ser_payment(p: Payment, member_name: str | None = None, plan_name: str | None = None,
                recorder_name: str | None = None) -> dict:
    return {"id": p.id, "receipt_no": p.receipt_no, "member": {"id": p.member_id, "name": member_name},
            "membership": {"id": p.membership_id, "plan_name": plan_name} if p.membership_id else None,
            "amount_paise": p.amount_paise, "method": p.method, "reference": p.reference, "paid_at": iso(p.paid_at),
            "source": p.source, "status": p.status, "void_reason": p.void_reason, "voided_at": iso(p.voided_at),
            "recorded_by": {"id": p.recorded_by, "name": recorder_name}, "note": p.note}


def pt_out(m: GymMember) -> dict:
    return {"enabled": bool(m.pt_enabled), "fee_paise": m.pt_fee_paise, "trainer": m.pt_trainer}


def ser_member_basic(m: GymMember, u: User, include_notes: bool = True) -> dict:
    d = {"id": m.id, "member_code": m.member_code, "name": m.display_name, "phone": u.phone, "email": u.email,
         "dob": dstr(u.dob), "gender": u.gender, "photo_url": storage.public_url(u.photo_url),
         "emergency_contact": m.emergency_contact, "status": m.status, "source": m.source,
         "joined_on": dstr(m.joined_on), "joined_at": dstr(m.joined_on), "last_checkin_at": iso(m.last_checkin_at),
         "last_visit_at": iso(m.last_checkin_at), "on_app": u.first_login_at is not None,
         "claimed_at": iso(m.claimed_at), "claim_disputed_at": iso(m.claim_disputed_at), "created_at": iso(m.created_at),
         "wa_opted_out": m.wa_opted_out_at is not None, "wa_opted_out_at": iso(m.wa_opted_out_at), "wa_opt_source": m.wa_opt_source,
         "pt": pt_out(m)}
    if include_notes:
        d["notes"] = m.notes
    return d


def user_names(db: Session, ids) -> dict[str, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {u.id: u.name for u in db.scalars(select(User).where(User.id.in_(ids)))}


def staff_names_for_gym(db: Session, gym_id: str, ids) -> dict[str, str]:
    return user_names(db, ids)



# ---------------- credit ----------------
def settle_credit(db: Session, gym: Gym, member: GymMember, actor_id: str | None = None,
                  prefer_membership_id: str | None = None) -> int:
    """Use the member's credit to pay their dues (preferred membership first, then oldest first).
    If credit went negative (a credit payment was voided), reverse the newest applications.
    Returns paise applied. Caller commits."""
    from app.models import CreditApplication
    from app.services.rules import credit_balance, membership_due
    bal = credit_balance(db, member.id)
    if bal < 0:
        for ca in db.scalars(select(CreditApplication).where(CreditApplication.member_id == member.id,
                                                             CreditApplication.reversed_at.is_(None))
                             .order_by(CreditApplication.created_at.desc())):
            if bal >= 0:
                break
            ca.reversed_at = utcnow()
            bal += ca.amount_paise
        db.flush()
        return 0
    if bal <= 0:
        return 0
    ms_list = list(db.scalars(select(Membership).where(Membership.member_id == member.id,
                                                       Membership.state != "cancelled")
                              .order_by(Membership.start_date)))
    ms_list.sort(key=lambda m: (m.id != prefer_membership_id, m.start_date))
    applied = 0
    for ms in ms_list:
        if bal <= 0:
            break
        due = membership_due(db, ms)
        if due <= 0:
            continue
        use = min(due, bal)
        db.add(CreditApplication(gym_id=gym.id, member_id=member.id, membership_id=ms.id, amount_paise=use,
                                 created_by=actor_id))
        db.flush()
        bal -= use
        applied += use
    if applied:
        from app.services.audit import audit
        audit(db, gym.id, actor_id, "credit.apply", "member", member.id,
              f"Used ₹{applied / 100:,.0f} credit towards {member.display_name}’s dues")
    return applied


def price_preview(db: Session, plan: Plan, member: GymMember | None, discount_paise: int = 0) -> dict:
    """What a new membership on this plan would cost this member (same maths as create_membership)."""
    base = plan_price(plan)
    first = member is None or not db.scalar(select(Membership.id).where(Membership.member_id == member.id).limit(1))
    joining = plan.joining_fee_paise if (plan.joining_fee_paise and first) else 0
    taxable = base - discount_paise + joining
    gst = round(taxable * plan.gst_rate / 100) if plan.gst_mode == "exclusive" and plan.gst_rate else 0
    return {"base_paise": base, "discount_paise": discount_paise, "joining_fee_paise": joining, "gst_paise": gst,
            "total_paise": taxable + gst}
