"""Gym UPI payments (inspired by YouPI-PG's manual UPI flow): members pay straight into the gym's own UPI ID.

Flow:  owner adds UPI ID  →  a payment request is created (staff at the desk, or the member for their dues)
       →  member pays via the upi:// link / QR (exact amount, our ref in `tr`, note in `tn`)
       →  member submits the UTR (claim)  →  staff check their bank/UPI app  →  verify (records the Payment +
          receipt automatically) or reject.
A UTR, screenshot or callback is a *claim*, not proof of settlement — that's why a human verifies.
"""
import re
import secrets
from datetime import timedelta
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, current_user, gym_ctx, perm_ctx
from app.errors import ApiError, validation
from app.models import Gym, GymMember, GymStaff, GymUpiAccount, Membership, Payment, Plan, UpiPaymentRequest, User
from app.responses import (DocRoute, ERRORS, MyUpiInfoOut, UpiAccountListOut, UpiAccountOut, UpiRequestListOut, UpiRequestOut)
from app.services.audit import audit
from app.services.cache import rate_limit
from app.services.core import (add_payment, check_duplicate_reference, create_membership, default_start, price_preview,
                               ser_membership, settle_credit)
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.rules import credit_balance, load_states, membership_due
from app.utils import clamp_limit, decode_cursor, encode_cursor, iso, rupees, utcnow

router = APIRouter(tags=["UPI payments"], route_class=DocRoute, responses=ERRORS)
me_router = APIRouter(prefix="/me", tags=["UPI payments"], route_class=DocRoute, responses=ERRORS)

VPA_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z][a-zA-Z0-9.\-]{1,63}$")
UTR_RE = re.compile(r"^\d{12}$")


# ---------------- schemas ----------------
class UpiAccountIn(BaseModel):
    vpa: str = Field(..., examples=["musclemen@okhdfcbank"], description="UPI ID that receives the money")
    payee_name: str = Field(..., min_length=2, max_length=120, description="Name on the bank account (shown in UPI apps)")
    label: str | None = Field(None, max_length=60, examples=["HDFC current a/c"])
    is_default: bool = True


class UpiAccountPatch(BaseModel):
    payee_name: str | None = Field(None, min_length=2, max_length=120)
    label: str | None = Field(None, max_length=60)
    is_default: bool | None = None
    active: bool | None = None


class UpiRequestIn(BaseModel):
    member_id: str
    plan_id: str | None = Field(None, description="Renewal: the membership on this plan is created when you verify")
    membership_id: str | None = Field(None, description="Defaults to the member’s current membership with dues")
    amount_paise: int | None = Field(None, gt=0, description="Defaults to the amount due")
    note: str | None = Field(None, max_length=50)
    upi_account_id: str | None = Field(None, description="Defaults to the gym’s default UPI ID")
    expires_in_min: int = Field(60, ge=5, le=1440)


class MyUpiRequestIn(BaseModel):
    member_id: str | None = Field(None, description="Which gym membership (omit if you have one gym)")
    membership_id: str | None = None
    amount_paise: int | None = Field(None, gt=0, description="Defaults to your total dues at that gym")


class RenewIn(BaseModel):
    member_id: str | None = Field(None, description="Which gym membership (omit if you have one gym)")
    plan_id: str


class ClaimIn(BaseModel):
    utr: str = Field(..., description="12-digit UPI transaction reference (UTR) from the payment app")


class VerifyIn(BaseModel):
    utr: str | None = Field(None, description="Override / provide the UTR seen in the bank app")
    note: str | None = None


class RejectIn(BaseModel):
    reason: str = Field(..., min_length=1, examples=["No credit found in bank account"])


# ---------------- helpers ----------------
def clean_vpa(v: str) -> str:
    v = (v or "").strip()
    if not VPA_RE.match(v):
        raise validation({"vpa": "That doesn’t look like a UPI ID (e.g. name@okhdfcbank)."})
    return v.lower()


def clean_utr(u: str) -> str:
    u = re.sub(r"\s", "", u or "")
    if not UTR_RE.match(u):
        raise validation({"utr": "UTR / UPI reference should be 12 digits."})
    return u


