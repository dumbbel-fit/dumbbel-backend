"""“How crowded is the gym now?” — estimated from check-ins (there's no check-out).

inside_now   = members with a successful check-in in the last `avg_session_min` (default 75) minutes.
typical      = the same estimate at the same time on the same weekday over the last 4 weeks (average).
busy level   = inside_now vs the gym's capacity (if set) or vs a "busy hour" for this gym (90th percentile
               of daily peaks over 4 weeks). quiet < 35 % ≤ moderate < 65 % ≤ busy < 90 % ≤ packed.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Checkin, Gym
from app.services.core import gym_settings
from app.utils import tz, utcnow

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LEVELS = [(0.35, "quiet", "Quiet", "Plenty of free equipment"), (0.65, "moderate", "Moderate", "Some machines in use"),
          (0.90, "busy", "Busy", "Expect short waits"), (9.99, "packed", "Packed", "Peak time — expect waits")]


def _open_window(gym: Gym, d: date) -> tuple[time, time]:
    hrs = (gym.hours or {}).get(DAY_KEYS[d.weekday()]) if isinstance(gym.hours, dict) else None
    try:
        if hrs:
            hrs = [(h["open"], h["close"]) if isinstance(h, dict) else h for h in (hrs if isinstance(hrs, list) else [hrs])
                   if not (isinstance(h, dict) and (h.get("closed") or not h.get("open")))]
            o = min(h[0] for h in hrs)
            c = max(h[1] for h in hrs)
            oh, om = map(int, o.split(":"))
            ch, cm = map(int, c.split(":"))
            return time(oh, om), time(min(ch, 23), cm if ch < 24 else 59)
    except Exception:
        pass
    return time(5, 0), time(23, 0)


def _level(pct: float | None):
    if pct is None:
        return {"key": "unknown", "label": "Not enough data", "hint": "We’ll show how busy it is after a few days of check-ins"}
    for cut, key, label, hint in LEVELS:
        if pct < cut:
            return {"key": key, "label": label, "hint": hint}


def gym_crowd(db: Session, gym: Gym, now: datetime | None = None) -> dict:
    now = now or utcnow()
    z = tz(gym.timezone)
    s = gym_settings(gym)
    session = int(s.get("avg_session_min", 75))
    capacity = s.get("capacity")
    win = timedelta(minutes=session)
    since = now - timedelta(days=29)
    rows = db.execute(select(Checkin.member_id, Checkin.at).where(
        Checkin.gym_id == gym.id, Checkin.result == "success", Checkin.at >= since)).all()
    by_day: dict[date, list[tuple[str, datetime]]] = defaultdict(list)
    for mid, at in rows:
        by_day[at.astimezone(z).date()].append((mid, at))

    def occ(day_rows, t: datetime) -> int:
        return len({m for m, a in day_rows if t - win <= a <= t})

    now_count = len({m for m, a in rows if now - win <= a <= now})
    local_now = now.astimezone(z)
    today = local_now.date()
    past = [today - timedelta(days=7 * k) for k in range(1, 5)]
    past = [d for d in past if by_day.get(d)]  # only days the gym actually had check-ins
    o, c = _open_window(gym, today)

    # typical occupancy for each half-hour slot today (same weekday, last 4 weeks)
    slots = []
    t = datetime.combine(today, o, z)
    end = datetime.combine(today, c, z)
    while t <= end:
        typ = round(sum(occ(by_day[d], t - (datetime.combine(today, time(0), z) - datetime.combine(d, time(0), z)))
                        for d in past) / len(past), 1) if past else None
        slots.append((t, typ))
        t += timedelta(minutes=30)

    # "busy hour" reference: 90th percentile of daily peaks (hourly samples) over 4 weeks
    peaks = []
    for d, dr in by_day.items():
        if d == today:
            continue
        do, dc = _open_window(gym, d)
        tt = datetime.combine(d, do, z)
        best = 0
        while tt <= datetime.combine(d, dc, z):
            best = max(best, occ(dr, tt))
            tt += timedelta(hours=1)
        peaks.append(best)
    peaks.sort()
    busy_ref = peaks[int(0.9 * (len(peaks) - 1))] if len(peaks) >= 3 else None
    ref = float(capacity) if capacity else (max(busy_ref, 3) if busy_ref else None)
    pct = (now_count / ref) if ref else None

    typical_now = None
    if past:
        typical_now = round(sum(occ(by_day[d], now - timedelta(days=(today - d).days)) for d in past) / len(past), 1)
    trend = None
    if typical_now is not None:
        diff = now_count - typical_now
        trend = "busier" if diff >= max(2, typical_now * 0.25) else "quieter" if diff <= -max(2, typical_now * 0.25) else "usual"

    is_open = o <= local_now.time() <= c
    upcoming = [(t, v) for t, v in slots if t >= local_now - timedelta(minutes=15) and v is not None]
    best_times = sorted(upcoming[1:] if len(upcoming) > 1 else upcoming, key=lambda x: (x[1], x[0]))[:3]
    lvl = _level(pct)
    if not is_open:
        lvl = {"key": "closed", "label": "Closed now", "hint": f"Opens at {o.strftime('%-I:%M %p')}"}
    return {
        "gym": {"id": gym.id, "name": gym.name},
        "now_count": now_count, "level": lvl["key"], "label": lvl["label"], "hint": lvl["hint"],
        "percent": round(min(pct, 1.0) * 100) if pct is not None else None,
        "capacity": capacity, "typical_now": typical_now, "trend": trend, "is_open": is_open,
        "open_from": o.strftime("%H:%M"), "open_until": c.strftime("%H:%M"),
        "today": [{"time": t.strftime("%H:%M"), "label": t.strftime("%-I %p").lower() if t.minute == 0 else "",
                   "typical": v, "is_now": t <= local_now < t + timedelta(minutes=30)} for t, v in slots],
        "best_times": [{"time": t.strftime("%H:%M"), "label": t.strftime("%-I:%M %p"), "typical": v} for t, v in best_times],
        "updated_at": now.isoformat(),
        "rule": f"Members who checked in during the last {session} min. “Usual” = same time on the last "
                f"{len(past) or 4} {local_now.strftime('%A')}s.",
        "has_history": bool(past),
    }
