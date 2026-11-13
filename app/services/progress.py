"""Engagement: XP, levels, badges, milestones, leaderboard — all derived from real check-ins (nothing to game)."""
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Checkin, Gym, GymMember, User
from app.services.rules import _runs, streaks, week_start
from app.utils import today_in, tz, utcnow

LEVELS = [(0, "Rookie"), (100, "Regular"), (300, "Committed"), (600, "Iron"), (1000, "Steel"), (1600, "Titan"),
          (2500, "Beast"), (4000, "Legend")]

BADGES = [
    # key, name, description, icon, target, metric
    ("first_visit", "First rep", "Your first check-in", "footprints", 1, "visits"),
    ("visits_10", "10 visits", "Checked in 10 times", "dumbbell", 10, "visits"),
    ("visits_25", "25 visits", "Checked in 25 times", "dumbbell", 25, "visits"),
    ("visits_50", "Half century", "50 gym visits", "medal", 50, "visits"),
    ("visits_100", "Century", "100 gym visits", "trophy", 100, "visits"),
    ("visits_250", "Gym rat", "250 gym visits", "crown", 250, "visits"),
    ("streak_3", "3-day streak", "3 visit days in a run", "flame", 3, "best_streak"),
    ("streak_7", "7-day streak", "7 visit days in a run", "flame", 7, "best_streak"),
    ("streak_14", "14-day streak", "14 visit days in a run", "flame", 14, "best_streak"),
    ("streak_30", "30-day streak", "30 visit days in a run", "flame", 30, "best_streak"),
    ("early_bird", "Early bird", "5 check-ins before 7 am", "sunrise", 5, "early"),
    ("night_owl", "Night owl", "5 check-ins after 8 pm", "moon", 5, "late"),
    ("weekend_warrior", "Weekend warrior", "Trained on 4 different weekends", "calendar-heart", 4, "weekends"),
    ("perfect_week", "Perfect week", "Hit your weekly goal", "target", 1, "goal_weeks"),
    ("goal_4_weeks", "On a roll", "Hit your weekly goal 4 weeks", "rocket", 4, "goal_weeks"),
    ("comeback", "Comeback", "Back after 2+ weeks away", "rotate-ccw", 1, "comebacks"),
]


def level_for(xp: int) -> dict:
    idx = max(i for i, (need, _) in enumerate(LEVELS) if xp >= need)
    cur_need, name = LEVELS[idx]
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    return {"level": idx + 1, "name": name, "xp": xp, "level_xp": cur_need,
            "next_level_xp": nxt[0] if nxt else None, "next_name": nxt[1] if nxt else None,
            "progress": round((xp - cur_need) / (nxt[0] - cur_need), 3) if nxt else 1.0}


def member_metrics(db: Session, member: GymMember, gym: Gym, weekly_goal: int) -> dict:
    z = tz(gym.timezone)
    ats = db.scalars(select(Checkin.at).where(Checkin.member_id == member.id, Checkin.result == "success")
                     .order_by(Checkin.at)).all()
    local = [a.astimezone(z) for a in ats]
    days = sorted({a.date() for a in local})
    today = today_in(gym.timezone)
    cur, best = streaks(days, today)
    best = max(best, member.best_streak or 0)
    first_hour = {}
    for a in local:
        first_hour.setdefault(a.date(), a.hour)
    weeks = Counter(week_start(d) for d in days)
    goal_weeks = sum(1 for n in weeks.values() if n >= max(1, weekly_goal))
    comebacks = sum(1 for a, b in zip(days, days[1:]) if (b - a).days >= 15)
    weekends = len({week_start(d) for d in days if d.weekday() >= 5})
    return {"visits": len(days), "streak": cur, "best_streak": best,
            "early": sum(1 for h in first_hour.values() if h < 7), "late": sum(1 for h in first_hour.values() if h >= 20),
            "weekends": weekends, "goal_weeks": goal_weeks, "comebacks": comebacks, "_days": days, "_runs": _runs(days)}


def xp_from(m: dict) -> int:
    # 10 XP per visit day · +5 per day inside a run of 3+ · +50 per goal week · +25 per comeback
    run_bonus = sum(max(0, r - 2) * 5 for r in m["_runs"])
    return m["visits"] * 10 + run_bonus + m["goal_weeks"] * 50 + m["comebacks"] * 25


def badges_for(m: dict, earned: dict) -> list[dict]:
    out = []
    for key, name, desc, icon, target, metric in BADGES:
        val = m.get(metric, 0)
        done = val >= target
        out.append({"key": key, "name": name, "description": desc, "icon": icon, "earned": done,
                    "earned_at": earned.get(key) if done else None,
                    "progress": min(1.0, round(val / target, 3)), "current": min(val, target), "target": target})
    return out


def sync_badges(db: Session, member: GymMember, gym: Gym, weekly_goal: int) -> tuple[dict, list[dict], dict]:
    """Recompute; persist newly earned badges. Returns (metrics, newly_earned, level)."""
    m = member_metrics(db, member, gym, weekly_goal)
    earned = dict(member.badges or {})
    new = []
    now = utcnow().isoformat()
    for b in badges_for(m, earned):
        if b["earned"] and b["key"] not in earned:
            earned[b["key"]] = now
            new.append(b)
    if new:
        member.badges = earned
    return m, new, level_for(xp_from(m))


def display_name(name: str | None) -> str:
    parts = (name or "Member").split()
    return parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")


def leaderboard(db: Session, gym: Gym, me: GymMember | None, period: str = "month", limit: int = 20) -> dict:
    today = today_in(gym.timezone)
    start = today.replace(day=1) if period == "month" else week_start(today)
    rows = db.execute(select(Checkin.member_id, func.count(func.distinct(Checkin.local_date))).where(
        Checkin.gym_id == gym.id, Checkin.result == "success", Checkin.local_date >= start)
        .group_by(Checkin.member_id)).all()
    counts = {mid: int(n) for mid, n in rows if mid}
    members = {m.id: (m, u) for m, u in db.execute(select(GymMember, User).join(User, User.id == GymMember.user_id).where(
        GymMember.id.in_(list(counts) or [""]), GymMember.status == "active", GymMember.claim_disputed_at.is_(None))).all()}
    ranked = sorted(((mid, n) for mid, n in counts.items() if mid in members),
                    key=lambda x: (-x[1], members[x[0]][0].display_name))
    out, me_row, rank, prev = [], None, 0, None
    for i, (mid, n) in enumerate(ranked, start=1):
        rank = rank if n == prev else i
        prev = n
        m, u = members[mid]
        visible = u.show_on_leaderboard or (me and mid == me.id)
        row = {"rank": rank, "visits": n, "is_me": bool(me and mid == me.id),
               "name": display_name(m.display_name) if visible else "Hidden member", "hidden": not visible}
        if row["is_me"]:
            me_row = row
        if len(out) < limit:
            out.append(row)
    return {"period": period, "from": start.isoformat(), "items": out, "me": me_row, "participants": len(ranked)}
