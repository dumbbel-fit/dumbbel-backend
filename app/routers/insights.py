"""DUMBBEL.fit executive console — decision insights (platform admins only).

  actions     who the team should contact today, and why (with a follow-up log)
  forecast    money due to renew in the next 30/60/90 days, expected collection, and what already leaked
  cohorts     retention of gyms and members by the month they joined
  adoption    which gym uses which part of the product
  benchmarks  every gym against the platform and its city
  impact      what WhatsApp reminders and Explore earned gyms
  demand      where people search in Explore versus where the gyms are

Every number comes straight from the tables — nothing is modelled beyond `expiring value × past renewal rate`,
and the response always says which rate was used. Money is in paise; days are IST."""
import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import platform_admin
from app.errors import ApiError
from app.models import ExecFollowup, Gym, GymPhoto, Plan, User
from app.responses import DocRoute, ERRORS
from app.routers.explore import haversine_km, listed, price_from
from app.services.audit import audit
from app.services.cache import cache
from app.services import testgyms
from app.services.storage import existing_keys
from app.utils import iso, utcnow

router = APIRouter(prefix="/internal/insights", tags=["Executive console"], route_class=DocRoute, responses=ERRORS)
IST = timezone(timedelta(hours=5, minutes=30))
RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
Range = Literal["7d", "30d", "90d", "365d"]
TTL = 120
MIN_SAMPLE = 5        # ended memberships a gym needs before its own renewal rate is trusted
GRACE_DAYS = 14       # a membership that ended this recently may still renew — not counted as churn yet
RATE_WINDOW = 180
# what a membership is worth if it renews: what the member paid, else today's plan price (imported rows carry ₹0)
VALUE = ("CASE WHEN m.price_paise > 0 THEN m.price_paise ELSE COALESCE(LEAST(pl.price_paise, "
         "COALESCE(pl.offer_price_paise, pl.price_paise)), 0) END")
NO_NEXT = ("NOT EXISTS (SELECT 1 FROM memberships n WHERE n.member_id = m.member_id AND n.id <> m.id "
           "AND n.state <> 'cancelled' AND n.start_date > m.start_date)")


def today_ist() -> date:
    return datetime.now(IST).date()


def utc_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def rows(db: Session, sql: str, **p) -> list[dict]:
    """Platform numbers leave test gyms out (app/services/testgyms.py); queries for one gym (`:g`) don't."""
    return [dict(r._mapping) for r in db.execute(text(testgyms.scope(sql, p)), p)]


def by_gym(db: Session, sql: str, **p) -> dict[str, dict]:
    return {r["gym_id"]: r for r in rows(db, sql, **p)}


def ts(v) -> str | None:
    return iso(v.replace(tzinfo=timezone.utc)) if v else None


def pct(a, b, nd=1):
    return round(a / b * 100, nd) if b else None


def ckey(key: str) -> str:
    """Cache key tagged with the current set of test gyms, so marking a gym as test refreshes every cached number."""
    from app.db import session_scope
    with session_scope() as sdb:
        return f"{key}:{testgyms.cache_tag(sdb)}"


def cached(key: str, refresh: bool, build):
    key = ckey(key)
    if not refresh:
        hit = cache.get(key)
        if hit:
            return json.loads(hit)
    out = build()
    out["generated_at"] = iso(utcnow())
    cache.set(key, json.dumps(out, default=str), ex=TTL)
    return out


def gym_base(db: Session, statuses=("active",)) -> list[dict]:
    marks = ",".join(f"'{s}'" for s in statuses)  # fixed literals, not user input
    return rows(db, f"""SELECT g.id, g.name, g.area, g.city, g.status, g.code_prefix, g.created_at, g.approved_at, g.lat, g.lng,
                               u.name owner_name, u.phone owner_phone, u.last_login_at owner_last_login
                        FROM gyms g LEFT JOIN users u ON u.id = g.owner_user_id WHERE g.status IN ({marks})""")


def renewal_rates(db: Session, td: date) -> tuple[dict[str, dict], dict]:
    """Share of ended memberships that were followed by another one, per gym, over the last 180 days.
    Memberships that ended in the last 14 days are left out — they may still renew."""
    data = rows(db, f"""SELECT m.gym_id, COUNT(*) ended,
                               SUM(CASE WHEN {NO_NEXT} THEN 0 ELSE 1 END) renewed
                        FROM memberships m WHERE m.state <> 'cancelled' AND m.end_date >= :a AND m.end_date <= :b
                        GROUP BY m.gym_id""", a=td - timedelta(days=RATE_WINDOW), b=td - timedelta(days=GRACE_DAYS + 1))
    ended = sum(int(r["ended"]) for r in data)
    renewed = sum(int(r["renewed"] or 0) for r in data)
    platform = {"ended": ended, "renewed": renewed, "rate_pct": pct(renewed, ended) if ended >= MIN_SAMPLE else None}
    per = {r["gym_id"]: {"ended": int(r["ended"]), "renewed": int(r["renewed"] or 0),
                         "rate_pct": pct(int(r["renewed"] or 0), int(r["ended"])) if int(r["ended"]) >= MIN_SAMPLE else None}
           for r in data}
    return per, platform


def rate_for(gym_id: str, per: dict, platform: dict) -> tuple[float, str]:
    """(rate %, basis) — the gym's own history when there is enough of it, else the platform's, else an assumption."""
    own = per.get(gym_id, {}).get("rate_pct")
    if own is not None:
        return own, "gym"
    if platform["rate_pct"] is not None:
        return platform["rate_pct"], "platform"
    return 50.0, "assumed"


# =====================================================================================================
# 1. Today's call list
# =====================================================================================================
REASONS = {
    "pending_approval": "Waiting for approval",
    "visits_waiting": "Explore visit requests unanswered",
    "whatsapp_broken": "WhatsApp stopped working",
    "gone_quiet": "No check-ins for a week",
    "no_members": "Approved but no members yet",
    "checkins_drop": "Check-ins falling",
    "payments_stopped": "Stopped recording payments",
    "upi_backlog": "UPI payments not verified",
    "renewals_no_whatsapp": "Renewals due, no WhatsApp reminders",
    "checkin_unused": "Check-in never used",
    "owner_inactive": "Owner hasn’t opened the app",
    "no_plans": "No plans set up",
    "lapsed_winnable": "Recent lapses still winnable",
}


def _inr(paise) -> str:
    return "₹" + f"{round(int(paise or 0) / 100):,}"


