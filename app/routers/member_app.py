"""§13 — member app: memberships, rotating QR, history, insights, payments, claim dispute.
Member routes only ever read rows where user_id = caller."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user
from app.errors import ApiError
from app.models import Checkin, Gym, GymMember, Membership, Payment, User
from app.schemas import DisputeIn
from app.security import QR_TTL, make_qr_token
from app.services import storage
from app.services.audit import audit
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.rules import days_left, hour_label, load_states, member_visit_stats, usual_hour, week_start
from app.utils import first_name, iso, month_bounds, today_in, utcnow
from app.responses import DocRoute, ERRORS, InsightsOut, MyCheckinsOut, MyMembershipsOut, MyPaymentsOut, QrOut

router = APIRouter(prefix="/me", tags=["Member app"], route_class=DocRoute, responses=ERRORS)


def _my_rows(db: Session, user: User):
    return db.execute(select(GymMember, Gym).join(Gym, Gym.id == GymMember.gym_id).where(
        GymMember.user_id == user.id, GymMember.claim_disputed_at.is_(None), GymMember.status == "active")).all()


def member_cards(db: Session, user: User) -> list[dict]:
    out = []
    for gm, g in _my_rows(db, user):
        today = today_in(g.timezone)
        st = load_states(db, g, [gm.id])[gm.id]
        cur = st.current
        cur_due = next((d for m, _, d in st.memberships if cur and m.id == cur.id), 0)
        stats = member_visit_stats(db, gm.id, g.timezone, user.weekly_goal)
        stats.pop("_days", None)
        stats["best_streak"] = max(stats["best_streak"], gm.best_streak or 0)
        stats["last_checkin_at"] = iso(gm.last_checkin_at)
        out.append({"member_id": gm.id, "member_code": gm.member_code,
                    "gym": {"id": g.id, "name": g.name, "area": g.area, "logo_url": storage.public_url(g.logo_url),
                            "phone": g.phone, "status": g.status},
                    "current": {"id": cur.id, "plan_name": cur.plan_name, "status": st.status,
                                "start_date": cur.start_date.isoformat(), "end_date": cur.end_date.isoformat(),
                                "days_left": days_left(cur, st.status, today), "due_paise": cur_due} if cur else None,
                    "due_paise": st.due_paise, "stats": stats})
    return out


def _resolve(db: Session, user: User, member_id: str | None) -> tuple[GymMember, Gym]:
    rows = _my_rows(db, user)
    if member_id:
        for gm, g in rows:
            if gm.id == member_id:
                return gm, g
        raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
    if not rows:
        raise ApiError(404, "NOT_FOUND", "No gym membership is linked to this number yet.")
    return rows[0]


@router.get("/memberships", response_model=MyMembershipsOut, summary="My gyms, current plan and visit stats")
def my_memberships(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"items": member_cards(db, user)}


QR_MESSAGES = {"expired": "Your membership has ended. Renew at the front desk to check in.",
               "frozen": "Your membership is frozen right now.",
               "suspended": "Your membership is on hold. Please talk to the front desk.",
               "cancelled": "This membership was cancelled.",
               "upcoming": "Your membership hasn’t started yet.",
               "no_plan": "No active membership. Ask the front desk to add one."}


@router.get("/qr", response_model=QrOut, summary="Rotating check-in QR token (refresh every 30 s)",
            description="Token only issued when status allows entry; otherwise `token` is null and `message` explains why.")
def my_qr(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    gm, g = _resolve(db, user, member_id)
    st = load_states(db, g, [gm.id])[gm.id]
    today = today_in(g.timezone)
    status = st.status
    base = {"member_id": gm.id, "member_code": gm.member_code, "gym": {"id": g.id, "name": g.name},
            "refresh_in_sec": QR_TTL, "server_time": iso(utcnow()),
            "days_left": days_left(st.current, status, today) if st.current else 0}
    if status in ("active", "expiring"):
        if g.status != "active":
            return {**base, "token": None, "expires_at": None, "status": "active",
                    "message": "QR check-in starts once your gym is verified. Check in at the desk for now."}
        token, exp = make_qr_token(gm.id, g.id)
        from datetime import datetime, timezone
        return {**base, "token": token, "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(),
                "status": "active", "message": None}
    return {**base, "token": None, "expires_at": None, "status": status, "message": QR_MESSAGES.get(status)}


@router.get("/checkins", response_model=MyCheckinsOut, summary="My visit calendar for a month + totals")
def my_checkins(member_id: str | None = None, month: str | None = Query(None, examples=["2026-11"]),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    gm, g = _resolve(db, user, member_id)
    start, end = month_bounds(month, g.timezone)
    rows = db.scalars(select(Checkin).where(Checkin.member_id == gm.id, Checkin.result == "success",
                                            Checkin.local_date >= start, Checkin.local_date <= end)
                      .order_by(Checkin.at.desc())).all()
    staff = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_([r.staff_user_id for r in rows] or [""])))}
    stats = member_visit_stats(db, gm.id, g.timezone, user.weekly_goal)
    return {"month": start.strftime("%Y-%m"), "days": sorted({r.local_date.day for r in rows}),
            "items": [{"at": iso(r.at), "method": r.method, "staff_first_name": first_name(staff.get(r.staff_user_id))}
                      for r in rows],
            "totals": {"month": len({r.local_date for r in rows}), "all_time": stats["total_visits"],
                       "streak": stats["streak"], "best_streak": max(stats["best_streak"], gm.best_streak or 0)}}


@router.get("/insights", response_model=InsightsOut, summary="Personal insights: weekly goal, usual time, membership end")
def my_insights(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    gm, g = _resolve(db, user, member_id)
    today = today_in(g.timezone)
    stats = member_visit_stats(db, gm.id, g.timezone, user.weekly_goal)
    st = load_states(db, g, [gm.id])[gm.id]
    items = []
    wv, goal = stats["week_visits"], user.weekly_goal
    left_days = 6 - today.weekday()
    if wv >= goal:
        detail = f"Goal hit! {wv} of {goal} visits this week."
    else:
        detail = f"{wv} of {goal} visits this week · {goal - wv} more to go" + (f" in {left_days + 1} days" if left_days >= 0 else "")
    items.append({"key": "weekly_goal", "title": f"{wv}/{goal} this week", "detail": detail, "value": wv, "goal": goal,
                  "rule": f"Visits Monday–Sunday ({week_start(today).strftime('%d %b')} onwards) vs your weekly goal."})
    h = usual_hour(db, gm.id, g.timezone)
    if h is not None:
        items.append({"key": "usual_time", "title": f"You usually train around {hour_label(h)}", "detail": None,
                      "hour": h, "rule": "Most common hour across your last 30 check-ins."})
    cur = st.current
    if cur and st.status in ("active", "expiring", "frozen") and (cur.end_date - today).days <= 7:
        dl = days_left(cur, st.status, today)
        items.append({"key": "membership_end", "title": f"Membership ends in {dl} day{'s' if dl != 1 else ''}",
                      "detail": f"{cur.plan_name} ends on {cur.end_date.strftime('%d %b')}. Renew at the front desk.",
                      "rule": "Shown when your membership ends within 7 days."})
    if st.status == "expired" and cur:
        items.append({"key": "expired", "title": "Membership ended",
                      "detail": f"{cur.plan_name} ended on {cur.end_date.strftime('%d %b')}.",
                      "rule": "Your latest membership end date has passed."})
    return {"items": items}


@router.get("/payments", response_model=MyPaymentsOut, summary="My payments and dues at a gym")
def my_payments(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    gm, g = _resolve(db, user, member_id)
    st = load_states(db, g, [gm.id])[gm.id]
    pays = db.scalars(select(Payment).where(Payment.member_id == gm.id).order_by(Payment.paid_at.desc())).all()
    plans = {m.id: m.plan_name for m in db.scalars(select(Membership).where(Membership.member_id == gm.id))}
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_([p.recorded_by for p in pays] or [""])))}
    return {"due_paise": st.due_paise, "credit_paise": st.credit_paise,
            "items": [{"id": p.id, "receipt_no": p.receipt_no, "plan_name": plans.get(p.membership_id),
                       "amount_paise": p.amount_paise, "method": p.method, "reference": p.reference,
                       "paid_at": iso(p.paid_at), "recorded_by_first_name": first_name(names.get(p.recorded_by)),
                       "status": p.status, "source": p.source} for p in pays]}


@router.post("/claims/{memberId}/dispute", status_code=204, summary="“This isn’t mine” — hide gym, flag to owner & support")
def dispute(memberId: str, body: DisputeIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    gm = db.get(GymMember, memberId)
    if not gm or gm.user_id != user.id:
        raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
    gm.claim_disputed_at = utcnow()
    gm.claim_dispute_note = body.note if body else None
    g = db.get(Gym, gm.gym_id)
    audit(db, g.id, user.id, "member.claim_dispute", "member", gm.id,
          f"{gm.display_name} said this membership isn’t theirs" + (f": {body.note}" if body and body.note else ""))
    db.commit()
    enqueue("push.send", send_push, g.owner_user_id, "admin", "Membership disputed",
            f"{gm.display_name} ({gm.member_code}) says this membership isn’t theirs. Check the phone number.",
            {"type": "claim_disputed", "member_id": gm.id})
    return Response(status_code=204)


@router.post("/claims/{memberId}/confirm", status_code=204,
             summary="Undo “This isn’t mine” — “Yes, this is my membership”",
             description="Clears the member’s own “not mine” flag so the membership shows again and check-in works.")
def confirm_claim(memberId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    gm = db.get(GymMember, memberId)
    if not gm or gm.user_id != user.id:
        raise ApiError(404, "NOT_FOUND", "That membership wasn’t found.")
    if gm.claim_disputed_at:
        gm.claim_disputed_at = None
        gm.claim_dispute_note = None
        audit(db, gm.gym_id, user.id, "member.claim_confirm", "member", gm.id,
              f"{gm.display_name} confirmed this membership is theirs")
        db.commit()
    return Response(status_code=204)