def upi_links(r: UpiPaymentRequest) -> tuple[str, dict]:
    params = {"pa": r.vpa, "pn": r.payee_name, "am": f"{r.amount_paise / 100:.2f}", "cu": "INR", "tr": r.ref}
    if r.note:
        params["tn"] = r.note
    qs = urlencode(params, quote_via=lambda v, *a: quote(v, safe="@"))  # UPI apps expect a literal @
    return f"upi://pay?{qs}", {"generic": f"upi://pay?{qs}", "gpay": f"tez://upi/pay?{qs}",
                               "phonepe": f"phonepe://pay?{qs}", "paytm": f"paytmmp://pay?{qs}",
                               "bhim": f"bhim://upi/pay?{qs}"}


def refresh_status(r: UpiPaymentRequest) -> UpiPaymentRequest:
    if r.status == "pending" and r.expires_at < utcnow():
        r.status = "expired"
    return r


def ser_req(r: UpiPaymentRequest, member: GymMember | None = None, receipt_no: str | None = None,
            plan_name: str | None = None) -> dict:
    refresh_status(r)
    if plan_name is None and r.plan_id:
        from sqlalchemy.orm import object_session
        sess = object_session(r)
        p = sess.get(Plan, r.plan_id) if sess else None
        plan_name = p.name if p else None
    uri, links = upi_links(r)
    payable = r.status in ("pending", "expired")
    return {"id": r.id, "status": r.status, "amount_paise": r.amount_paise, "vpa": r.vpa, "payee_name": r.payee_name,
            "ref": r.ref, "note": r.note, "upi_uri": uri if payable else None, "qr_payload": uri if payable else None,
            "app_links": links if payable else None, "membership_id": r.membership_id,
            "member": {"id": member.id, "name": member.display_name, "member_code": member.member_code} if member else None,
            "utr": r.utr, "claimed_at": iso(r.claimed_at), "decided_at": iso(r.decided_at),
            "reject_reason": r.reject_reason, "payment_id": r.payment_id, "receipt_no": receipt_no,
            "created_via": r.created_via, "purpose": r.purpose or "dues", "plan_id": r.plan_id,
            "plan_name": plan_name, "result_membership_id": r.result_membership_id,
            "expires_at": iso(r.expires_at), "created_at": iso(r.created_at)}


def ser_acct(a: GymUpiAccount) -> dict:
    return {"id": a.id, "vpa": a.vpa, "payee_name": a.payee_name, "label": a.label, "is_default": a.is_default,
            "active": a.active, "created_at": iso(a.created_at)}


def default_account(db: Session, gym_id: str, account_id: str | None = None) -> GymUpiAccount:
    q = select(GymUpiAccount).where(GymUpiAccount.gym_id == gym_id, GymUpiAccount.active.is_(True))
    if account_id:
        a = db.scalar(q.where(GymUpiAccount.id == account_id))
        if not a:
            raise validation({"upi_account_id": "That UPI ID isn’t active for this gym."})
        return a
    a = db.scalar(q.order_by(GymUpiAccount.is_default.desc(), GymUpiAccount.created_at).limit(1))
    if not a:
        raise ApiError(409, "UPI_NOT_SET_UP", "This gym hasn’t added a UPI ID yet.")
    return a


def resolve_amount(db: Session, gym: Gym, member: GymMember, membership_id: str | None,
                   amount: int | None) -> tuple[Membership | None, int]:
    ms = None
    if membership_id:
        ms = db.get(Membership, membership_id)
        if not ms or ms.member_id != member.id:
            raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
        if ms.state == "cancelled":
            raise ApiError(409, "NOT_ACTIVE", "This membership is cancelled.")
    else:
        st = load_states(db, gym, [member.id])[member.id]
        due_ms = [(m, d) for m, _, d in st.memberships if d > 0]
        if st.current and any(m.id == st.current.id for m, _ in due_ms):
            ms = st.current
        elif due_ms:
            ms = min(due_ms, key=lambda x: x[0].start_date)[0]
    due = membership_due(db, ms) if ms else 0
    if amount is None:
        if not ms:
            st = load_states(db, gym, [member.id])[member.id]
            due = st.due_paise
        amount = due
    if amount <= 0:
        raise validation({"amount_paise": "Nothing is due. Enter an amount to collect."})
    return ms, amount


