"""§9 — payments list, record, void, receipt."""
import csv
import io
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, Idem, current_user, idem_header, load_gym_ctx, payments_ctx, perm_ctx
from app.errors import ApiError, validation
from app.models import Gym, GymMember, Membership, Payment, User
from app.schemas import PaymentCreate, VoidIn
from app.services.audit import audit
from app.services.core import (add_payment, check_duplicate_reference, gym_settings, ser_payment, user_names,
                               validate_paid_at, validate_reference)
from app.services.receipt import render_receipt_pdf
from app.services.rules import membership_due
from app.utils import today_in, amount_in_words, clamp_limit, decode_cursor, encode_cursor, local_midnight_utc, rupees, tz, \
    utcnow

from app.responses import DocRoute, ERRORS, PaymentCreateOut, PaymentListOut, PaymentStatsOut, PaymentWrapOut

router = APIRouter(tags=["Payments"], route_class=DocRoute, responses=ERRORS)


def _q_filter(gym_id: str, q: str):
    from app.routers.members import phone_digits
    ql = q.strip()
    like = f"%{ql}%"
    member_q = select(GymMember.id).join(User, User.id == GymMember.user_id).where(GymMember.gym_id == gym_id)
    d = phone_digits(ql)
    who = or_(GymMember.display_name.ilike(like), GymMember.member_code.ilike(like),
              *([User.phone.like(f"%{d}%")] if len(d) >= 3 else []))
    return or_(Payment.member_id.in_(member_q.where(who)), Payment.reference.ilike(like), Payment.receipt_no.ilike(like))


@router.get("/gyms/{gymId}/payments", response_model=PaymentListOut, summary="Payments list with totals (payments permission)",
            description="`from`/`to` are dates (YYYY-MM-DD, gym timezone, inclusive). Totals cover recorded payments matching the filters.")