def build_actions(db: Session) -> dict:
    td = today_ist()
    now = utcnow().replace(tzinfo=None)
    P = {"td": td, "d14": td - timedelta(days=14), "d28": td - timedelta(days=28), "d7e": td + timedelta(days=7),
         "u30": utc_start(td - timedelta(days=29)), "day_ago": now - timedelta(hours=24)}
    gyms = gym_base(db, ("active", "pending"))
    members = by_gym(db, "SELECT gym_id, COUNT(*) n FROM gym_members WHERE status='active' GROUP BY gym_id")
    plans = by_gym(db, "SELECT gym_id, COUNT(*) n FROM plans WHERE active=1 GROUP BY gym_id")
    ci = by_gym(db, """SELECT gym_id, MAX(at) last_at, SUM(local_date >= :d14) cur, SUM(local_date >= :d28 AND local_date < :d14) prev
                       FROM checkins WHERE result='success' GROUP BY gym_id""", **P)
    pay = by_gym(db, """SELECT gym_id, MAX(paid_at) last_at, COALESCE(SUM(CASE WHEN paid_at >= :u30 THEN amount_paise ELSE 0 END),0) gmv30
                        FROM payments WHERE status='recorded' AND source<>'import' GROUP BY gym_id""", **P)
    exp = by_gym(db, f"""SELECT m.gym_id, COUNT(*) n, COALESCE(SUM({VALUE}),0) v FROM memberships m LEFT JOIN plans pl ON pl.id=m.plan_id
                         WHERE m.state<>'cancelled' AND m.end_date >= :td AND m.end_date < :d7e AND {NO_NEXT} GROUP BY m.gym_id""", **P)
    lapsed = by_gym(db, f"""SELECT m.gym_id, COUNT(*) n, COALESCE(SUM({VALUE}),0) v FROM memberships m LEFT JOIN plans pl ON pl.id=m.plan_id
                            WHERE m.state<>'cancelled' AND m.end_date >= :d14 AND m.end_date < :td AND {NO_NEXT} GROUP BY m.gym_id""", **P)
    wa = by_gym(db, "SELECT gym_id, status, provider, last_error FROM gym_whatsapp")
    upi = by_gym(db, """SELECT gym_id, COUNT(*) n, COALESCE(SUM(amount_paise),0) v FROM upi_payment_requests
                        WHERE status='claimed' AND claimed_at < :day_ago GROUP BY gym_id""", **P)
    visits = by_gym(db, """SELECT gym_id, COUNT(*) n FROM gym_visits WHERE status='requested' AND created_at < :day_ago
                           AND visit_date >= :d14 GROUP BY gym_id""", **P)
    snoozed: dict[str, set] = defaultdict(set)
    for f in rows(db, "SELECT gym_id, reason FROM exec_followups WHERE action='snoozed' AND snooze_until >= :td", **P):
        snoozed[f["gym_id"]].add(f["reason"] or "*")
    last_touch = {f["gym_id"]: f for f in rows(db, """SELECT f.gym_id, f.action, f.note, f.reason, f.created_at, u.name by_name FROM exec_followups f
        JOIN (SELECT gym_id, MAX(created_at) c FROM exec_followups GROUP BY gym_id) x ON x.gym_id=f.gym_id AND x.c=f.created_at
        LEFT JOIN users u ON u.id=f.by_user_id""")}

    out, hidden = [], 0
    for g in gyms:
        gid, why = g["id"], []
        n_mem = int(members.get(gid, {}).get("n", 0))

        def add(key, detail, sev, impact=0):
            why.append({"key": key, "title": REASONS[key], "detail": detail, "severity": sev, "impact_paise": int(impact or 0)})

        if g["status"] == "pending":
            age_h = (now - g["created_at"]).total_seconds() / 3600
            if age_h >= 24:
                add("pending_approval", f"Registered {int(age_h // 24)} day{'s' if age_h >= 48 else ''} ago and still waiting for review.", 3)
        else:
            c, p = ci.get(gid, {}), pay.get(gid, {})
            cur, prev = int(c.get("cur") or 0), int(c.get("prev") or 0)
            since = g["approved_at"] or g["created_at"]
            live_days = (now - since).days if since else 0
            if visits.get(gid):
                n = int(visits[gid]["n"])
                add("visits_waiting", f"{n} visit request{'s' if n > 1 else ''} from Explore unanswered for over a day. These are people ready to join.", 3)
            w = wa.get(gid)
            if w and w["status"] == "error":
                add("whatsapp_broken", (w["last_error"] or "The linked WhatsApp number needs to be reconnected.")[:160], 3,
                    exp.get(gid, {}).get("v", 0))
            if not n_mem and live_days >= 3:
                add("no_members", f"Approved {live_days} days ago and no members added. Onboarding is stuck.", 3)
            elif n_mem and c.get("last_at") and (now - c["last_at"]).days >= 7:
                add("gone_quiet", f"Last check-in was {(now - c['last_at']).days} days ago, with {n_mem} members on the books.", 3, p.get("gmv30", 0))
            elif n_mem and not c.get("last_at") and live_days >= 7:
                add("checkin_unused", f"{n_mem} members added but nobody has ever checked in.", 2)
            if prev >= 20 and cur < prev * 0.6:
                add("checkins_drop", f"{cur} check-ins in the last 14 days, down from {prev} in the 14 before ({round((cur - prev) / prev * 100)}%).", 2, p.get("gmv30", 0))
            if p.get("last_at") and (now - p["last_at"]).days >= 14 and cur > 0:
                add("payments_stopped", f"Members are checking in but no payment has been recorded for {(now - p['last_at']).days} days.", 2)
            if upi.get(gid):
                n = int(upi[gid]["n"])
                add("upi_backlog", f"{n} UPI payment{'s' if n > 1 else ''} ({_inr(upi[gid]['v'])}) claimed by members and not verified for over a day.", 2, upi[gid]["v"])
            e = exp.get(gid)
            if e and int(e["v"]) > 0 and not (w and w["status"] == "connected"):
                add("renewals_no_whatsapp", f"{int(e['n'])} membership{'s' if int(e['n']) > 1 else ''} worth {_inr(e['v'])} end within 7 days and WhatsApp reminders are off.", 2, e["v"])
            last_login = g["owner_last_login"]
            if live_days >= 14 and (not last_login or (now - last_login).days >= 14):
                add("owner_inactive", "The owner has never signed in." if not last_login else f"The owner last signed in {(now - last_login).days} days ago.", 2)
            if not plans.get(gid) and live_days >= 2:
                add("no_plans", "No active plans, so memberships can’t be sold or shown on Explore.", 2)
            lp = lapsed.get(gid)
            if lp and int(lp["n"]) >= 3:
                add("lapsed_winnable", f"{int(lp['n'])} members lapsed in the last 14 days ({_inr(lp['v'])}). A call or message now usually brings some back.", 1, lp["v"])

        sn = snoozed.get(gid, set())
        if "*" in sn:
            hidden += bool(why)
            continue
        hidden += sum(1 for r in why if r["key"] in sn)
        why = [r for r in why if r["key"] not in sn]
        if not why:
            continue
        why.sort(key=lambda r: (-r["severity"], -r["impact_paise"]))
        impact = max(r["impact_paise"] for r in why)
        score = sum(r["severity"] * 10 for r in why) + min(15, impact // 100000) + min(10, n_mem // 20)
        lt = last_touch.get(gid)
        out.append({"gym_id": gid, "name": g["name"], "area": g["area"], "city": g["city"], "status": g["status"],
                    "owner_name": g["owner_name"], "owner_phone": g["owner_phone"], "members": n_mem,
                    "gmv_30d_paise": int(pay.get(gid, {}).get("gmv30", 0) or 0), "score": int(score),
                    "level": "urgent" if why[0]["severity"] == 3 else "soon" if why[0]["severity"] == 2 else "nice",
                    "impact_paise": impact, "reasons": why,
                    "last_contact": {"action": lt["action"], "note": lt["note"], "reason": lt["reason"], "by": lt["by_name"],
                                     "at": ts(lt["created_at"])} if lt else None})
    out.sort(key=lambda r: -r["score"])
    counts = defaultdict(int)
    for r in out:
        for x in r["reasons"]:
            counts[x["key"]] += 1
    return {"items": out, "snoozed": hidden,
            "summary": {"gyms": len(out), "urgent": sum(1 for r in out if r["level"] == "urgent"),
                        "money_at_stake_paise": sum(r["impact_paise"] for r in out)},
            "reasons": [{"key": k, "title": REASONS[k], "gyms": counts[k]} for k in REASONS if counts[k]]}


@router.get("/actions", summary="Today's call list: gyms the team should contact, ranked, with the reason and the money involved")
def actions(refresh: bool = False, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    return cached("ins:actions", refresh, lambda: build_actions(db))


class FollowupIn(BaseModel):
    gym_id: str
    action: Literal["called", "messaged", "snoozed", "note"]
    reason: str | None = Field(None, max_length=40, description="Alert key this answers; leave empty for the whole gym")
    note: str | None = Field(None, max_length=500)
    snooze_days: int | None = Field(None, ge=1, le=90, description="Required with action=snoozed")


@router.post("/followups", status_code=201, summary="Log a call / message / note for a gym, or snooze an alert")
def add_followup(body: FollowupIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    g = db.get(Gym, body.gym_id)
    if not g:
        raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
    if body.reason and body.reason not in REASONS:
        raise ApiError(422, "VALIDATION", "Unknown alert.")
    if body.action == "snoozed" and not body.snooze_days:
        raise ApiError(422, "VALIDATION", "Choose how long to snooze for.")
    if body.action == "note" and not (body.note or "").strip():
        raise ApiError(422, "VALIDATION", "Write a note first.")
    until = today_ist() + timedelta(days=body.snooze_days) if body.action == "snoozed" else None
    f = ExecFollowup(gym_id=g.id, reason=body.reason, action=body.action, note=(body.note or "").strip() or None,
                     snooze_until=until, by_user_id=admin.id)
    db.add(f)
    what = {"called": "called", "messaged": "messaged", "snoozed": f"snoozed until {until}", "note": "noted"}[body.action]
    audit(db, g.id, admin.id, "console.followup", "gym", g.id, f"Console: {what}" + (f" — {REASONS[body.reason]}" if body.reason else ""))
    db.commit()
    cache.delete(ckey("ins:actions"))
    return {"id": f.id, "gym_id": g.id, "action": f.action, "reason": f.reason, "note": f.note,
            "snooze_until": until.isoformat() if until else None}


@router.get("/followups", summary="Follow-up history for a gym (newest first)")
def list_followups(gym_id: str, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    data = rows(db, """SELECT f.id, f.action, f.reason, f.note, f.snooze_until, f.created_at, u.name by_name FROM exec_followups f
                       LEFT JOIN users u ON u.id=f.by_user_id WHERE f.gym_id=:g ORDER BY f.created_at DESC LIMIT 50""", g=gym_id)
    return {"items": [{"id": r["id"], "action": r["action"], "reason": r["reason"], "reason_title": REASONS.get(r["reason"]),
                       "note": r["note"], "snooze_until": r["snooze_until"].isoformat() if r["snooze_until"] else None,
                       "by": r["by_name"], "at": ts(r["created_at"])} for r in data]}


@router.delete("/followups/snooze", status_code=204, summary="Bring a gym's snoozed alerts back")
def unsnooze(gym_id: str, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    db.execute(text("UPDATE exec_followups SET snooze_until = :y WHERE gym_id=:g AND action='snoozed' AND snooze_until >= :td"),
               {"g": gym_id, "td": today_ist(), "y": today_ist() - timedelta(days=1)})
    db.commit()
    cache.delete(ckey("ins:actions"))


# =====================================================================================================
# 2. Revenue forecast and leak
# =====================================================================================================
def build_forecast(db: Session, horizon: int) -> dict:
    td = today_ist()
    end = td + timedelta(days=horizon)
    per, platform = renewal_rates(db, td)
    gyms = {g["id"]: g for g in gym_base(db)}
    P = {"td": td, "end": end, "d90": td - timedelta(days=90), "d14": td - timedelta(days=GRACE_DAYS),
         "u30": utc_start(td - timedelta(days=29)), "uh": utc_start(td - timedelta(days=horizon - 1))}
    due = rows(db, f"""SELECT m.gym_id, FLOOR(DATEDIFF(m.end_date, :td) / 7) wk, COUNT(*) n, COALESCE(SUM({VALUE}),0) v
                       FROM memberships m LEFT JOIN plans pl ON pl.id=m.plan_id
                       WHERE m.state<>'cancelled' AND m.end_date >= :td AND m.end_date < :end AND {NO_NEXT}
                       GROUP BY m.gym_id, wk""", **P)
    secured = by_gym(db, f"""SELECT m.gym_id, COUNT(*) n FROM memberships m WHERE m.state<>'cancelled' AND m.end_date >= :td
                             AND m.end_date < :end AND NOT ({NO_NEXT}) GROUP BY m.gym_id""", **P)
    leak = rows(db, f"""SELECT m.gym_id, (m.end_date >= :d14) fresh, COUNT(*) n, COALESCE(SUM({VALUE}),0) v
                        FROM memberships m LEFT JOIN plans pl ON pl.id=m.plan_id
                        WHERE m.state<>'cancelled' AND m.end_date >= :d90 AND m.end_date < :td AND {NO_NEXT}
                        GROUP BY m.gym_id, fresh""", **P)
    # money from people buying their first membership — the part of revenue that isn't a renewal
    new30 = by_gym(db, """SELECT p.gym_id, COALESCE(SUM(p.amount_paise),0) v FROM payments p JOIN memberships m ON m.id=p.membership_id
                          WHERE p.status='recorded' AND p.source<>'import' AND p.paid_at >= :u30
                          AND NOT EXISTS (SELECT 1 FROM memberships o WHERE o.member_id=m.member_id AND o.id<>m.id
                                          AND o.state<>'cancelled' AND o.start_date < m.start_date)
                          GROUP BY p.gym_id""", **P)
    actual = by_gym(db, """SELECT gym_id, COALESCE(SUM(amount_paise),0) v FROM payments WHERE status='recorded' AND source<>'import'
                           AND paid_at >= :uh GROUP BY gym_id""", **P)
    wa = by_gym(db, "SELECT gym_id, status FROM gym_whatsapp")

    n_weeks = (horizon + 6) // 7
    weeks = [{"index": i, "from": (td + timedelta(days=i * 7)).isoformat(),
              "to": (min(end, td + timedelta(days=i * 7 + 7)) - timedelta(days=1)).isoformat(),
              "memberships": 0, "due_paise": 0, "expected_paise": 0} for i in range(n_weeks)]
    table: dict[str, dict] = {}

    def row(gid):
        if gid not in table:
            g = gyms[gid]
            rate, basis = rate_for(gid, per, platform)
            table[gid] = {"gym_id": gid, "name": g["name"], "area": g["area"], "city": g["city"], "rate_pct": rate,
                          "rate_basis": basis, "rate_sample": per.get(gid, {}).get("ended", 0), "due": 0, "due_paise": 0,
                          "expected_paise": 0, "already_renewed": int(secured.get(gid, {}).get("n", 0)),
                          "lapsed_recent": 0, "lapsed_recent_paise": 0, "lapsed_old": 0, "lapsed_old_paise": 0,
                          "new_sales_paise": round(int(new30.get(gid, {}).get("v", 0)) * horizon / 30),
                          "actual_prev_paise": int(actual.get(gid, {}).get("v", 0)),
                          "whatsapp": (wa.get(gid) or {}).get("status") == "connected"}
        return table[gid]

    for r in due:
        if r["gym_id"] not in gyms:
            continue
        t = row(r["gym_id"])
        n, v = int(r["n"]), int(r["v"])
        exp = round(v * t["rate_pct"] / 100)
        t["due"] += n
        t["due_paise"] += v
        t["expected_paise"] += exp
        wk = weeks[min(int(r["wk"]), n_weeks - 1)]
        wk["memberships"] += n
        wk["due_paise"] += v
        wk["expected_paise"] += exp
    for r in leak:
        if r["gym_id"] not in gyms:
            continue
        t = row(r["gym_id"])
        k = "lapsed_recent" if r["fresh"] else "lapsed_old"
        t[k] += int(r["n"])
        t[k + "_paise"] += int(r["v"])
    for gid in gyms:
        if gid in new30 or gid in actual:
            row(gid)
    items = sorted(table.values(), key=lambda t: -(t["due_paise"] + t["lapsed_recent_paise"]))
    tot = lambda k: sum(t[k] for t in items)
    return {"horizon_days": horizon, "from": td.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
            "platform_rate": platform, "weeks": weeks, "items": items,
            "totals": {"due": tot("due"), "due_paise": tot("due_paise"), "expected_paise": tot("expected_paise"),
                       "at_risk_paise": tot("due_paise") - tot("expected_paise"), "already_renewed": tot("already_renewed"),
                       "new_sales_paise": tot("new_sales_paise"), "forecast_paise": tot("expected_paise") + tot("new_sales_paise"),
                       "actual_prev_paise": tot("actual_prev_paise"),
                       "lapsed_recent": tot("lapsed_recent"), "lapsed_recent_paise": tot("lapsed_recent_paise"),
                       "lapsed_old": tot("lapsed_old"), "lapsed_old_paise": tot("lapsed_old_paise")},
            "method": {"rate_window_days": RATE_WINDOW, "grace_days": GRACE_DAYS, "min_sample": MIN_SAMPLE,
                       "note": "Expected = value of memberships ending in the period × the gym’s own renewal rate over the last "
                               f"{RATE_WINDOW} days (platform rate when the gym has fewer than {MIN_SAMPLE} ended memberships). "
                               "New sales = first-time membership payments in the last 30 days, scaled to the period."}}


def _horizon(h: int):
    if h not in (30, 60, 90):
        raise ApiError(422, "VALIDATION", "Forecast period must be 30, 60 or 90 days.")


@router.get("/forecast", summary="Renewals due in the next 30/60/90 days, expected collection per gym, and revenue that already lapsed")
def forecast(horizon: int = Query(30, description="30, 60 or 90"), refresh: bool = False, admin: User = Depends(platform_admin),
             db: Session = Depends(get_db)):
    _horizon(horizon)
    return cached(f"ins:fc:{horizon}", refresh, lambda: build_forecast(db, horizon))


@router.get("/forecast/{gymId}", summary="One gym: the members due to renew and the ones who recently lapsed")
def forecast_gym(gymId: str, horizon: int = Query(30, description="30, 60 or 90"), admin: User = Depends(platform_admin),
                 db: Session = Depends(get_db)):
    _horizon(horizon)
    g = db.get(Gym, gymId)
    if not g:
        raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
    td = today_ist()
    P = {"g": gymId, "td": td, "end": td + timedelta(days=horizon), "d90": td - timedelta(days=90)}
    q = f"""SELECT gm.display_name name, gm.member_code code, m.plan_name plan, m.end_date, {VALUE} value_paise, gm.last_checkin_at,
                   (gm.wa_opted_out_at IS NOT NULL) wa_opted_out,
                   (SELECT MAX(w.created_at) FROM wa_messages w WHERE w.gym_id=m.gym_id AND w.member_id=gm.id
                    AND w.event IN ('expiry_reminder','expired') AND w.status IN ('sent','delivered','read')) reminded_at
            FROM memberships m JOIN gym_members gm ON gm.id=m.member_id LEFT JOIN plans pl ON pl.id=m.plan_id
            WHERE m.gym_id=:g AND m.state<>'cancelled' AND {NO_NEXT} AND {{win}} ORDER BY m.end_date {{dir}} LIMIT 200"""

    def shape(rs):
        return [{"name": r["name"], "code": r["code"], "plan": r["plan"], "end_date": r["end_date"].isoformat(),
                 "days": (r["end_date"] - td).days, "value_paise": int(r["value_paise"] or 0),
                 "last_checkin_at": ts(r["last_checkin_at"]), "reminded_at": ts(r["reminded_at"]),
                 "wa_opted_out": bool(r["wa_opted_out"])} for r in rs]
    return {"gym": {"id": g.id, "name": g.name}, "horizon_days": horizon,
            "due": shape(rows(db, q.format(win="m.end_date >= :td AND m.end_date < :end", dir="ASC"), **P)),
            "lapsed": shape(rows(db, q.format(win="m.end_date >= :d90 AND m.end_date < :td", dir="DESC"), **P))}


# =====================================================================================================
# 3. Cohort retention
# =====================================================================================================
def ym(d: date) -> int:
    return d.year * 12 + d.month - 1


def ym_label(i: int) -> str:
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def build_cohorts(db: Session, kind: str, metric: str, months: int, gym_id: str | None, imported: bool) -> dict:
    td = today_ist()
    cur = ym(td)
    first = cur - (months - 1)
    first_day = date(first // 12, first % 12 + 1, 1)
    size: dict[int, int] = defaultdict(int)
    alive: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))  # cohort → month offset → ids

    if kind == "members":
        cond = "gm.joined_on >= :f" + (" AND gm.gym_id = :g" if gym_id else "") + ("" if imported else " AND gm.source <> 'import'")
        P = {"f": first_day, "g": gym_id}
        joined = {r["id"]: ym(r["joined_on"]) for r in rows(db, f"SELECT gm.id, gm.joined_on FROM gym_members gm WHERE {cond}", **P)}
        for c in joined.values():
            size[c] += 1
        if metric == "paying":
            for r in rows(db, f"""SELECT m.member_id, m.start_date, m.end_date FROM memberships m JOIN gym_members gm ON gm.id=m.member_id
                                  WHERE m.state<>'cancelled' AND {cond}""", **P):
                c = joined[r["member_id"]]
                for k in range(max(ym(r["start_date"]), c), min(ym(r["end_date"]), cur) + 1):
                    alive[c][k - c].add(r["member_id"])
        else:
            for r in rows(db, f"""SELECT c.member_id, YEAR(c.local_date) y, MONTH(c.local_date) mo FROM checkins c
                                  JOIN gym_members gm ON gm.id=c.member_id
                                  WHERE c.result='success' AND {cond} GROUP BY c.member_id, y, mo""", **P):
                c, k = joined[r["member_id"]], int(r["y"]) * 12 + int(r["mo"]) - 1
                if c <= k <= cur:
                    alive[c][k - c].add(r["member_id"])
    else:
        started = {}
        for r in rows(db, """SELECT id, DATE(DATE_ADD(COALESCE(approved_at, created_at), INTERVAL 330 MINUTE)) d FROM gyms
                             WHERE status IN ('active','suspended') AND COALESCE(approved_at, created_at) >= :f""", f=utc_start(first_day)):
            started[r["id"]] = ym(r["d"])
            size[started[r["id"]]] += 1
        seen = rows(db, "SELECT gym_id, YEAR(local_date) y, MONTH(local_date) mo FROM checkins WHERE result='success' AND local_date >= :f GROUP BY gym_id, y, mo", f=first_day)
        if metric != "checkins":
            seen += rows(db, """SELECT gym_id, YEAR(DATE_ADD(paid_at, INTERVAL 330 MINUTE)) y, MONTH(DATE_ADD(paid_at, INTERVAL 330 MINUTE)) mo
                                FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :f GROUP BY gym_id, y, mo""", f=utc_start(first_day))
        for r in seen:
            c = started.get(r["gym_id"])
            k = int(r["y"]) * 12 + int(r["mo"]) - 1
            if c is not None and c <= k <= cur:
                alive[c][k - c].add(r["gym_id"])

    cohorts = []
    for c in range(first, cur + 1):
        n = size.get(c, 0)
        cells = [{"k": k, "n": len(alive[c][k]), "pct": pct(len(alive[c][k]), n, 0), "partial": c + k == cur}
                 for k in range(cur - c + 1)]
        cohorts.append({"cohort": ym_label(c), "size": n, "cells": cells})
    average = []
    for k in range(months):
        full = [(co["cells"][k]["n"], co["size"]) for co in cohorts if len(co["cells"]) > k and co["size"] and not co["cells"][k]["partial"]]
        average.append({"k": k, "pct": pct(sum(a for a, _ in full), sum(b for _, b in full), 0) if full else None,
                        "cohorts": len(full)})
    return {"kind": kind, "metric": metric, "months": months, "gym_id": gym_id, "include_imported": imported,
            "cohorts": cohorts, "average": average, "total": sum(size.values())}


@router.get("/cohorts", summary="Retention by join month — members (still paying / still visiting) or gyms (still active)")
def cohorts(kind: Literal["members", "gyms"] = "members", metric: Literal["paying", "visiting", "active", "checkins"] | None = None,
            months: int = Query(6, description="6 or 12"), gym_id: str | None = None, include_imported: bool = False, refresh: bool = False,
            admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    if months not in (6, 12):
        raise ApiError(422, "VALIDATION", "Show 6 or 12 months.")
    if kind == "members":
        metric = metric if metric in ("paying", "visiting") else "paying"
    else:
        metric, gym_id = metric if metric in ("active", "checkins") else "active", None
    return cached(f"ins:co:{kind}:{metric}:{months}:{gym_id}:{include_imported}", refresh,
                  lambda: build_cohorts(db, kind, metric, months, gym_id, include_imported))


# =====================================================================================================
# 4. Feature adoption
# =====================================================================================================
FEATURES = [
    ("plans", "Plans", "Has at least one active plan"),
    ("members", "Members", "Members added; active = new members in the last 30 days"),
    ("checkins", "Check-ins", "Any check-in; active = in the last 30 days"),
    ("gym_qr", "Gym QR", "Members scan the gym’s QR themselves"),
    ("payments", "Payments", "Payments recorded; active = in the last 30 days"),
    ("upi", "UPI in app", "UPI ID added; active = app UPI payments verified in the last 30 days"),
    ("member_app", "Member app", "Members signed in; active = 20% or more of members"),
    ("whatsapp", "WhatsApp", "Number linked; active = messages sent in the last 30 days"),
    ("explore", "Explore listing", "Listed; active = location and 3+ photos"),
    ("visits", "Visit bookings", "Visits booked from Explore; active = in the last 30 days"),
    ("staff", "Staff", "Staff accounts besides the owner"),
]


def build_adoption(db: Session) -> dict:
    td = today_ist()
    P = {"d30": td - timedelta(days=29), "u30": utc_start(td - timedelta(days=29))}
    gyms = gym_base(db)
    plans = by_gym(db, "SELECT gym_id, COUNT(*) n FROM plans WHERE active=1 GROUP BY gym_id")
    mem = by_gym(db, """SELECT gm.gym_id, COUNT(*) n, SUM(gm.created_at >= :u30) new, SUM(u.first_login_at IS NOT NULL) on_app
                        FROM gym_members gm JOIN users u ON u.id=gm.user_id WHERE gm.status='active' GROUP BY gm.gym_id""", **P)
    ci = by_gym(db, """SELECT gym_id, COUNT(*) n, SUM(local_date >= :d30) n30, SUM(method='self') self_n,
                       SUM(method='self' AND local_date >= :d30) self30 FROM checkins WHERE result='success' GROUP BY gym_id""", **P)
    pay = by_gym(db, "SELECT gym_id, COUNT(*) n, SUM(paid_at >= :u30) n30 FROM payments WHERE status='recorded' AND source<>'import' GROUP BY gym_id", **P)
    upi_ids = by_gym(db, "SELECT gym_id, COUNT(*) n FROM gym_upi_accounts WHERE active=1 GROUP BY gym_id")
    upi_ok = by_gym(db, "SELECT gym_id, COUNT(*) n FROM upi_payment_requests WHERE status='verified' AND decided_at >= :u30 GROUP BY gym_id", **P)
    wa = by_gym(db, "SELECT gym_id, status FROM gym_whatsapp")
    wa_sent = by_gym(db, "SELECT gym_id, COUNT(*) n FROM wa_messages WHERE status IN ('sent','delivered','read') AND created_at >= :u30 GROUP BY gym_id", **P)
    vis = by_gym(db, "SELECT gym_id, COUNT(*) n, SUM(created_at >= :u30) n30 FROM gym_visits GROUP BY gym_id", **P)
    staff = by_gym(db, "SELECT gym_id, COUNT(*) n FROM gym_staff WHERE role='staff' AND status<>'deactivated' GROUP BY gym_id")
    photos = db.execute(select(GymPhoto.gym_id, GymPhoto.file_key)).all()
    real = existing_keys([k for _, k in photos])
    n_photos: dict[str, int] = defaultdict(int)
    for gid, k in photos:
        n_photos[gid] += k in real
    orm = {g.id: g for g in db.scalars(select(Gym).where(Gym.status == "active", Gym.is_test.is_(False)))}

    def lvl(any_, active):
        return 2 if active else 1 if any_ else 0

    items = []
    for g in gyms:
        gid = g["id"]
        m, c, p = mem.get(gid, {}), ci.get(gid, {}), pay.get(gid, {})
        n_mem, on_app = int(m.get("n") or 0), int(m.get("on_app") or 0)
        is_listed = gid in orm and listed(orm[gid])
        f = {"plans": 2 if plans.get(gid) else 0,
             "members": lvl(n_mem, int(m.get("new") or 0)),
             "checkins": lvl(c.get("n"), int(c.get("n30") or 0)),
             "gym_qr": lvl(int(c.get("self_n") or 0), int(c.get("self30") or 0)),
             "payments": lvl(p.get("n"), int(p.get("n30") or 0)),
             "upi": lvl(upi_ids.get(gid), upi_ok.get(gid)),
             "member_app": lvl(on_app, n_mem and on_app / n_mem >= 0.2),
             "whatsapp": lvl((wa.get(gid) or {}).get("status") == "connected", (wa.get(gid) or {}).get("status") == "connected" and wa_sent.get(gid)),
             "explore": lvl(is_listed, is_listed and g["lat"] is not None and n_photos[gid] >= 3),
             "visits": lvl(vis.get(gid), int((vis.get(gid) or {}).get("n30") or 0)),
             "staff": 2 if staff.get(gid) else 0}
        items.append({"gym_id": gid, "name": g["name"], "area": g["area"], "city": g["city"], "members": n_mem,
                      "owner_name": g["owner_name"], "owner_phone": g["owner_phone"], "features": f,
                      "score": round(sum(f.values()) / (2 * len(FEATURES)) * 100),
                      "live_days": (utcnow().replace(tzinfo=None) - (g["approved_at"] or g["created_at"])).days})
    items.sort(key=lambda r: (r["score"], -r["members"]))
    feats = []
    for key, label, hint in FEATURES:
        c2 = sum(1 for r in items if r["features"][key] == 2)
        c1 = sum(1 for r in items if r["features"][key] == 1)
        feats.append({"key": key, "label": label, "hint": hint, "active": c2, "setup": c1, "none": len(items) - c1 - c2,
                      "active_pct": pct(c2, len(items), 0)})
    return {"features": feats, "items": items, "gyms": len(items),
            "avg_score": round(statistics.mean(r["score"] for r in items)) if items else None}


@router.get("/adoption", summary="Feature adoption grid: every live gym against every part of the product (0 none, 1 set up, 2 in use)")
def adoption(refresh: bool = False, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    return cached("ins:adoption", refresh, lambda: build_adoption(db))


# =====================================================================================================
# 5. Benchmarks
# =====================================================================================================
METRICS = [
    {"key": "renewal_rate_pct", "label": "Renewal rate", "unit": "pct", "better": "high"},
    {"key": "visits_per_week", "label": "Visits per member / week", "unit": "num", "better": "high"},
    {"key": "revenue_per_member_paise", "label": "Revenue per active member (30d)", "unit": "inr", "better": "high"},
    {"key": "app_adoption_pct", "label": "Members on the app", "unit": "pct", "better": "high"},
    {"key": "price_month_paise", "label": "Monthly price (cheapest plan)", "unit": "inr", "better": None},
    {"key": "active_members", "label": "Active members", "unit": "num", "better": "high"},
]


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def build_benchmarks(db: Session) -> dict:
    td = today_ist()
    P = {"td": td, "d30": td - timedelta(days=29), "u30": utc_start(td - timedelta(days=29))}
    per, platform = renewal_rates(db, td)
    gyms = gym_base(db)
    active = by_gym(db, "SELECT gym_id, COUNT(DISTINCT member_id) n FROM memberships WHERE state<>'cancelled' AND start_date<=:td AND end_date>=:td GROUP BY gym_id", **P)
    mem = by_gym(db, """SELECT gm.gym_id, COUNT(*) n, SUM(u.first_login_at IS NOT NULL) on_app FROM gym_members gm JOIN users u ON u.id=gm.user_id
                        WHERE gm.status='active' GROUP BY gm.gym_id""")
    ci = by_gym(db, "SELECT gym_id, COUNT(*) n FROM checkins WHERE result='success' AND local_date >= :d30 GROUP BY gym_id", **P)
    pay = by_gym(db, "SELECT gym_id, COALESCE(SUM(amount_paise),0) v FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :u30 GROUP BY gym_id", **P)
    plans: dict[str, list] = defaultdict(list)
    for p in db.scalars(select(Plan).where(Plan.active.is_(True), Plan.gym_id.not_in(select(Gym.id).where(Gym.is_test.is_(True))))):
        plans[p.gym_id].append(p)
    items = []
    for g in gyms:
        gid = g["id"]
        act = int(active.get(gid, {}).get("n", 0))
        n_mem, on_app = int(mem.get(gid, {}).get("n") or 0), int(mem.get(gid, {}).get("on_app") or 0)
        pf = price_from(plans.get(gid, []))
        items.append({"gym_id": gid, "name": g["name"], "area": g["area"], "city": (g["city"] or "").strip().title() or None,
                      "members": n_mem, "active_members": act,
                      "renewal_rate_pct": per.get(gid, {}).get("rate_pct"), "renewal_sample": per.get(gid, {}).get("ended", 0),
                      "visits_per_week": round(int(ci.get(gid, {}).get("n", 0)) / act / (30 / 7), 2) if act else None,
                      "revenue_per_member_paise": round(int(pay.get(gid, {}).get("v", 0)) / act) if act else None,
                      "app_adoption_pct": pct(on_app, n_mem),
                      "price_month_paise": pf["per_month_paise"] if pf else None})
    keys = [m["key"] for m in METRICS]
    for k in keys:  # percentile: share of gyms this gym is at or above
        vals = sorted(r[k] for r in items if r[k] is not None)
        for r in items:
            r.setdefault("percentile", {})[k] = round(sum(1 for v in vals if v <= r[k]) / len(vals) * 100) if r[k] is not None and vals else None
    by_city: dict[str, list] = defaultdict(list)
    for r in items:
        if r["city"]:
            by_city[r["city"]].append(r)
    return {"metrics": METRICS, "items": items, "platform": {**{k: _median([r[k] for r in items]) for k in keys}, "gyms": len(items)},
            "platform_renewal": platform,
            "cities": sorted([{"city": c, "gyms": len(rs), **{k: _median([r[k] for r in rs]) for k in keys}}
                              for c, rs in by_city.items()], key=lambda c: -c["gyms"])}


@router.get("/benchmarks", summary="Every live gym against the platform median and its city: renewal, attendance, revenue, price")
def benchmarks(refresh: bool = False, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    return cached("ins:bench", refresh, lambda: build_benchmarks(db))


# =====================================================================================================
# 6. Impact of WhatsApp and Explore
# =====================================================================================================
def build_impact(db: Session, rng: str) -> dict:
    td = today_ist()
    s = utc_start(td - timedelta(days=RANGES[rng] - 1))
    names = {g["id"]: g for g in gym_base(db, ("active", "suspended"))}
    # ---- WhatsApp
    conn = rows(db, "SELECT provider, status, COUNT(*) n FROM gym_whatsapp GROUP BY provider, status")
    msgs = rows(db, """SELECT gym_id, event, COUNT(*) total, SUM(status IN ('sent','delivered','read')) sent,
                       SUM(status IN ('delivered','read')) delivered, SUM(status='read') read_n, SUM(status='failed') failed
                       FROM wa_messages WHERE created_at >= :s GROUP BY gym_id, event""", s=s)
    links = rows(db, f"""SELECT t.gym_id, COUNT(*) made, SUM(t.opened_at IS NOT NULL) opened, SUM(t.used_at IS NOT NULL) used,
                         COALESCE(SUM(CASE WHEN t.used_at IS NOT NULL THEN {VALUE} ELSE 0 END),0) v
                         FROM renewal_tokens t LEFT JOIN memberships m ON m.id=t.result_membership_id LEFT JOIN plans pl ON pl.id=m.plan_id
                         WHERE t.created_at >= :s GROUP BY t.gym_id""", s=s)
    # a membership that started being sold within 14 days of a delivered reminder, counted once
    after = rows(db, f"""SELECT x.gym_id, COUNT(*) n, COALESCE(SUM(x.v),0) v FROM (
                           SELECT DISTINCT m.id, m.gym_id, {VALUE} v FROM wa_messages w
                           JOIN memberships m ON m.member_id = w.member_id AND m.gym_id = w.gym_id AND m.state<>'cancelled'
                                AND m.created_at > w.created_at AND m.created_at <= DATE_ADD(w.created_at, INTERVAL 14 DAY)
                           LEFT JOIN plans pl ON pl.id=m.plan_id
                           WHERE w.created_at >= :s AND w.event IN ('expiry_reminder','expired','away_nudge')
                                 AND w.status IN ('sent','delivered','read')) x GROUP BY x.gym_id""", s=s)
    wa_gym: dict[str, dict] = defaultdict(lambda: {"sent": 0, "delivered": 0, "read": 0, "failed": 0, "links": 0, "link_opens": 0,
                                                   "link_renewals": 0, "link_paise": 0, "after_reminder": 0, "after_reminder_paise": 0})
    by_event: dict[str, dict] = defaultdict(lambda: {"sent": 0, "delivered": 0, "read": 0, "failed": 0})
    for r in msgs:
        for dst in (wa_gym[r["gym_id"]], by_event[r["event"]]):
            dst["sent"] += int(r["sent"] or 0)
            dst["delivered"] += int(r["delivered"] or 0)
            dst["read"] += int(r["read_n"] or 0)
            dst["failed"] += int(r["failed"] or 0)
    for r in links:
        w = wa_gym[r["gym_id"]]
        w["links"], w["link_opens"], w["link_renewals"], w["link_paise"] = int(r["made"]), int(r["opened"] or 0), int(r["used"] or 0), int(r["v"])
    for r in after:
        wa_gym[r["gym_id"]]["after_reminder"], wa_gym[r["gym_id"]]["after_reminder_paise"] = int(r["n"]), int(r["v"])
    wsum = lambda k: sum(w[k] for w in wa_gym.values())
    connected = sum(r["n"] for r in conn if r["status"] == "connected")
    whatsapp = {"gyms_connected": connected, "gyms_total": sum(1 for g in names.values() if g["status"] == "active"),
                "by_provider": {p: sum(r["n"] for r in conn if r["status"] == "connected" and r["provider"] == p) for p in ("web", "cloud")},
                "sent": wsum("sent"), "delivered": wsum("delivered"), "read": wsum("read"), "failed": wsum("failed"),
                "delivery_pct": pct(wsum("delivered"), wsum("sent")), "read_pct": pct(wsum("read"), wsum("sent")),
                "links": wsum("links"), "link_opens": wsum("link_opens"), "link_renewals": wsum("link_renewals"),
                "link_paise": wsum("link_paise"), "after_reminder": wsum("after_reminder"), "after_reminder_paise": wsum("after_reminder_paise"),
                "by_event": [{"event": e, **v} for e, v in sorted(by_event.items(), key=lambda kv: -kv[1]["sent"])],
                "gyms": sorted([{"gym_id": gid, "name": names[gid]["name"], "city": names[gid]["city"], **w}
                                for gid, w in wa_gym.items() if gid in names], key=lambda r: -(r["after_reminder_paise"] + r["sent"]))}
    # ---- Explore
    ev = rows(db, "SELECT kind, gym_id, COUNT(*) n FROM explore_events WHERE created_at >= :s GROUP BY kind, gym_id", s=s)
    vis = rows(db, """SELECT gym_id, status, COUNT(*) n, AVG(CASE WHEN handled_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, created_at, handled_at) END) mins
                      FROM gym_visits WHERE created_at >= :s GROUP BY gym_id, status""", s=s)
    earned = by_gym(db, """SELECT v.gym_id, COUNT(DISTINCT v.member_id) joined, COALESCE(SUM(p.amount_paise),0) v FROM gym_visits v
                           JOIN payments p ON p.member_id = v.member_id AND p.gym_id = v.gym_id AND p.status='recorded' AND p.paid_at >= v.created_at
                           WHERE v.created_at >= :s AND v.member_id IS NOT NULL GROUP BY v.gym_id""", s=s)
    ex_gym: dict[str, dict] = defaultdict(lambda: {"views": 0, "booked": 0, "visited": 0, "joined": 0, "waiting": 0, "lost": 0,
                                                   "earned_paise": 0, "_mins": []})
    for r in ev:
        if r["kind"] == "view" and r["gym_id"]:
            ex_gym[r["gym_id"]]["views"] += int(r["n"])
    for r in vis:
        e, n = ex_gym[r["gym_id"]], int(r["n"])
        e["booked"] += n
        if r["status"] in ("visited", "joined"):
            e["visited"] += n
        if r["status"] == "joined":
            e["joined"] += n
        if r["status"] == "requested":
            e["waiting"] += n
        if r["status"] in ("cancelled", "no_show"):
            e["lost"] += n
        if r["mins"] is not None:
            e["_mins"].append(float(r["mins"]))
    for gid, r in earned.items():
        ex_gym[gid]["earned_paise"] = int(r["v"])
    esum = lambda k: sum(e[k] for e in ex_gym.values())
    all_mins = [m for e in ex_gym.values() for m in e["_mins"]]
    searches = sum(int(r["n"]) for r in ev if r["kind"] == "search")
    explore = {"searches": searches, "views": esum("views"), "booked": esum("booked"), "visited": esum("visited"), "joined": esum("joined"),
               "waiting": esum("waiting"), "lost": esum("lost"), "earned_paise": esum("earned_paise"),
               "visit_to_join_pct": pct(esum("joined"), esum("booked")),
               "avg_response_min": round(statistics.mean(all_mins)) if all_mins else None,
               "gyms_listed": sum(1 for g in db.scalars(select(Gym).where(Gym.status == "active", Gym.is_test.is_(False))) if listed(g)),
               "gyms": sorted([{"gym_id": gid, "name": names[gid]["name"], "city": names[gid]["city"],
                                **{k: v for k, v in e.items() if k != "_mins"},
                                "avg_response_min": round(statistics.mean(e["_mins"])) if e["_mins"] else None}
                               for gid, e in ex_gym.items() if gid in names], key=lambda r: -(r["earned_paise"] + r["booked"] * 100 + r["views"]))}
    return {"range": rng, "whatsapp": whatsapp, "explore": explore,
            "total_paise": whatsapp["after_reminder_paise"] + explore["earned_paise"],
            "method": "WhatsApp: memberships sold within 14 days of a renewal reminder that was sent (‘link’ numbers are the "
                      "subset completed through the personal renewal link). Explore: payments from people who booked a visit "
                      "and were then added as a member."}


@router.get("/impact", summary="What WhatsApp reminders and Explore earned gyms in the period")
def impact(rng: Range = Query("30d", alias="range"), refresh: bool = False, admin: User = Depends(platform_admin),
           db: Session = Depends(get_db)):
    return cached(f"ins:impact:{rng}", refresh, lambda: build_impact(db, rng))


# =====================================================================================================
# 7. Demand map
# =====================================================================================================
def build_demand(db: Session, rng: str) -> dict:
    td = today_ist()
    s = utc_start(td - timedelta(days=RANGES[rng] - 1))
    gyms = [{"id": g.id, "name": g.name, "area": g.area, "city": g.city, "lat": float(g.lat), "lng": float(g.lng), "listed": listed(g)}
            for g in db.scalars(select(Gym).where(Gym.status == "active", Gym.is_test.is_(False), Gym.lat.is_not(None), Gym.lng.is_not(None)))]
    supply = [g for g in gyms if g["listed"]]
    raw = rows(db, """SELECT lat, lng, COUNT(*) searches, COUNT(DISTINCT user_id) people, SUM(results = 0) none_found,
                      AVG(nearest_km) nearest, MAX(created_at) last_at FROM explore_events
                      WHERE kind='search' AND lat IS NOT NULL AND created_at >= :s GROUP BY lat, lng""", s=s)
    cells = []
    for r in raw:
        lat, lng = float(r["lat"]), float(r["lng"])
        near = min(supply, key=lambda g: haversine_km(lat, lng, g["lat"], g["lng"]), default=None)
        km = round(haversine_km(lat, lng, near["lat"], near["lng"]), 1) if near else None
        within = sum(1 for g in supply if haversine_km(lat, lng, g["lat"], g["lng"]) <= 3)
        n = int(r["searches"])
        cells.append({"lat": lat, "lng": lng, "searches": n, "people": int(r["people"] or 0), "empty": int(r["none_found"] or 0),
                      "gyms_3km": within, "nearest_km": km,
                      "nearest": {"name": near["name"], "area": near["area"], "city": near["city"]} if near else None,
                      "gap": "none" if within == 0 else "thin" if n / within >= 15 else "served",
                      "last_at": ts(r["last_at"])})
    cells.sort(key=lambda c: (c["gap"] != "none", -c["searches"]))
    terms = rows(db, """SELECT LOWER(TRIM(q)) term, COUNT(*) n, SUM(results = 0) none_found FROM explore_events
                        WHERE kind='search' AND q IS NOT NULL AND created_at >= :s GROUP BY term ORDER BY n DESC LIMIT 25""", s=s)
    views = rows(db, """SELECT e.gym_id, g.name, g.area, g.city, COUNT(*) n FROM explore_events e JOIN gyms g ON g.id=e.gym_id
                        WHERE e.kind='view' AND e.created_at >= :s GROUP BY e.gym_id, g.name, g.area, g.city ORDER BY n DESC LIMIT 15""", s=s)
    first = db.execute(text("SELECT MIN(created_at) FROM explore_events")).scalar()
    total = sum(c["searches"] for c in cells)
    unserved = [c for c in cells if c["gap"] == "none"]
    return {"range": rng, "cells": cells, "gyms": gyms,
            "terms": [{"term": t["term"], "searches": int(t["n"]), "empty": int(t["none_found"] or 0)} for t in terms],
            "top_viewed": [{"gym_id": v["gym_id"], "name": v["name"], "area": v["area"], "city": v["city"], "views": int(v["n"])} for v in views],
            "totals": {"searches": total, "areas": len(cells), "unserved_areas": len(unserved),
                       "unserved_searches": sum(c["searches"] for c in unserved),
                       "unserved_pct": pct(sum(c["searches"] for c in unserved), total, 0), "listed_gyms": len(supply)},
            "collecting_since": ts(first),
            "method": "Each dot is a ~1 km square where people opened Explore with their location on. ‘No gym nearby’ means no "
                      "listed gym within 3 km. Locations are rounded before they are stored."}


@router.get("/demand", summary="Where people search in Explore versus where listed gyms are (areas to sign up next)")
def demand(rng: Range = Query("30d", alias="range"), refresh: bool = False, admin: User = Depends(platform_admin),
           db: Session = Depends(get_db)):
    return cached(f"ins:demand:{rng}", refresh, lambda: build_demand(db, rng))


# =====================================================================================================
# Headline strip for the overview page
# =====================================================================================================
@router.get("/headline", summary="Three numbers for the top of the overview: gyms to call, expected renewals, money lapsing")
def headline(admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    a = cached("ins:actions", False, lambda: build_actions(db))
    f = cached("ins:fc:30", False, lambda: build_forecast(db, 30))
    t = f["totals"]
    return {"to_call": a["summary"]["gyms"], "urgent": a["summary"]["urgent"], "top": [
                {"gym_id": r["gym_id"], "name": r["name"], "why": r["reasons"][0]["title"]} for r in a["items"][:3]],
            "due_paise": t["due_paise"], "expected_paise": t["expected_paise"], "at_risk_paise": t["at_risk_paise"],
            "winnable_paise": t["lapsed_recent_paise"], "winnable": t["lapsed_recent"]}