def new_request(db: Session, gym: Gym, member: GymMember, ms: Membership | None, amount: int, acct: GymUpiAccount,
                via: str, actor_id: str, note: str | None, expires_in_min: int,
                plan: Plan | None = None) -> UpiPaymentRequest:
    # one open request per member+amount+purpose: reuse it (double taps, retries)
    existing = db.scalar(select(UpiPaymentRequest).where(
        UpiPaymentRequest.member_id == member.id, UpiPaymentRequest.amount_paise == amount,
        UpiPaymentRequest.status == "pending", UpiPaymentRequest.expires_at > utcnow(),
        UpiPaymentRequest.upi_account_id == acct.id,
        UpiPaymentRequest.plan_id == plan.id if plan else UpiPaymentRequest.plan_id.is_(None)).limit(1))
    if existing:
        return existing
    label = f" {plan.name}" if plan else (f" {ms.plan_name}" if ms else "")
    note = (note or f"{gym.code_prefix} {member.member_code}{label}")[:50]
    r = UpiPaymentRequest(gym_id=gym.id, member_id=member.id, membership_id=ms.id if ms else None,
                          upi_account_id=acct.id, vpa=acct.vpa, payee_name=acct.payee_name, amount_paise=amount,
                          ref=f"DMBL{secrets.token_hex(6).upper()}", note=re.sub(r"[^\w .\-/]", "", note),
                          status="pending", created_via=via, created_by=actor_id,
                          purpose="renewal" if plan else "dues", plan_id=plan.id if plan else None,
                          expires_at=utcnow() + timedelta(minutes=expires_in_min))
    db.add(r)
    db.flush()
    return r


def _get_req(db: Session, ctx: GymCtx, rid: str) -> UpiPaymentRequest:
    r = db.get(UpiPaymentRequest, rid)
    if not r or r.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That UPI payment wasn’t found.")
    return refresh_status(r)


def _utr_taken(db: Session, gym_id: str, utr: str, exclude_req: str | None = None):
    if db.scalar(select(Payment.id).where(Payment.gym_id == gym_id, Payment.method == "upi",
                                          Payment.reference == utr, Payment.status == "recorded")):
        raise ApiError(409, "DUPLICATE_REFERENCE", "This UTR has already been recorded for a payment.")
    q = select(UpiPaymentRequest.id).where(UpiPaymentRequest.gym_id == gym_id, UpiPaymentRequest.utr == utr,
                                           UpiPaymentRequest.status.in_(("claimed", "verified")))
    if exclude_req:
        q = q.where(UpiPaymentRequest.id != exclude_req)
    if db.scalar(q):
        raise ApiError(409, "DUPLICATE_REFERENCE", "This UTR was already submitted for another payment.")


def _active_plan(db: Session, gym_id: str, plan_id: str) -> Plan:
    plan = db.get(Plan, plan_id)
    if not plan or plan.gym_id != gym_id:
        raise ApiError(404, "NOT_FOUND", "That plan wasn’t found.")
    if not plan.active:
        raise ApiError(422, "PLAN_INACTIVE")
    return plan


def payment_staff(db: Session, gym_id: str) -> list[str]:
    """Users at this gym who can verify payments (owner + roles with payments.record)."""
    from app.permissions import effective_permissions
    out = []
    for st in db.scalars(select(GymStaff).where(GymStaff.gym_id == gym_id, GymStaff.status == "active")):
        if "payments.record" in effective_permissions(st.role, st.staff_role, bool(st.can_record_payments),
                                                      st.permissions):
            out.append(st.user_id)
    return out


