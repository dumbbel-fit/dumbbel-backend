"""Gym performance score (0–100) and the gym's key numbers, both from the gym's own data.

Five parts, each 0–100 (None when there isn't enough data yet; the overall score then uses the parts that exist):
  retention    25  members with a plan 30 days ago who still have one today
  renewal      25  plans that ended 15–44 days ago and were followed by another plan (14 days' grace to renew)
  attendance   20  members with a plan for the last 4 weeks who came in at least 3 of those 4 weeks (target 70 %)
  collection   15  money collected against plans started in the last 60 days (price vs paid + credit used)
  acquisition  15  new members in the last 30 days against the active base (target 10 % a month)
Imported members / plans / payments are left out of acquisition and collection (they are history, not activity).
`gym_numbers` is the fact sheet the assistant answers from: counts and money only, no member names or phones."""
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.utils import local_midnight_utc, today_in

WEIGHTS = {"retention": 25, "renewal": 25, "attendance": 20, "collection": 15, "acquisition": 15}
LABELS = {"retention": "Member retention", "renewal": "Renewal rate", "attendance": "Attendance consistency",
          "collection": "Payment collection", "acquisition": "New members"}
TIPS = {
    "retention": "Call members whose plan ended recently; most come back with one reminder.",
    "renewal": "Turn on WhatsApp expiry reminders and renew members at the desk before their plan ends.",
    "attendance": "Message members who haven’t come in for 10 days; a nudge early keeps them coming.",
    "collection": "Collect pending dues from the Members list (filter: Dues) and record part payments as they come.",
    "acquisition": "Put the QR poster up, ask members to bring a friend, and list your gym on DUMBBEL.fit Explore.",
}
MIN = {"retention": 5, "renewal": 5, "attendance": 5, "collection": 3}
LIVE = "m.state <> 'cancelled' AND m.start_date <= :d AND m.end_date >= :d"
NEXT = ("EXISTS (SELECT 1 FROM memberships n WHERE n.member_id = m.member_id AND n.id <> m.id "
        "AND n.state <> 'cancelled' AND n.start_date > m.start_date)")


def _one(db: Session, sql: str, **p) -> int:
    return int(db.execute(text(sql), p).scalar() or 0)


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def _live_members(db: Session, gym_id: str, d: date) -> int:
    return _one(db, f"""SELECT COUNT(DISTINCT m.member_id) FROM memberships m JOIN gym_members gm ON gm.id = m.member_id
                        WHERE m.gym_id = :g AND gm.status = 'active' AND {LIVE}""", g=gym_id, d=d)


