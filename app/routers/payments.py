"""§9 — payments list, record, void, receipt."""
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, Idem, current_user, idem_header, load_gym_ctx, payments_ctx
from app.errors import ApiError
from app.models import Gym, GymMember, Membership, Payment, User
from app.schemas import PaymentCreate, VoidIn
from app.services.audit import audit
from app.services.core import (add_payment, check_duplicate_reference, gym_settings, ser_payment, user_names,
                               validate_paid_at, validate_reference)
from app.services.receipt import render_receipt_pdf
from app.services.rules import membership_due
from app.utils import amount_in_words, clamp_limit, decode_cursor, encode_cursor, local_midnight_utc, rupees, tz, \
    utcnow

router = APIRouter(tags=["Payments"])


@router.get("/gyms/{gymId}/payments", summary="Payments list with totals (payments permission)",
            description="`from`/`to` are dates (YYYY-MM-DD, gym timezone, inclusive). Totals cover recorded payments matching the filters.")
def list_payments(from_: date | None = Query(None, alias="from"), to: date | None = None,
                  method: Literal["cash", "upi", "card", "bank"] | None = None, member_id: str | None = None,
                  status: Literal["recorded", "voided"] | None = None, source: Literal["manual", "import", "app"] | None = None,
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


@router.post("/gyms/{gymId}/payments", status_code=201, summary="Record payment (payments permission)",
             description="Requires `Idempotency-Key`. 409 DUPLICATE_REFERENCE (same UTR/ref) · 409 POSSIBLE_DUPLICATE "
                         "(same member + amount within 10 min; resend with confirm_duplicate=true) · 422 OVERPAYMENT "
                         "(unless as_credit=true: the extra becomes member credit). Omit membership_id for an advance/credit.")
def record_payment(body: PaymentCreate, ctx: GymCtx = Depends(payments_ctx), db: Session = Depends(get_db),
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


@router.post("/payments/{id}/void", summary="Void payment (owner only). Voided payments stay visible")
def void_payment(id: str, body: VoidIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _payment_ctx(db, user, id)
    ctx = load_gym_ctx(db, user, p.gym_id)
    ctx.require_owner()
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
        ctx.require_payments()
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