def list_payments(from_: date | None = Query(None, alias="from"), to: date | None = None,
                  method: Literal["cash", "upi", "card", "bank"] | None = None, member_id: str | None = None,
                  status: Literal["recorded", "voided"] | None = None, source: Literal["manual", "import", "app"] | None = None,
                  q: str | None = Query(None, description="Member name, phone, UTR/reference or receipt no"),
                  limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                  ctx: GymCtx = Depends(payments_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    conds = [Payment.gym_id == gym.id]
    if from_:
        conds.append(Payment.paid_at >= local_midnight_utc(from_, gym.timezone))
    if to:
        conds.append(Payment.paid_at < local_midnight_utc(to + timedelta(days=1), gym.timezone))
    if method:
        conds.append(Payment.method == method)
    if member_id:
        conds.append(Payment.member_id == member_id)
    if source:
        conds.append(Payment.source == source)
    if q and q.strip():
        conds.append(_q_filter(gym.id, q))
    base = list(conds)
    if status:
        conds.append(Payment.status == status)

    tot_rows = db.execute(select(Payment.method, func.count(), func.sum(Payment.amount_paise))
                          .where(*base, Payment.status == "recorded").group_by(Payment.method)).all()
    by_method = {"cash": 0, "upi": 0, "card": 0, "bank": 0}
    count = 0
    for mth, n, s in tot_rows:
        by_method[mth] = int(s or 0)
        count += n

    q = select(Payment).where(*conds)
    c = decode_cursor(cursor)
    if c:
        t = datetime.fromisoformat(c["t"])
        q = q.where(or_(Payment.paid_at < t, and_(Payment.paid_at == t, Payment.id < c["id"])))
    lim = clamp_limit(limit)
    rows = db.scalars(q.order_by(Payment.paid_at.desc(), Payment.id.desc()).limit(lim + 1)).all()
    nxt = None
    if len(rows) > lim:
        rows = rows[:lim]
        nxt = encode_cursor({"t": rows[-1].paid_at.isoformat(), "id": rows[-1].id})
    members = {m.id: m.display_name for m in db.scalars(select(GymMember).where(
        GymMember.id.in_([p.member_id for p in rows] or [""])))}
    plans = {m.id: m.plan_name for m in db.scalars(select(Membership).where(
        Membership.id.in_([p.membership_id for p in rows if p.membership_id] or [""])))}
    names = user_names(db, [p.recorded_by for p in rows])
    return {"items": [ser_payment(p, members.get(p.member_id), plans.get(p.membership_id), names.get(p.recorded_by))
                      for p in rows],
            "totals": {"collected_paise": sum(by_method.values()), "count": count, "by_method": by_method},
            "next_cursor": nxt}


@router.post("/gyms/{gymId}/payments", response_model=PaymentCreateOut, status_code=201, summary="Record payment (payments permission)",
             description="Requires `Idempotency-Key`. 409 DUPLICATE_REFERENCE (same UTR/ref) · 409 POSSIBLE_DUPLICATE "
                         "(same member + amount within 10 min; resend with confirm_duplicate=true) · 422 OVERPAYMENT "
                         "(unless as_credit=true: the extra becomes member credit). Omit membership_id for an advance/credit.")
def record_payment(body: PaymentCreate, ctx: GymCtx = Depends(perm_ctx("payments.record")), db: Session = Depends(get_db),
                   idempotency_key: str | None = Depends(idem_header)):
    idem = Idem(db, ctx.user, idempotency_key, f"POST /gyms/{ctx.gym.id}/payments")
    if idem.replay:
        return idem.replay
    gym = ctx.gym
    member = db.get(GymMember, body.member_id)
    if not member or member.gym_id != gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    ms = None
    if body.membership_id:
        ms = db.get(Membership, body.membership_id)
        if not ms or ms.member_id != member.id:
            raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
        if ms.state == "cancelled":
            raise ApiError(409, "NOT_ACTIVE", "This membership is cancelled.")
    ref = validate_reference(body.method, body.reference)
    paid_at = validate_paid_at(gym, body.paid_at)
    check_duplicate_reference(db, gym.id, body.method, ref)
    if not body.confirm_duplicate:
        recent = db.scalar(select(Payment).where(Payment.member_id == member.id, Payment.amount_paise == body.amount_paise,
                                                 Payment.status == "recorded",
                                                 Payment.created_at >= utcnow() - timedelta(minutes=10)).limit(1))
        if recent:
            raise ApiError(409, "POSSIBLE_DUPLICATE", None, {"payment_id": recent.id, "receipt_no": recent.receipt_no,
                                                              "paid_at": recent.paid_at.isoformat()})
    main_amount, credit_amt = body.amount_paise, 0
    if ms:
        due = membership_due(db, ms)
        if main_amount > due:
            if not body.as_credit:
                raise ApiError(422, "OVERPAYMENT", None, {"due_paise": due})
            credit_amt, main_amount = main_amount - due, due
    pay = credit = None
    if main_amount > 0:
        pay = add_payment(db, gym, ctx.user.id, member, ms, main_amount, body.method, ref, paid_at, body.note)
        audit(db, gym.id, ctx.user.id, "payment.record", "payment", pay.id,
              f"Recorded {rupees(pay.amount_paise)} {pay.method.upper()} from {member.display_name}")
    if credit_amt > 0:
        credit = add_payment(db, gym, ctx.user.id, member, None, credit_amt, body.method,
                             ref if pay is None else None, paid_at, body.note or "Credit from overpayment")
        audit(db, gym.id, ctx.user.id, "payment.record", "payment", credit.id,
              f"Recorded {rupees(credit_amt)} {credit.method.upper()} as credit for {member.display_name}")
    primary = pay or credit
    out = {"payment": ser_payment(primary, member.display_name, ms.plan_name if (ms and primary.membership_id) else None,
                                  ctx.user.name),
           "credit_payment": ser_payment(credit, member.display_name, None, ctx.user.name) if (credit and pay) else None,
           "due_paise": membership_due(db, ms) if ms else 0}
    return idem.respond(201, out)


def _payment_ctx(db: Session, user: User, payment_id: str):
    p = db.get(Payment, payment_id)
    if not p:
        raise ApiError(404, "NOT_FOUND", "That payment wasn’t found.")
    return p


@router.post("/payments/{id}/void", response_model=PaymentWrapOut, summary="Void payment (owner only). Voided payments stay visible")
def void_payment(id: str, body: VoidIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _payment_ctx(db, user, id)
    ctx = load_gym_ctx(db, user, p.gym_id)
    ctx.require("payments.void")
    if p.status == "voided":
        raise ApiError(409, "NOT_ACTIVE", "This payment is already voided.")
    p.status, p.void_reason, p.voided_by, p.voided_at = "voided", body.reason, user.id, utcnow()
    member = db.get(GymMember, p.member_id)
    audit(db, p.gym_id, user.id, "payment.void", "payment", p.id,
          f"Voided {rupees(p.amount_paise)} {p.method.upper()} from {member.display_name}: {body.reason}",
          {"status": "recorded"}, {"status": "voided"})
    db.commit()
    names = user_names(db, [p.recorded_by])
    ms = db.get(Membership, p.membership_id) if p.membership_id else None
    return {"payment": ser_payment(p, member.display_name, ms.plan_name if ms else None, names.get(p.recorded_by))}


@router.get("/payments/{id}/receipt", summary="Receipt PDF (or ?format=json)",
            description="Staff with payments permission at the gym, or the member who paid.",
            responses={200: {"content": {"application/pdf": {}, "application/json": {}}}})
def receipt(id: str, format: Literal["pdf", "json"] = "pdf", user: User = Depends(current_user),
            db: Session = Depends(get_db)):
    p = _payment_ctx(db, user, id)
    member = db.get(GymMember, p.member_id)
    if member.user_id != user.id:
        ctx = load_gym_ctx(db, user, p.gym_id)
        ctx.require("payments.view")
    gym = db.get(Gym, p.gym_id)
    ms = db.get(Membership, p.membership_id) if p.membership_id else None
    recorder = db.get(User, p.recorded_by) if p.recorded_by else None
    d = {"id": p.id, "receipt_no": p.receipt_no, "status": p.status, "void_reason": p.void_reason,
         "gym": {"name": gym.name, "gstin": gym.gstin, "address_line": gym.address_line, "area": gym.area,
                 "city": gym.city, "pincode": gym.pincode, "phone": gym.phone},
         "member": {"id": member.id, "name": member.display_name, "member_code": member.member_code},
         "plan_name": ms.plan_name if ms else "Advance / credit",
         "membership": {"start_date": ms.start_date.isoformat(), "end_date": ms.end_date.isoformat()} if ms else None,
         "amount_paise": p.amount_paise, "amount_in_words": amount_in_words(p.amount_paise), "method": p.method,
         "reference": p.reference, "paid_at": p.paid_at.isoformat(),
         "paid_at_local": p.paid_at.astimezone(tz(gym.timezone)).strftime("%d %b %Y, %I:%M %p"),
         "recorded_by": recorder.name if recorder else None, "footer": gym_settings(gym).get("receipt_footer")}
    if format == "json":
        return d
    pdf = render_receipt_pdf(d)
    fname = p.receipt_no.replace("/", "-")
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{fname}.pdf"'})


# ---------------- revenue stats & export ----------------
def _periods(range_: str, group: str, today: date) -> list[tuple[date, date, str]]:
    n, unit = int(range_[:-1]), range_[-1]
    if unit == "m":
        first = date(today.year, today.month, 1)
        y, mo = first.year, first.month - (n - 1)
        while mo <= 0:
            y, mo = y - 1, mo + 12
        start = date(y, mo, 1)
    else:
        start = today - timedelta(days=n - 1)
    out, cur = [], start
    while cur <= today:
        if group == "month":
            nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
            out.append((cur, nxt, cur.strftime("%Y-%m")))
        elif group == "week":
            ws = cur - timedelta(days=cur.weekday())
            nxt = ws + timedelta(days=7)
            out.append((max(cur, ws), nxt, ws.isoformat()))
        else:
            nxt = cur + timedelta(days=1)
            out.append((cur, nxt, cur.isoformat()))
        cur = nxt
    return out


@router.get("/gyms/{gymId}/payments/stats", response_model=PaymentStatsOut,
            summary="Revenue time-series: totals per period by method and source (payments.view)",
            description="`range`: 7d · 30d · 90d · 3m · 6m · 12m · 24m. `group`: day · week · month. "
                        "Recorded (non-voided) payments; periods in the gym’s timezone. "
                        "`totals.collected_excl_import_paise` leaves imported history out.")
def payment_stats(range: Literal["7d", "30d", "90d", "3m", "6m", "12m", "24m"] = "12m",
                  group: Literal["day", "week", "month"] = "month",
                  ctx: GymCtx = Depends(payments_ctx), db: Session = Depends(get_db)):
    gym = ctx.gym
    today = today_in(gym.timezone)
    periods = _periods(range, group, today)
    if group == "day" and len(periods) > 400:
        raise validation({"group": "Too many days; use week or month for long ranges."})
    start_utc = local_midnight_utc(periods[0][0], gym.timezone)
    rows = db.execute(select(Payment.paid_at, Payment.amount_paise, Payment.method, Payment.source).where(
        Payment.gym_id == gym.id, Payment.status == "recorded", Payment.paid_at >= start_utc)).all()
    z = tz(gym.timezone)
    series = [{"period": label, "start": s.isoformat(), "collected_paise": 0, "count": 0,
               "by_method": {"cash": 0, "upi": 0, "card": 0, "bank": 0}, "by_source": {"manual": 0, "import": 0, "app": 0}}
              for s, _, label in periods]
    starts = [s for s, _, _ in periods]
    import bisect
    for paid_at, amt, mth, src in rows:
        d = paid_at.astimezone(z).date()
        i = bisect.bisect_right(starts, d) - 1
        if i < 0 or d >= periods[i][1]:
            continue
        pt = series[i]
        pt["collected_paise"] += amt
        pt["count"] += 1
        pt["by_method"][mth] += amt
        pt["by_source"][src] += amt
    tot = sum(p["collected_paise"] for p in series)
    imp = sum(p["by_source"]["import"] for p in series)
    return {"range": range, "group": group, "from": periods[0][0].isoformat(), "to": today.isoformat(),
            "series": series,
            "totals": {"collected_paise": tot, "collected_excl_import_paise": tot - imp,
                       "count": sum(p["count"] for p in series),
                       "by_method": {k: sum(p["by_method"][k] for p in series) for k in ("cash", "upi", "card", "bank")},
                       "by_source": {k: sum(p["by_source"][k] for p in series) for k in ("manual", "import", "app")}}}


@router.get("/gyms/{gymId}/payments/export.csv", summary="Export payments CSV (payments.view + reports.export)",
            description="Same filters as the payments list; exports every matching row.",
            responses={200: {"content": {"text/csv": {}}}})
def export_payments(from_: date | None = Query(None, alias="from"), to: date | None = None,
                    method: Literal["cash", "upi", "card", "bank"] | None = None, member_id: str | None = None,
                    status: Literal["recorded", "voided"] | None = None,
                    source: Literal["manual", "import", "app"] | None = None, q: str | None = None,
                    ctx: GymCtx = Depends(payments_ctx), db: Session = Depends(get_db)):
    ctx.require("reports.export")
    gym = ctx.gym
    conds = [Payment.gym_id == gym.id]
    if from_:
        conds.append(Payment.paid_at >= local_midnight_utc(from_, gym.timezone))
    if to:
        conds.append(Payment.paid_at < local_midnight_utc(to + timedelta(days=1), gym.timezone))
    for col, val in ((Payment.method, method), (Payment.member_id, member_id), (Payment.status, status),
                     (Payment.source, source)):
        if val:
            conds.append(col == val)
    if q and q.strip():
        conds.append(_q_filter(gym.id, q))
    rows = db.scalars(select(Payment).where(*conds).order_by(Payment.paid_at.desc()).limit(50000)).all()
    members = {m.id: m for m in db.scalars(select(GymMember).where(GymMember.id.in_({p.member_id for p in rows} or {""})))}
    plans = {m.id: m.plan_name for m in db.scalars(select(Membership).where(
        Membership.id.in_({p.membership_id for p in rows if p.membership_id} or {""})))}
    names = user_names(db, [p.recorded_by for p in rows])
    z = tz(gym.timezone)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Receipt no", "Paid at", "Member code", "Member", "Plan", "Amount (₹)", "Method", "Reference",
                "Source", "Status", "Void reason", "Recorded by", "Note"])
    for p in rows:
        m = members.get(p.member_id)
        w.writerow([p.receipt_no, p.paid_at.astimezone(z).strftime("%Y-%m-%d %H:%M"), m.member_code if m else "",
                    m.display_name if m else "", plans.get(p.membership_id, "Advance / credit"),
                    f"{p.amount_paise / 100:.2f}", p.method, p.reference or "", p.source, p.status,
                    p.void_reason or "", names.get(p.recorded_by) or "", p.note or ""])
    audit(db, gym.id, ctx.user.id, "export.payments", None, None, f"Exported {len(rows)} payments")
    db.commit()
    name = f"{gym.code_prefix}-payments-{today_in(gym.timezone).isoformat()}.csv"
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