def score_parts(db: Session, gym, td: date | None = None) -> dict:
    """Raw rates and part scores as of `td` (default today in the gym's time zone)."""
    g = gym.id
    td = td or today_in(gym.timezone)
    tz_ = gym.timezone
    out = {}

    # retention: had a plan on td-30, still has one on td
    base = _one(db, f"SELECT COUNT(DISTINCT m.member_id) FROM memberships m WHERE m.gym_id = :g AND {LIVE}", g=g, d=td - timedelta(days=30))
    kept = _one(db, f"""SELECT COUNT(DISTINCT m.member_id) FROM memberships m WHERE m.gym_id = :g AND {LIVE}
                        AND m.member_id IN (SELECT m2.member_id FROM memberships m2 WHERE m2.gym_id = :g AND m2.state <> 'cancelled'
                                            AND m2.start_date <= :e AND m2.end_date >= :e)""", g=g, d=td - timedelta(days=30), e=td)
    out["retention"] = {"rate_pct": _pct(kept, base), "of": base, "kept": kept,
                        "score": _pct(kept, base) if base >= MIN["retention"] else None}

    # renewal: ended 15–44 days ago, followed by another plan
    ended = _one(db, "SELECT COUNT(*) FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' AND m.end_date BETWEEN :a AND :b",
                 g=g, a=td - timedelta(days=44), b=td - timedelta(days=15))
    renewed = _one(db, f"SELECT COUNT(*) FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' AND m.end_date BETWEEN :a AND :b AND {NEXT}",
                   g=g, a=td - timedelta(days=44), b=td - timedelta(days=15))
    out["renewal"] = {"rate_pct": _pct(renewed, ended), "of": ended, "renewed": renewed,
                      "score": _pct(renewed, ended) if ended >= MIN["renewal"] else None}

    # attendance: members with a plan all 4 weeks who came in >= 3 of the 4 weeks
    s28 = td - timedelta(days=27)
    elig = db.execute(text("""SELECT DISTINCT m.member_id FROM memberships m JOIN gym_members gm ON gm.id = m.member_id
                              WHERE m.gym_id = :g AND gm.status = 'active' AND m.state <> 'cancelled'
                              AND m.start_date <= :a AND m.end_date >= :b"""), {"g": g, "a": s28, "b": td}).scalars().all()
    weeks = {}
    if elig:
        for mid_, wk in db.execute(text("""SELECT member_id, FLOOR(DATEDIFF(local_date, :a) / 7) wk FROM checkins
                                           WHERE gym_id = :g AND result = 'success' AND local_date BETWEEN :a AND :b
                                           GROUP BY member_id, wk"""), {"g": g, "a": s28, "b": td}):
            weeks[mid_] = weeks.get(mid_, 0) + 1
    tracked = _one(db, "SELECT COUNT(*) FROM checkins WHERE gym_id = :g AND result = 'success' AND local_date BETWEEN :a AND :b", g=g, a=s28, b=td)
    regular = sum(1 for m in elig if weeks.get(m, 0) >= 3)
    rate = _pct(regular, len(elig))
    out["attendance"] = {"rate_pct": rate, "of": len(elig), "regular": regular, "checkins_28d": tracked,
                         "score": min(100.0, round(rate / 0.7, 1)) if rate is not None and len(elig) >= MIN["attendance"] and tracked >= 10 else None}

    # collection: plans started in the last 60 days, price vs paid (+ credit used), each capped at its price
    row = db.execute(text("""SELECT COUNT(*) n, COALESCE(SUM(m.price_paise), 0) billed,
                                    COALESCE(SUM(LEAST(m.price_paise,
                                        COALESCE((SELECT SUM(p.amount_paise) FROM payments p WHERE p.membership_id = m.id AND p.status = 'recorded'), 0)
                                      + COALESCE((SELECT SUM(c.amount_paise) FROM credit_applications c WHERE c.membership_id = m.id AND c.reversed_at IS NULL), 0))), 0) got
                             FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' AND m.source = 'manual'
                             AND m.price_paise > 0 AND m.start_date BETWEEN :a AND :b"""), {"g": g, "a": td - timedelta(days=59), "b": td}).one()
    n, billed, got = int(row[0]), int(row[1]), int(row[2])
    out["collection"] = {"rate_pct": _pct(got, billed), "plans": n, "billed_paise": billed, "collected_paise": got,
                         "score": _pct(got, billed) if n >= MIN["collection"] else None}

    # acquisition: new (not imported) members in the last 30 days vs the active base
    active = _live_members(db, g, td)
    new = _one(db, "SELECT COUNT(*) FROM gym_members WHERE gym_id = :g AND source <> 'import' AND created_at >= :s",
               g=g, s=local_midnight_utc(td - timedelta(days=29), tz_))
    r = _pct(new, active)
    out["acquisition"] = {"rate_pct": r, "new": new, "active": active,
                          "score": min(100.0, round(r / 10 * 100, 1)) if r is not None and active >= 5 else None}
    return out


def overall(parts: dict) -> float | None:
    have = [(WEIGHTS[k], v["score"]) for k, v in parts.items() if v.get("score") is not None]
    if not have:
        return None
    return round(sum(w * s for w, s in have) / sum(w for w, _ in have))


def grade(score) -> str:
    if score is None:
        return "Not enough data yet"
    return "Excellent" if score >= 80 else "Good" if score >= 65 else "Fair" if score >= 50 else "Needs attention"


def gym_score(db: Session, gym) -> dict:
    td = today_in(gym.timezone)
    now, before = score_parts(db, gym, td), score_parts(db, gym, td - timedelta(days=30))
    s, prev = overall(now), overall(before)
    parts = [{"key": k, "label": LABELS[k], "weight": WEIGHTS[k], "score": now[k]["score"], "rate_pct": now[k]["rate_pct"],
              "previous_score": before[k]["score"], "detail": now[k], "tip": TIPS[k]} for k in WEIGHTS]
    weakest = min((p for p in parts if p["score"] is not None), key=lambda p: p["score"], default=None)
    return {"score": s, "grade": grade(s), "previous_score": prev, "change": (s - prev) if s is not None and prev is not None else None,
            "as_of": td.isoformat(), "parts": parts, "focus": weakest["key"] if weakest and weakest["score"] < 70 else None}