# ---------------- gym: UPI accounts ----------------
@router.get("/gyms/{gymId}/upi-accounts", response_model=UpiAccountListOut, summary="Gym UPI IDs (any staff)")
def list_accounts(include_inactive: bool = False, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    q = select(GymUpiAccount).where(GymUpiAccount.gym_id == ctx.gym.id)
    if not include_inactive:
        q = q.where(GymUpiAccount.active.is_(True))
    return {"items": [ser_acct(a) for a in db.scalars(q.order_by(GymUpiAccount.is_default.desc(),
                                                                 GymUpiAccount.created_at))]}


def _set_default(db: Session, gym_id: str, keep_id: str):
    for a in db.scalars(select(GymUpiAccount).where(GymUpiAccount.gym_id == gym_id, GymUpiAccount.id != keep_id)):
        a.is_default = False


@router.post("/gyms/{gymId}/upi-accounts", response_model=UpiAccountOut, status_code=201,
             summary="Add a UPI ID (settings.manage)",
             description="Use a **business / merchant** UPI ID where possible (PhonePe Business, Paytm for Business, "
                         "bank merchant VPA). Some UPI apps limit pre-filled-amount payments to personal UPI IDs.")
def add_account(body: UpiAccountIn, ctx: GymCtx = Depends(perm_ctx("settings.manage")), db: Session = Depends(get_db)):
    vpa = clean_vpa(body.vpa)
    a = db.scalar(select(GymUpiAccount).where(GymUpiAccount.gym_id == ctx.gym.id, GymUpiAccount.vpa == vpa))
    if a and a.active:
        raise ApiError(409, "UPI_EXISTS", "This UPI ID is already added.")
    first = not db.scalar(select(GymUpiAccount.id).where(GymUpiAccount.gym_id == ctx.gym.id,
                                                         GymUpiAccount.active.is_(True)).limit(1))
    if a:
        a.active, a.payee_name, a.label = True, body.payee_name.strip(), body.label
    else:
        a = GymUpiAccount(gym_id=ctx.gym.id, vpa=vpa, payee_name=body.payee_name.strip(), label=body.label,
                          created_by=ctx.user.id)
        db.add(a)
    a.is_default = body.is_default or first
    db.flush()
    if a.is_default:
        _set_default(db, ctx.gym.id, a.id)
    audit(db, ctx.gym.id, ctx.user.id, "upi.account_add", "upi_account", a.id, f"Added UPI ID {vpa}")
    db.commit()
    return ser_acct(a)


@router.patch("/gyms/{gymId}/upi-accounts/{accountId}", response_model=UpiAccountOut,
              summary="Edit / make default / disable a UPI ID (settings.manage)")
def patch_account(accountId: str, body: UpiAccountPatch, ctx: GymCtx = Depends(perm_ctx("settings.manage")),
                  db: Session = Depends(get_db)):
    a = db.get(GymUpiAccount, accountId)
    if not a or a.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That UPI ID wasn’t found.")
    data = body.model_dump(exclude_unset=True)
    for k in ("payee_name", "label", "active"):
        if k in data and data[k] is not None:
            setattr(a, k, data[k])
    if data.get("is_default"):
        a.is_default = True
        _set_default(db, ctx.gym.id, a.id)
    if a.active is False:
        a.is_default = False
    audit(db, ctx.gym.id, ctx.user.id, "upi.account_update", "upi_account", a.id, f"Updated UPI ID {a.vpa}")
    db.commit()
    return ser_acct(a)


@router.delete("/gyms/{gymId}/upi-accounts/{accountId}", status_code=204, summary="Remove a UPI ID (soft)")
def delete_account(accountId: str, ctx: GymCtx = Depends(perm_ctx("settings.manage")), db: Session = Depends(get_db)):
    a = db.get(GymUpiAccount, accountId)
    if not a or a.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That UPI ID wasn’t found.")
    a.active, a.is_default = False, False
    audit(db, ctx.gym.id, ctx.user.id, "upi.account_remove", "upi_account", a.id, f"Removed UPI ID {a.vpa}")
    db.commit()
    return Response(status_code=204)


# ---------------- gym: payment requests ----------------
@router.post("/gyms/{gymId}/upi-requests", response_model=UpiRequestOut, status_code=201,
             summary="Create a UPI payment request / QR at the desk (payments.record)",
             description="Returns `upi_uri` (render it as a QR) and `app_links` (GPay/PhonePe/Paytm deep links). "
                         "Amount defaults to what’s due. Re-posting the same member+amount returns the open request.")
def create_request(body: UpiRequestIn, ctx: GymCtx = Depends(perm_ctx("payments.record")), db: Session = Depends(get_db)):
    gym = ctx.gym
    member = db.get(GymMember, body.member_id)
    if not member or member.gym_id != gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    acct = default_account(db, gym.id, body.upi_account_id)
    plan = None
    if body.plan_id:
        plan = _active_plan(db, gym.id, body.plan_id)
        ms, amount = None, body.amount_paise or max(0, price_preview(db, plan, member)["total_paise"]
                                                     - max(0, credit_balance(db, member.id)))
        if amount <= 0:
            raise validation({"plan_id": "The member’s credit already covers this plan — renew from their profile."})
    else:
        ms, amount = resolve_amount(db, gym, member, body.membership_id, body.amount_paise)
    r = new_request(db, gym, member, ms, amount, acct, "staff", ctx.user.id, body.note, body.expires_in_min, plan)
    audit(db, gym.id, ctx.user.id, "upi.request", "upi_request", r.id,
          f"UPI request {rupees(amount)} for {member.display_name}")
    db.commit()
    return ser_req(r, member)


@router.get("/gyms/{gymId}/upi-requests", response_model=UpiRequestListOut,
            summary="UPI payments to review (payments.view). `status=claimed` = waiting for you to verify")
def list_requests(status: Literal["pending", "claimed", "verified", "rejected", "expired", "cancelled"] | None = None,
                  member_id: str | None = None, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                  ctx: GymCtx = Depends(perm_ctx("payments.view")), db: Session = Depends(get_db)):
    gym = ctx.gym
    # lazily expire stale pending requests
    db.query(UpiPaymentRequest).filter(UpiPaymentRequest.gym_id == gym.id, UpiPaymentRequest.status == "pending",
                                       UpiPaymentRequest.expires_at < utcnow()).update({"status": "expired"})
    db.commit()
    counts = dict(db.execute(select(UpiPaymentRequest.status, func.count()).where(
        UpiPaymentRequest.gym_id == gym.id).group_by(UpiPaymentRequest.status)).all())
    q = select(UpiPaymentRequest).where(UpiPaymentRequest.gym_id == gym.id)
    if status:
        q = q.where(UpiPaymentRequest.status == status)
    if member_id:
        q = q.where(UpiPaymentRequest.member_id == member_id)
    c = decode_cursor(cursor)
    if c:
        q = q.where(UpiPaymentRequest.id < c["id"])
    lim = clamp_limit(limit)
    rows = db.scalars(q.order_by(UpiPaymentRequest.id.desc()).limit(lim + 1)).all()
    nxt = encode_cursor({"id": rows[lim - 1].id}) if len(rows) > lim else None
    rows = rows[:lim]
    members = {m.id: m for m in db.scalars(select(GymMember).where(GymMember.id.in_({r.member_id for r in rows} or {""})))}
    receipts = {p.id: p.receipt_no for p in db.scalars(select(Payment).where(
        Payment.id.in_({r.payment_id for r in rows if r.payment_id} or {""})))}
    return {"items": [ser_req(r, members.get(r.member_id), receipts.get(r.payment_id)) for r in rows],
            "next_cursor": nxt, "counts": {k: int(v) for k, v in counts.items()}}


@router.get("/gyms/{gymId}/upi-requests/{requestId}", response_model=UpiRequestOut, summary="One UPI payment")
def get_request(requestId: str, ctx: GymCtx = Depends(perm_ctx("payments.view")), db: Session = Depends(get_db)):
    r = _get_req(db, ctx, requestId)
    p = db.get(Payment, r.payment_id) if r.payment_id else None
    return ser_req(r, db.get(GymMember, r.member_id), p.receipt_no if p else None)


@router.post("/gyms/{gymId}/upi-requests/{requestId}/verify", response_model=UpiRequestOut,
             summary="Verify: you saw the credit in your bank → records the payment + receipt (payments.record)",
             description="Allowed for claimed, pending or expired requests (members sometimes pay late). The payment is "
                         "recorded as method `upi`, source `app`, reference = UTR; any amount above what’s due becomes "
                         "member credit. 409 DUPLICATE_REFERENCE if the UTR was already used.")
def verify_request(requestId: str, body: VerifyIn, ctx: GymCtx = Depends(perm_ctx("payments.record")),
                   db: Session = Depends(get_db)):
    gym = ctx.gym
    r = _get_req(db, ctx, requestId)
    if r.status not in ("claimed", "pending", "expired"):
        raise ApiError(409, "NOT_ACTIVE", f"This UPI payment is already {r.status}.")
    utr = clean_utr(body.utr) if body.utr else (r.utr or None)
    if utr:
        _utr_taken(db, gym.id, utr, exclude_req=r.id)
        check_duplicate_reference(db, gym.id, "upi", utr)
    member = db.get(GymMember, r.member_id)
    main = apply_upi_payment(db, gym, member, r, utr, ctx.user.id, body.note)
    r.status, r.utr, r.decided_by, r.decided_at, r.payment_id = "verified", utr, ctx.user.id, utcnow(), main.id
    audit(db, gym.id, ctx.user.id, "upi.verify", "upi_request", r.id,
          f"Verified UPI {rupees(r.amount_paise)} from {member.display_name}" + (f" (UTR {utr})" if utr else ""))
    audit(db, gym.id, ctx.user.id, "payment.record", "payment", main.id,
          f"Recorded {rupees(r.amount_paise)} UPI from {member.display_name}")
    db.commit()
    msg = f"{gym.name} confirmed your UPI payment of {rupees(r.amount_paise)}. Receipt {main.receipt_no}."
    if r.result_membership_id:
        new_ms = db.get(Membership, r.result_membership_id)
        msg = (f"Renewed! Your {new_ms.plan_name} membership at {gym.name} runs "
               f"{new_ms.start_date.strftime('%d %b')} – {new_ms.end_date.strftime('%d %b %Y')}. Receipt {main.receipt_no}.")
    enqueue("push.send", send_push, member.user_id, "member", "Payment received", msg,
            {"type": "upi_verified", "request_id": r.id, "payment_id": main.id,
             "membership_id": r.result_membership_id or r.membership_id})
    return ser_req(r, member, main.receipt_no)


def apply_upi_payment(db: Session, gym: Gym, member: GymMember, r: UpiPaymentRequest, utr: str | None,
                      actor_id: str, extra_note: str | None = None) -> Payment:
    """Record a verified UPI payment so it lands on the member:
    renewal → create the membership first; then pay the target membership (or whatever is due),
    and anything left becomes credit that's immediately used for other dues."""
    ms = db.get(Membership, r.membership_id) if r.membership_id else None
    if ms and ms.state == "cancelled":
        ms = None
    if r.purpose == "renewal" and r.plan_id and not r.result_membership_id:
        plan = _active_plan(db, gym.id, r.plan_id)
        ms = create_membership(db, gym, actor_id, member, plan=plan, start=default_start(db, gym, member.id))
        r.membership_id = r.result_membership_id = ms.id
        audit(db, gym.id, actor_id, "membership.renew", "membership", ms.id,
              f"Renewed {member.display_name} on {ms.plan_name} via app UPI ({ms.start_date} → {ms.end_date})")
    if ms is None:  # no target: pay the oldest membership that has dues
        for cand in db.scalars(select(Membership).where(Membership.member_id == member.id,
                                                        Membership.state != "cancelled").order_by(Membership.start_date)):
            if membership_due(db, cand) > 0:
                ms = cand
                break
    to_ms = min(r.amount_paise, membership_due(db, ms)) if ms else 0
    paid_at = r.claimed_at or utcnow()
    note = f"UPI {r.ref}" + (f" · {extra_note}" if extra_note else "")
    main = None
    if to_ms > 0:
        main = add_payment(db, gym, actor_id, member, ms, to_ms, "upi", utr, paid_at, note, source="app")
    if r.amount_paise - to_ms > 0:
        extra = add_payment(db, gym, actor_id, member, None, r.amount_paise - to_ms, "upi",
                            utr if main is None else None, paid_at, note + " (credit)", source="app")
        main = main or extra
    db.flush()
    settle_credit(db, gym, member, actor_id)
    return main


@router.post("/gyms/{gymId}/upi-requests/{requestId}/reject", response_model=UpiRequestOut,
             summary="Reject: no matching credit in your bank (payments.record)")
def reject_request(requestId: str, body: RejectIn, ctx: GymCtx = Depends(perm_ctx("payments.record")),
                   db: Session = Depends(get_db)):
    r = _get_req(db, ctx, requestId)
    if r.status not in ("claimed", "pending", "expired"):
        raise ApiError(409, "NOT_ACTIVE", f"This UPI payment is already {r.status}.")
    r.status, r.reject_reason, r.decided_by, r.decided_at = "rejected", body.reason, ctx.user.id, utcnow()
    member = db.get(GymMember, r.member_id)
    audit(db, ctx.gym.id, ctx.user.id, "upi.reject", "upi_request", r.id,
          f"Rejected UPI {rupees(r.amount_paise)} from {member.display_name}: {body.reason}")
    db.commit()
    enqueue("push.send", send_push, member.user_id, "member", "Payment not confirmed",
            f"{ctx.gym.name} couldn’t find your UPI payment of {rupees(r.amount_paise)}: {body.reason}",
            {"type": "upi_rejected", "request_id": r.id})
    return ser_req(r, member)


@router.post("/gyms/{gymId}/upi-requests/{requestId}/cancel", response_model=UpiRequestOut,
             summary="Cancel an open request (payments.record)")
def cancel_request(requestId: str, ctx: GymCtx = Depends(perm_ctx("payments.record")), db: Session = Depends(get_db)):
    r = _get_req(db, ctx, requestId)
    if r.status not in ("pending", "expired"):
        raise ApiError(409, "NOT_ACTIVE", f"This UPI payment is {r.status}; it can’t be cancelled.")
    r.status, r.decided_by, r.decided_at = "cancelled", ctx.user.id, utcnow()
    db.commit()
    return ser_req(r, db.get(GymMember, r.member_id))


# ---------------- member app ----------------
def _my_member(db: Session, user: User, member_id: str | None) -> tuple[GymMember, Gym]:
    q = select(GymMember, Gym).join(Gym, Gym.id == GymMember.gym_id).where(
        GymMember.user_id == user.id, GymMember.claim_disputed_at.is_(None), GymMember.status == "active")
    rows = db.execute(q).all()
    if member_id:
        rows = [(m, g) for m, g in rows if m.id == member_id]
    if not rows:
        raise ApiError(404, "NOT_FOUND", "No gym membership found.")
    return rows[0]


@me_router.get("/upi", summary="Can I pay by UPI here? Gym UPI ID + my dues", response_model=MyUpiInfoOut)
def my_upi_info(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m, g = _my_member(db, user, member_id)
    acct = db.scalar(select(GymUpiAccount).where(GymUpiAccount.gym_id == g.id, GymUpiAccount.active.is_(True))
                     .order_by(GymUpiAccount.is_default.desc()).limit(1))
    st = load_states(db, g, [m.id])[m.id]
    open_req = db.scalar(select(UpiPaymentRequest).where(UpiPaymentRequest.member_id == m.id,
                                                         UpiPaymentRequest.status.in_(("pending", "claimed")))
                         .order_by(UpiPaymentRequest.created_at.desc()).limit(1))
    return {"member_id": m.id, "gym": {"id": g.id, "name": g.name}, "upi_enabled": bool(acct),
            "payee_name": acct.payee_name if acct else None, "vpa": acct.vpa if acct else None,
            "due_paise": st.due_paise, "open_request": ser_req(open_req, m) if open_req else None}


@me_router.post("/upi-requests", response_model=UpiRequestOut, status_code=201,
                summary="Pay my dues by UPI: get the upi:// link / QR",
                description="Amount defaults to your dues. After paying in your UPI app, submit the 12-digit UTR "
                            "with POST /me/upi-requests/{id}/claim. 409 UPI_NOT_SET_UP if the gym has no UPI ID.")
def my_create_request(body: MyUpiRequestIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"upireq:{user.id}", 10, 3600)
    m, g = _my_member(db, user, body.member_id)
    if g.status != "active":
        raise ApiError(423, "GYM_PENDING")
    acct = default_account(db, g.id)
    ms, amount = resolve_amount(db, g, m, body.membership_id, body.amount_paise)
    r = new_request(db, g, m, ms, amount, acct, "member", user.id, None, 1440)
    audit(db, g.id, user.id, "upi.request", "upi_request", r.id, f"{m.display_name} started a UPI payment of {rupees(amount)}")
    db.commit()
    return ser_req(r, m)


@me_router.get("/upi-requests", response_model=UpiRequestListOut, summary="My UPI payments")
def my_requests(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m, _ = _my_member(db, user, member_id)
    rows = db.scalars(select(UpiPaymentRequest).where(UpiPaymentRequest.member_id == m.id)
                      .order_by(UpiPaymentRequest.created_at.desc()).limit(50)).all()
    receipts = {p.id: p.receipt_no for p in db.scalars(select(Payment).where(
        Payment.id.in_({r.payment_id for r in rows if r.payment_id} or {""})))}
    out = [ser_req(r, m, receipts.get(r.payment_id)) for r in rows]
    db.commit()
    return {"items": out, "next_cursor": None}


def _my_req(db: Session, user: User, rid: str) -> tuple[UpiPaymentRequest, GymMember]:
    r = db.get(UpiPaymentRequest, rid)
    m = db.get(GymMember, r.member_id) if r else None
    if not r or not m or m.user_id != user.id:
        raise ApiError(404, "NOT_FOUND", "That payment wasn’t found.")
    return refresh_status(r), m


@me_router.get("/upi-requests/{requestId}", response_model=UpiRequestOut, summary="One of my UPI payments (poll status)")
def my_request(requestId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r, m = _my_req(db, user, requestId)
    p = db.get(Payment, r.payment_id) if r.payment_id else None
    out = ser_req(r, m, p.receipt_no if p else None)
    db.commit()
    return out


@me_router.post("/upi-requests/{requestId}/claim", response_model=UpiRequestOut,
                summary="I’ve paid: submit the 12-digit UTR for the gym to verify",
                description="Status becomes `claimed`; the gym owner is notified to check their bank and verify. "
                            "Works on expired requests too (late payments).")
def my_claim(requestId: str, body: ClaimIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"upiclaim:{user.id}", 10, 3600)
    r, m = _my_req(db, user, requestId)
    if r.status not in ("pending", "expired", "claimed"):
        raise ApiError(409, "NOT_ACTIVE", f"This payment is already {r.status}.")
    utr = clean_utr(body.utr)
    _utr_taken(db, r.gym_id, utr, exclude_req=r.id)
    r.status, r.utr, r.claimed_at = "claimed", utr, utcnow()
    g = db.get(Gym, r.gym_id)
    audit(db, g.id, user.id, "upi.claim", "upi_request", r.id,
          f"{m.display_name} says they paid {rupees(r.amount_paise)} by UPI (UTR {utr})")
    db.commit()
    for uid in payment_staff(db, g.id):
        enqueue("push.send", send_push, uid, "admin", "UPI payment to verify",
                f"{m.display_name} paid {rupees(r.amount_paise)} (UTR {utr}). Check your bank and verify.",
                {"type": "upi_claimed", "gym_id": g.id, "request_id": r.id})
    return ser_req(r, m)


@me_router.post("/upi-requests/{requestId}/cancel", response_model=UpiRequestOut, summary="Cancel my open UPI payment")
def my_cancel(requestId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r, m = _my_req(db, user, requestId)
    if r.status not in ("pending", "expired"):
        raise ApiError(409, "NOT_ACTIVE", f"This payment is {r.status}; it can’t be cancelled.")
    r.status, r.decided_at = "cancelled", utcnow()
    db.commit()
    return ser_req(r, m)


_ = (and_, or_)


# ---------------- member app: self renewal ----------------
@me_router.get("/plans", summary="Plans I can renew to at my gym, with my price and start date",
               description="`price` is what this member pays (offer price, joining fee for a first membership, GST). "
                           "`credit_paise` is applied automatically, so `to_pay_paise` = price − credit.")
def my_plans(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m, g = _my_member(db, user, member_id)
    start = default_start(db, g, m.id)
    credit = max(0, credit_balance(db, m.id))
    acct = db.scalar(select(GymUpiAccount.id).where(GymUpiAccount.gym_id == g.id, GymUpiAccount.active.is_(True)).limit(1))
    out = []
    for p in db.scalars(select(Plan).where(Plan.gym_id == g.id, Plan.active.is_(True)).order_by(Plan.sort_order, Plan.duration_days)):
        pr = price_preview(db, p, m)
        out.append({"id": p.id, "name": p.name, "duration_days": p.duration_days, "description": p.description,
                    "facilities": p.facilities or [], "price": pr, "price_paise": pr["total_paise"],
                    "to_pay_paise": max(0, pr["total_paise"] - credit),
                    "start_date": start.isoformat(), "end_date": (start + timedelta(days=p.duration_days - 1)).isoformat()})
    return {"member_id": m.id, "gym": {"id": g.id, "name": g.name}, "upi_enabled": bool(acct) and g.status == "active",
            "credit_paise": credit, "items": out}


@me_router.post("/renew", status_code=201, summary="Renew my membership from the app",
                description="Pick a plan. If your credit covers it, the membership is created now (`mode: credit`). "
                            "Otherwise you get a UPI payment (`mode: upi`, same as /me/upi-requests): pay, submit the "
                            "UTR, and the new membership starts as soon as the gym verifies the payment.")
def my_renew(body: RenewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"renew:{user.id}", 10, 3600)
    m, g = _my_member(db, user, body.member_id)
    if g.status != "active":
        raise ApiError(423, "GYM_PENDING")
    plan = _active_plan(db, g.id, body.plan_id)
    total = price_preview(db, plan, m)["total_paise"]
    credit = max(0, credit_balance(db, m.id))
    if total - credit <= 0:
        ms = create_membership(db, g, user.id, m, plan=plan, start=default_start(db, g, m.id))
        db.flush()
        settle_credit(db, g, m, user.id, ms.id)
        audit(db, g.id, user.id, "membership.renew", "membership", ms.id,
              f"{m.display_name} renewed on {plan.name} from the app using credit")
        db.commit()
        return {"mode": "credit", "membership": ser_membership(ms, g, db, can_pay=True, due=membership_due(db, ms))}
    acct = default_account(db, g.id)
    r = new_request(db, g, m, None, total - credit, acct, "member", user.id, None, 1440, plan)
    audit(db, g.id, user.id, "upi.request", "upi_request", r.id,
          f"{m.display_name} started a renewal to {plan.name} ({rupees(r.amount_paise)}) from the app")
    db.commit()
    return {"mode": "upi", "request": ser_req(r, m, plan_name=plan.name)}
