"""Derived values & business rules (spec §4). Status is computed on read, never trusted from storage."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Checkin, CreditApplication, Gym, Membership, MembershipFreeze, Payment
from app.utils import local_date_of, today_in, tz

LIVE = ("active", "expiring", "frozen", "suspended")
# check-in refusals caused by the plan (not by a bad QR / location): what a gym wants to see
PLAN_DENIALS = ("expired", "cancelled", "frozen", "suspended", "not_started")


def freeze_covers(fr: MembershipFreeze, d: date) -> bool:
    return fr.from_date <= d and (fr.to_date is None or d <= fr.to_date)


def membership_status(m: Membership, today: date, freezes: list[MembershipFreeze] | None = None) -> str:
    if m.state == "cancelled":
        return "cancelled"
    if getattr(m, "suspended_at", None) and m.end_date >= today:
        return "suspended"
    if freezes is not None:
        if any(freeze_covers(f, today) for f in freezes):
            return "frozen"
    elif m.state == "frozen":
        return "frozen"
    if m.start_date > today:
        return "upcoming"
    if m.end_date < today:
        return "expired"
    if (m.end_date - today).days <= 7:
        return "expiring"
    return "active"


def days_left(m: Membership, status: str, today: date) -> int:
    if status in LIVE:
        return max(0, (m.end_date - today).days + 1)
    if status == "upcoming":
        return (m.end_date - m.start_date).days + 1
    return 0


def pick_current(items: list[tuple[Membership, str]]) -> tuple[Membership, str] | None:
    """active/frozen/expiring > upcoming (soonest) > most recently ended > latest cancelled."""
    if not items:
        return None
    live = [x for x in items if x[1] in LIVE]
    if live:
        return min(live, key=lambda x: x[0].start_date)
    up = [x for x in items if x[1] == "upcoming"]
    if up:
        return min(up, key=lambda x: x[0].start_date)
    ended = [x for x in items if x[1] == "expired"]
    if ended:
        return max(ended, key=lambda x: x[0].end_date)
    return max(items, key=lambda x: (x[0].end_date, x[0].created_at))


@dataclass
class MemberState:
    memberships: list = field(default_factory=list)   # [(Membership, status, due)]
    current: Membership | None = None
    status: str = "no_plan"
    due_paise: int = 0          # Σ due over non-cancelled memberships
    credit_paise: int = 0       # unallocated payments (membership_id NULL)

    def current_dict(self, today: date) -> dict | None:
        m = self.current
        if not m:
            return None
        return {"membership_id": m.id, "plan_name": m.plan_name, "status": self.status,
                "start_date": m.start_date.isoformat(), "end_date": m.end_date.isoformat(),
                "days_left": days_left(m, self.status, today)}


def load_states(db: Session, gym: Gym, member_ids: list[str] | None = None) -> dict[str, MemberState]:
    """Batch-compute membership status / dues for a gym (3 queries)."""
    today = today_in(gym.timezone)
    q = select(Membership).where(Membership.gym_id == gym.id)
    if member_ids is not None:
        if not member_ids:
            return {}
        q = q.where(Membership.member_id.in_(member_ids))
    ms = db.scalars(q).all()
    ms_ids = [m.id for m in ms]
    freezes = defaultdict(list)
    paid = {}
    if ms_ids:
        for chunk in _chunks(ms_ids, 1000):
            for f in db.scalars(select(MembershipFreeze).where(MembershipFreeze.membership_id.in_(chunk))):
                freezes[f.membership_id].append(f)
            for mid, s in db.execute(select(Payment.membership_id, func.sum(Payment.amount_paise))
                                     .where(Payment.membership_id.in_(chunk), Payment.status == "recorded")
                                     .group_by(Payment.membership_id)):
                paid[mid] = int(s or 0)
            for mid, s in db.execute(select(CreditApplication.membership_id, func.sum(CreditApplication.amount_paise))
                                     .where(CreditApplication.membership_id.in_(chunk),
                                            CreditApplication.reversed_at.is_(None))
                                     .group_by(CreditApplication.membership_id)):
                paid[mid] = paid.get(mid, 0) + int(s or 0)
    credit_q = select(Payment.member_id, func.sum(Payment.amount_paise)).where(
        Payment.gym_id == gym.id, Payment.membership_id.is_(None), Payment.status == "recorded")
    if member_ids is not None:
        credit_q = credit_q.where(Payment.member_id.in_(member_ids))
    credits = {k: int(v or 0) for k, v in db.execute(credit_q.group_by(Payment.member_id))}
    used_q = select(CreditApplication.member_id, func.sum(CreditApplication.amount_paise)).where(
        CreditApplication.gym_id == gym.id, CreditApplication.reversed_at.is_(None))
    if member_ids is not None:
        used_q = used_q.where(CreditApplication.member_id.in_(member_ids))
    for k, v in db.execute(used_q.group_by(CreditApplication.member_id)):
        credits[k] = credits.get(k, 0) - int(v or 0)

    by_member: dict[str, list] = defaultdict(list)
    for m in ms:
        st = membership_status(m, today, freezes.get(m.id, []))
        due = 0 if st == "cancelled" else max(0, m.price_paise - paid.get(m.id, 0))
        by_member[m.member_id].append((m, st, due))

    out: dict[str, MemberState] = {}
    for mid in (member_ids if member_ids is not None else list(by_member.keys())):
        items = by_member.get(mid, [])
        s = MemberState(memberships=items, credit_paise=max(0, credits.get(mid, 0)))
        cur = pick_current([(m, st) for m, st, _ in items])
        if cur:
            s.current, s.status = cur
        s.due_paise = sum(d for _, _, d in items)
        out[mid] = s
    return out


def member_state(db: Session, gym: Gym, member_id: str) -> MemberState:
    return load_states(db, gym, [member_id])[member_id]


def membership_due(db: Session, m: Membership) -> int:
    if m.state == "cancelled":
        return 0
    paid = db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0))
                     .where(Payment.membership_id == m.id, Payment.status == "recorded")) or 0
    applied = db.scalar(select(func.coalesce(func.sum(CreditApplication.amount_paise), 0))
                        .where(CreditApplication.membership_id == m.id, CreditApplication.reversed_at.is_(None))) or 0
    return max(0, m.price_paise - int(paid) - int(applied))


def credit_balance(db: Session, member_id: str) -> int:
    """Unallocated recorded payments minus credit already applied to memberships (can be < 0 after a void)."""
    unalloc = db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
        Payment.member_id == member_id, Payment.membership_id.is_(None), Payment.status == "recorded")) or 0
    used = db.scalar(select(func.coalesce(func.sum(CreditApplication.amount_paise), 0)).where(
        CreditApplication.member_id == member_id, CreditApplication.reversed_at.is_(None))) or 0
    return int(unalloc) - int(used)


def freezes_of(db: Session, membership_id: str) -> list[MembershipFreeze]:
    return list(db.scalars(select(MembershipFreeze).where(MembershipFreeze.membership_id == membership_id)
                           .order_by(MembershipFreeze.from_date)))


def status_of(db: Session, m: Membership, tzname: str) -> str:
    return membership_status(m, today_in(tzname), freezes_of(db, m.id))


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ---------------- visits & streaks ----------------
def visit_days(db: Session, member_id: str, since: date | None = None) -> list[date]:
    q = select(Checkin.local_date).where(Checkin.member_id == member_id, Checkin.result == "success")
    if since:
        q = q.where(Checkin.local_date >= since)
    return sorted(set(db.scalars(q.distinct()).all()))


def _runs(days: list[date]) -> list[int]:
    """Up to 2 rest days never break a run: consecutive visits with gap ≤ 3 days continue it."""
    runs, cur = [], 0
    prev = None
    for d in days:
        if prev is None or (d - prev).days <= 3:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
        prev = d
    if cur:
        runs.append(cur)
    return runs


def streaks(days: list[date], today: date) -> tuple[int, int]:
    """(current, best)."""
    if not days:
        return 0, 0
    runs = _runs(days)
    best = max(runs)
    current = runs[-1] if (today - days[-1]).days < 3 else 0
    return current, best


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def usual_hour(db: Session, member_id: str, tzname: str) -> int | None:
    ats = db.scalars(select(Checkin.at).where(Checkin.member_id == member_id, Checkin.result == "success")
                     .order_by(Checkin.at.desc()).limit(30)).all()
    if not ats:
        return None
    z = tz(tzname)
    return Counter(a.astimezone(z).hour for a in ats).most_common(1)[0][0]


def member_visit_stats(db: Session, member_id: str, tzname: str, weekly_goal: int = 4) -> dict:
    today = today_in(tzname)
    days = visit_days(db, member_id)
    cur, best = streaks(days, today)
    ws = week_start(today)
    return {"streak": cur, "best_streak": best,
            "week_visits": sum(1 for d in days if ws <= d <= today), "weekly_goal": weekly_goal,
            "month_visits": sum(1 for d in days if d.year == today.year and d.month == today.month),
            "total_visits": len(days), "first_visit": days[0].isoformat() if days else None,
            "last_visit": days[-1].isoformat() if days else None, "_days": days}


def hour_label(h: int) -> str:
    suffix = "am" if h < 12 else "pm"
    hh = h % 12 or 12
    return f"{hh} {suffix}"


def local_hour(dt, tzname) -> int:
    return dt.astimezone(tz(tzname)).hour


__all__ = ["membership_status", "days_left", "pick_current", "load_states", "member_state", "membership_due",
           "streaks", "visit_days", "member_visit_stats", "usual_hour", "local_date_of", "LIVE"]