def gym_numbers(db: Session, gym) -> dict:
    """This month so far vs the same days last month, plus where things stand today. Counts and money only."""
    g, tz_ = gym.id, gym.timezone
    td = today_in(tz_)
    m0 = td.replace(day=1)
    pm_end = m0 - timedelta(days=1)
    pm0 = pm_end.replace(day=1)
    same = min(td.day, pm_end.day)
    pm_same_end = pm0 + timedelta(days=same - 1)
    U = lambda d: local_midnight_utc(d, tz_)  # noqa: E731

    def money(a, b):  # [a, b] local days
        return _one(db, "SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE gym_id = :g AND status = 'recorded' AND source <> 'import' "
                        "AND paid_at >= :s AND paid_at < :e", g=g, s=U(a), e=U(b + timedelta(days=1)))

    def renewals(a, b):  # a new plan for someone who had one before
        return _one(db, """SELECT COUNT(*) FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' AND m.source = 'manual'
                           AND m.created_at >= :s AND m.created_at < :e
                           AND EXISTS (SELECT 1 FROM memberships o WHERE o.member_id = m.member_id AND o.id <> m.id AND o.start_date < m.start_date)""",
                    g=g, s=U(a), e=U(b + timedelta(days=1)))

    def joins(a, b):
        return _one(db, "SELECT COUNT(*) FROM gym_members WHERE gym_id = :g AND source <> 'import' AND created_at >= :s AND created_at < :e",
                    g=g, s=U(a), e=U(b + timedelta(days=1)))

    def visits(a, b):
        return _one(db, "SELECT COUNT(*) FROM checkins WHERE gym_id = :g AND result = 'success' AND local_date BETWEEN :a AND :b", g=g, a=a, b=b)

    def ended_not_renewed(a, b):
        return _one(db, f"SELECT COUNT(*) FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' AND m.end_date BETWEEN :a AND :b AND NOT {NEXT}",
                    g=g, a=a, b=b)

    away = _one(db, f"""SELECT COUNT(DISTINCT m.member_id) FROM memberships m JOIN gym_members gm ON gm.id = m.member_id
                        WHERE m.gym_id = :g AND gm.status = 'active' AND {LIVE}
                        AND NOT EXISTS (SELECT 1 FROM checkins c WHERE c.member_id = m.member_id AND c.result = 'success' AND c.local_date > :a)""",
                g=g, d=td, a=td - timedelta(days=10))
    due = db.execute(text("""SELECT COUNT(*), COALESCE(SUM(d), 0) FROM (
                               SELECT m.member_id, GREATEST(0, m.price_paise
                                 - COALESCE((SELECT SUM(p.amount_paise) FROM payments p WHERE p.membership_id = m.id AND p.status = 'recorded'), 0)
                                 - COALESCE((SELECT SUM(c.amount_paise) FROM credit_applications c WHERE c.membership_id = m.id AND c.reversed_at IS NULL), 0)) d
                               FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' AND m.end_date >= :a) x WHERE d > 0"""),
                     {"g": g, "a": td - timedelta(days=60)}).one()
    rev_now, rev_prev, rev_full = money(m0, td), money(pm0, pm_same_end), money(pm0, pm_end)
    return {
        "gym": gym.name, "today": td.isoformat(), "this_month": m0.strftime("%B %Y"), "last_month": pm0.strftime("%B %Y"),
        "compare_note": f"This month = 1–{td.day} {m0:%b}; last month = the same days (1–{same} {pm0:%b}), so the two are fair to compare.",
        "revenue": {"this_month_paise": rev_now, "last_month_same_days_paise": rev_prev, "last_month_full_paise": rev_full,
                    "change_pct": _pct(rev_now - rev_prev, rev_prev)},
        "renewals": {"this_month": renewals(m0, td), "last_month_same_days": renewals(pm0, pm_same_end)},
        "new_members": {"this_month": joins(m0, td), "last_month_same_days": joins(pm0, pm_same_end)},
        "checkins": {"this_month": visits(m0, td), "last_month_same_days": visits(pm0, pm_same_end)},
        "plans_ended_not_renewed": {"this_month": ended_not_renewed(m0, td), "last_month_same_days": ended_not_renewed(pm0, pm_same_end)},
        "active_members": _live_members(db, g, td),
        "members_not_visited_10_days": away,
        "expiring_next_7_days": _one(db, "SELECT COUNT(DISTINCT m.member_id) FROM memberships m WHERE m.gym_id = :g AND m.state <> 'cancelled' "
                                         f"AND m.end_date BETWEEN :a AND :b AND NOT {NEXT}", g=g, a=td, b=td + timedelta(days=7)),
        "dues": {"members": int(due[0]), "total_paise": int(due[1])},
        "setup": {"plans": _one(db, "SELECT COUNT(*) FROM plans WHERE gym_id = :g AND active = 1", g=g),
                  "staff": _one(db, "SELECT COUNT(*) FROM gym_staff WHERE gym_id = :g AND status <> 'deactivated' AND role <> 'owner'", g=g),
                  "whatsapp_linked": _one(db, "SELECT COUNT(*) FROM gym_whatsapp WHERE gym_id = :g AND status = 'connected'", g=g) > 0,
                  "upi_ids": _one(db, "SELECT COUNT(*) FROM gym_upi_accounts WHERE gym_id = :g", g=g)},
    }
