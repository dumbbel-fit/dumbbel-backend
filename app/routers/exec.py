"""DUMBBEL.fit executive console (platform admins only): platform KPIs, growth, revenue, funnel, per-gym
performance + health, gym deep-dive, cities, users, platform activity, system status, AI summary, CSV export.
All money in paise. Days are IST (Asia/Kolkata)."""
import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import platform_admin
from app.errors import ApiError
from app.models import User
from app.responses import DocRoute, ERRORS
from app.services import ai, storage
from app.services.cache import cache
from app.utils import clamp_limit, decode_cursor, encode_cursor, iso, utcnow

router = APIRouter(prefix="/internal", tags=["Executive console"], route_class=DocRoute, responses=ERRORS)
IST = timezone(timedelta(hours=5, minutes=30))
RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
Range = Literal["7d", "30d", "90d", "365d"]
PAY_DAY = "DATE(DATE_ADD(p.paid_at, INTERVAL 330 MINUTE))"


def _window(rng: str) -> dict:
    n = RANGES[rng]
    today = datetime.now(IST).date()
    start = today - timedelta(days=n - 1)
    pstart = start - timedelta(days=n)
    to_utc = lambda d: datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
    return {"n": n, "today": today, "start": start, "pstart": pstart, "start_utc": to_utc(start),
            "pstart_utc": to_utc(pstart), "end_utc": to_utc(today + timedelta(days=1)),
            "today_utc": to_utc(today)}


def q1(db: Session, sql: str, **p):
    return db.execute(text(sql), p).scalar() or 0


def rows(db: Session, sql: str, **p):
    return [dict(r._mapping) for r in db.execute(text(sql), p)]


def delta(cur: float, prev: float) -> float | None:
    return None if not prev else round((cur - prev) / prev * 100, 1)


DUES_SQL = """
SELECT COALESCE(SUM(GREATEST(0, m.price_paise - COALESCE(pp.paid,0) - COALESCE(ca.applied,0))),0)
FROM memberships m
LEFT JOIN (SELECT membership_id, SUM(amount_paise) paid FROM payments WHERE status='recorded' AND membership_id IS NOT NULL
           GROUP BY membership_id) pp ON pp.membership_id = m.id
LEFT JOIN (SELECT membership_id, SUM(amount_paise) applied FROM credit_applications WHERE reversed_at IS NULL
           GROUP BY membership_id) ca ON ca.membership_id = m.id
WHERE m.state <> 'cancelled' {extra}"""


@router.get("/overview", summary="Platform KPIs with period-over-period change, growth series and funnel")
def overview(rng: Range = Query("30d", alias="range"), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    key = f"exec:ov:{rng}"
    hit = cache.get(key)
    if hit:
        return json.loads(hit)
    w = _window(rng)
    P = {"s": w["start_utc"], "e": w["end_utc"], "ps": w["pstart_utc"], "sd": w["start"], "psd": w["pstart"],
         "td": w["today"], "tu": w["today_utc"], "m30": utcnow().replace(tzinfo=None) - timedelta(days=30),
         "d1": utcnow().replace(tzinfo=None) - timedelta(days=1), "d7": w["today"] + timedelta(days=7)}

    gyms_by_status = {r["status"]: r["n"] for r in rows(db, "SELECT status, COUNT(*) n FROM gyms GROUP BY status")}
    gyms = {"total": sum(gyms_by_status.values()), "by_status": gyms_by_status,
            "new": q1(db, "SELECT COUNT(*) FROM gyms WHERE created_at >= :s", **P),
            "new_prev": q1(db, "SELECT COUNT(*) FROM gyms WHERE created_at >= :ps AND created_at < :s", **P),
            "businesses": q1(db, "SELECT COUNT(DISTINCT COALESCE(org_id, id)) FROM gyms"),
            "cities": q1(db, "SELECT COUNT(DISTINCT LOWER(TRIM(city))) FROM gyms WHERE city IS NOT NULL AND city <> ''"),
            "pending_over_48h": q1(db, "SELECT COUNT(*) FROM gyms WHERE status='pending' AND created_at < :d", d=P["d1"] - timedelta(days=1))}

    total_m = q1(db, "SELECT COUNT(*) FROM gym_members WHERE status='active'")
    on_app = q1(db, "SELECT COUNT(*) FROM gym_members gm JOIN users u ON u.id=gm.user_id WHERE gm.status='active' AND u.first_login_at IS NOT NULL")
    active_m = q1(db, "SELECT COUNT(DISTINCT member_id) FROM memberships WHERE state<>'cancelled' AND start_date<=:td AND end_date>=:td", **P)
    members = {"total": total_m, "active": active_m, "on_app": on_app,
               "app_adoption_pct": round(on_app / total_m * 100, 1) if total_m else 0,
               "new": q1(db, "SELECT COUNT(*) FROM gym_members WHERE created_at >= :s AND source <> 'import'", **P),
               "new_prev": q1(db, "SELECT COUNT(*) FROM gym_members WHERE created_at >= :ps AND created_at < :s AND source <> 'import'", **P),
               "imported": q1(db, "SELECT COUNT(*) FROM gym_members WHERE created_at >= :s AND source = 'import'", **P)}
    users = {"total": q1(db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL"),
             "mau": q1(db, "SELECT COUNT(*) FROM users WHERE last_login_at >= :m30", **P),
             "dau": q1(db, "SELECT COUNT(*) FROM users WHERE last_login_at >= :d1", **P),
             "new": q1(db, "SELECT COUNT(*) FROM users WHERE first_login_at >= :s", **P),
             "staff": q1(db, "SELECT COUNT(*) FROM gym_staff WHERE status='active'")}

    ci_now = q1(db, "SELECT COUNT(*) FROM checkins WHERE result='success' AND local_date >= :sd", **P)
    ci_prev = q1(db, "SELECT COUNT(*) FROM checkins WHERE result='success' AND local_date >= :psd AND local_date < :sd", **P)
    by_method = {r["method"]: r["n"] for r in rows(db, "SELECT method, COUNT(*) n FROM checkins WHERE result='success' AND local_date >= :sd GROUP BY method", **P)}
    denied = q1(db, "SELECT COUNT(*) FROM checkins WHERE result<>'success' AND local_date >= :sd", **P)
    checkins = {"total": ci_now, "prev": ci_prev, "change_pct": delta(ci_now, ci_prev),
                "today": q1(db, "SELECT COUNT(*) FROM checkins WHERE result='success' AND local_date = :td", **P),
                "unique_members": q1(db, "SELECT COUNT(DISTINCT member_id) FROM checkins WHERE result='success' AND local_date >= :sd", **P),
                "per_day": round(ci_now / w["n"], 1), "by_method": by_method, "denied": denied,
                "self_share_pct": round(by_method.get("self", 0) / ci_now * 100, 1) if ci_now else 0}

    g_now = q1(db, "SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :s", **P)
    g_prev = q1(db, "SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :ps AND paid_at < :s", **P)
    n_pay = q1(db, "SELECT COUNT(*) FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :s", **P)
    money = {"gmv_paise": int(g_now), "gmv_prev_paise": int(g_prev), "change_pct": delta(g_now, g_prev),
             "payments": n_pay, "avg_ticket_paise": int(g_now / n_pay) if n_pay else 0,
             "today_paise": int(q1(db, "SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :tu", **P)),
             "by_method": {r["method"]: int(r["v"]) for r in rows(db, "SELECT method, SUM(amount_paise) v FROM payments WHERE status='recorded' AND source<>'import' AND paid_at >= :s GROUP BY method", **P)},
             "by_source": {r["source"]: int(r["v"]) for r in rows(db, "SELECT source, SUM(amount_paise) v FROM payments WHERE status='recorded' AND paid_at >= :s GROUP BY source", **P)},
             "imported_history_paise": int(q1(db, "SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status='recorded' AND source='import'")),
             "dues_outstanding_paise": int(q1(db, DUES_SQL.format(extra=""))),
             "voided": q1(db, "SELECT COUNT(*) FROM payments WHERE status='voided' AND voided_at >= :s", **P)}

    ended = q1(db, "SELECT COUNT(*) FROM memberships WHERE state<>'cancelled' AND end_date >= :sd AND end_date < :td", **P)
    renewed = q1(db, """SELECT COUNT(*) FROM memberships m WHERE m.state<>'cancelled' AND m.end_date >= :sd AND m.end_date < :td
                        AND EXISTS (SELECT 1 FROM memberships n WHERE n.member_id=m.member_id AND n.state<>'cancelled'
                                    AND n.start_date > m.start_date)""", **P)
    memberships = {"sold": q1(db, "SELECT COUNT(*) FROM memberships WHERE created_at >= :s AND source='manual'", **P),
                   "sold_prev": q1(db, "SELECT COUNT(*) FROM memberships WHERE created_at >= :ps AND created_at < :s AND source='manual'", **P),
                   "ended": ended, "renewed": renewed, "renewal_rate_pct": round(renewed / ended * 100, 1) if ended else None,
                   "churned": ended - renewed,
                   "expiring_7d": q1(db, "SELECT COUNT(*) FROM memberships WHERE state<>'cancelled' AND end_date >= :td AND end_date <= :d7", **P),
                   "frozen": q1(db, "SELECT COUNT(*) FROM memberships WHERE state='frozen'"),
                   "suspended": q1(db, "SELECT COUNT(*) FROM memberships WHERE suspended_at IS NOT NULL AND state<>'cancelled' AND end_date >= :td", **P)}

    upi_st = {r["status"]: r["n"] for r in rows(db, "SELECT status, COUNT(*) n FROM upi_payment_requests WHERE created_at >= :s GROUP BY status", **P)}
    mins = [r["m"] for r in rows(db, "SELECT TIMESTAMPDIFF(MINUTE, claimed_at, decided_at) m FROM upi_payment_requests WHERE status='verified' AND claimed_at IS NOT NULL AND decided_at >= :s", **P) if r["m"] is not None]
    mins.sort()
    upi = {"by_status": upi_st, "verified_paise": int(q1(db, "SELECT COALESCE(SUM(amount_paise),0) FROM upi_payment_requests WHERE status='verified' AND decided_at >= :s", **P)),
           "median_verify_min": mins[len(mins) // 2] if mins else None, "waiting_now": q1(db, "SELECT COUNT(*) FROM upi_payment_requests WHERE status='claimed'"),
           "gyms_with_upi": q1(db, "SELECT COUNT(DISTINCT gym_id) FROM gym_upi_accounts WHERE active=1"),
           "business_upi_ids": q1(db, "SELECT COUNT(*) FROM gym_upi_accounts WHERE active=1 AND account_type='merchant'")}
    engagement = {"food_logs": q1(db, "SELECT COUNT(*) FROM food_logs WHERE created_at >= :s AND meal<>'water'", **P),
                  "food_loggers": q1(db, "SELECT COUNT(DISTINCT user_id) FROM food_logs WHERE created_at >= :s", **P),
                  "fitness_profiles": q1(db, "SELECT COUNT(*) FROM users WHERE fitness_profile IS NOT NULL"),
                  "badges_earned": q1(db, "SELECT COALESCE(SUM(JSON_LENGTH(badges)),0) FROM gym_members WHERE badges IS NOT NULL"),
                  "on_leaderboard_pct": round(q1(db, "SELECT COUNT(*) FROM users WHERE show_on_leaderboard=1 AND first_login_at IS NOT NULL") /
                                              max(1, q1(db, "SELECT COUNT(*) FROM users WHERE first_login_at IS NOT NULL")) * 100, 1)}

    # daily series (IST)
    days = [w["start"] + timedelta(days=i) for i in range(w["n"])]
    ci_d = {r["d"]: r["n"] for r in rows(db, "SELECT local_date d, COUNT(*) n FROM checkins WHERE result='success' AND local_date >= :sd GROUP BY local_date", **P)}
    gmv_d = {r["d"]: int(r["v"]) for r in rows(db, f"SELECT {PAY_DAY} d, SUM(amount_paise) v FROM payments p WHERE status='recorded' AND source<>'import' AND paid_at >= :s GROUP BY d", **P)}
    mem_d = {r["d"]: r["n"] for r in rows(db, "SELECT DATE(DATE_ADD(created_at, INTERVAL 330 MINUTE)) d, COUNT(*) n FROM gym_members WHERE created_at >= :s GROUP BY d", **P)}
    gym_d = {r["d"]: r["n"] for r in rows(db, "SELECT DATE(DATE_ADD(created_at, INTERVAL 330 MINUTE)) d, COUNT(*) n FROM gyms WHERE created_at >= :s GROUP BY d", **P)}
    series = [{"date": d.isoformat(), "checkins": ci_d.get(d, 0), "gmv_paise": gmv_d.get(d, 0), "new_members": mem_d.get(d, 0),
               "new_gyms": gym_d.get(d, 0)} for d in days]
    hours = rows(db, "SELECT HOUR(DATE_ADD(at, INTERVAL 330 MINUTE)) h, COUNT(*) n FROM checkins WHERE result='success' AND local_date >= :sd GROUP BY h", **P)
    by_hour = [0] * 24
    for r in hours:
        by_hour[int(r["h"])] = r["n"]
    funnel = {"gyms_registered": gyms["total"],
              "gyms_with_documents": q1(db, "SELECT COUNT(DISTINCT gym_id) FROM gym_documents"),
              "gyms_approved": gyms_by_status.get("active", 0),
              "gyms_with_members": q1(db, "SELECT COUNT(DISTINCT gym_id) FROM gym_members"),
              "gyms_with_checkins_period": q1(db, "SELECT COUNT(DISTINCT gym_id) FROM checkins WHERE result='success' AND local_date >= :sd", **P),
              "members_total": total_m, "members_on_app": on_app,
              "members_checked_in_period": checkins["unique_members"]}
    out = {"range": rng, "from": w["start"].isoformat(), "to": w["today"].isoformat(), "gyms": gyms, "members": members,
           "users": users, "checkins": checkins, "money": money, "memberships": memberships, "upi": upi,
           "engagement": engagement, "series": series, "by_hour": by_hour, "funnel": funnel,
           "generated_at": utcnow().isoformat()}
    cache.set(key, json.dumps(out, default=str), ex=120)
    return out


GYM_SQL = """
SELECT g.id, g.name, g.code_prefix, g.city, g.area, g.status, g.created_at, g.approved_at, COALESCE(g.org_id, g.id) org,
  u.name owner_name, u.phone owner_phone,
  (SELECT COUNT(*) FROM gym_members gm WHERE gm.gym_id=g.id AND gm.status='active') members,
  (SELECT COUNT(*) FROM gym_members gm JOIN users uu ON uu.id=gm.user_id WHERE gm.gym_id=g.id AND gm.status='active' AND uu.first_login_at IS NOT NULL) on_app,
  (SELECT COUNT(DISTINCT m.member_id) FROM memberships m WHERE m.gym_id=g.id AND m.state<>'cancelled' AND m.start_date<=:td AND m.end_date>=:td) active_members,
  (SELECT COUNT(*) FROM checkins c WHERE c.gym_id=g.id AND c.result='success' AND c.local_date>=:sd) checkins,
  (SELECT COUNT(*) FROM checkins c WHERE c.gym_id=g.id AND c.result='success' AND c.local_date>=:psd AND c.local_date<:sd) checkins_prev,
  (SELECT COALESCE(SUM(p.amount_paise),0) FROM payments p WHERE p.gym_id=g.id AND p.status='recorded' AND p.source<>'import' AND p.paid_at>=:s) gmv,
  (SELECT COALESCE(SUM(p.amount_paise),0) FROM payments p WHERE p.gym_id=g.id AND p.status='recorded' AND p.source<>'import' AND p.paid_at>=:ps AND p.paid_at<:s) gmv_prev,
  (SELECT MAX(c.at) FROM checkins c WHERE c.gym_id=g.id AND c.result='success') last_checkin_at,
  (SELECT COUNT(*) FROM gym_staff s WHERE s.gym_id=g.id AND s.status<>'deactivated') staff,
  (SELECT COUNT(*) FROM gym_upi_accounts a WHERE a.gym_id=g.id AND a.active=1) upi_ids,
  (SELECT COUNT(*) FROM plans pl WHERE pl.gym_id=g.id AND pl.active=1) plans
FROM gyms g LEFT JOIN users u ON u.id = g.owner_user_id"""


def _health(r: dict, w: dict) -> tuple[int, list[str]]:
    flags, score = [], 100
    now = utcnow().replace(tzinfo=None)
    if r["status"] == "pending":
        age_h = (now - r["created_at"]).total_seconds() / 3600
        if age_h > 48:
            flags.append(f"Pending approval {int(age_h // 24)} days")
        return 50, flags
    if r["status"] in ("suspended", "rejected"):
        return 0, [r["status"].title()]
    if not r["members"]:
        flags.append("No members yet")
        score -= 40
    last = r["last_checkin_at"]
    if not last or (now - last).days >= 7:
        flags.append("No check-ins in 7+ days")
        score -= 30
    adopt = r["on_app"] / r["members"] * 100 if r["members"] else 0
    if r["members"] and adopt < 20:
        flags.append(f"Low app adoption ({adopt:.0f}%)")
        score -= 15
    if r["active_members"] and r["checkins"] / max(1, r["active_members"]) / max(1, w["n"] / 7) < 1:
        flags.append("Members visit < 1×/week")
        score -= 10
    if r["checkins_prev"] and r["checkins"] < r["checkins_prev"] * 0.7:
        flags.append("Check-ins down 30%+")
        score -= 15
    if not r["upi_ids"]:
        flags.append("No UPI ID")
        score -= 5
    if not r["plans"]:
        flags.append("No active plans")
        score -= 10
    return max(0, min(100, score)), flags


def gym_rows(db: Session, rng: str, status: str | None = None, q: str | None = None) -> list[dict]:
    w = _window(rng)
    P = {"s": w["start_utc"], "ps": w["pstart_utc"], "sd": w["start"], "psd": w["pstart"], "td": w["today"]}
    sql, extra = GYM_SQL, []
    if status:
        extra.append("g.status = :status")
        P["status"] = status
    if q:
        extra.append("(g.name LIKE :q OR g.city LIKE :q OR g.area LIKE :q OR g.code_prefix LIKE :q OR u.phone LIKE :q)")
        P["q"] = f"%{q.strip()}%"
    if extra:
        sql += " WHERE " + " AND ".join(extra)
    out = []
    for r in rows(db, sql, **P):
        score, flags = _health(r, w)
        r.update(health=score, flags=flags,
                 app_adoption_pct=round(r["on_app"] / r["members"] * 100, 1) if r["members"] else 0,
                 checkins_change_pct=delta(r["checkins"], r["checkins_prev"]), gmv_change_pct=delta(r["gmv"], r["gmv_prev"]),
                 visits_per_active_week=round(r["checkins"] / max(1, r["active_members"]) / max(1, w["n"] / 7), 2) if r["active_members"] else 0,
                 created_at=iso(r["created_at"].replace(tzinfo=timezone.utc)) if r["created_at"] else None,
                 approved_at=iso(r["approved_at"].replace(tzinfo=timezone.utc)) if r["approved_at"] else None,
                 last_checkin_at=iso(r["last_checkin_at"].replace(tzinfo=timezone.utc)) if r["last_checkin_at"] else None,
                 gmv=int(r["gmv"]), gmv_prev=int(r["gmv_prev"]))
        out.append(r)
    return out


SORTS = {"gmv": lambda r: -r["gmv"], "checkins": lambda r: -r["checkins"], "members": lambda r: -r["members"],
         "health": lambda r: r["health"], "adoption": lambda r: -r["app_adoption_pct"], "newest": lambda r: r["created_at"] or "",
         "name": lambda r: (r["name"] or "").lower()}


@router.get("/analytics/gyms", summary="Every gym's performance: members, check-ins, GMV, adoption, health score + flags")
def analytics_gyms(rng: Range = Query("30d", alias="range"), sort: Literal["gmv", "checkins", "members", "health", "adoption", "newest", "name"] = "gmv",
                   status: Literal["pending", "active", "rejected", "suspended"] | None = None, q: str | None = None,
                   limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                   admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    data = gym_rows(db, rng, status, q)
    data.sort(key=SORTS[sort], reverse=(sort == "newest"))
    off = int(decode_cursor(cursor).get("o", 0))
    lim = clamp_limit(limit)
    page = data[off:off + lim]
    tot = {"gyms": len(data), "members": sum(r["members"] for r in data), "gmv": sum(r["gmv"] for r in data),
           "checkins": sum(r["checkins"] for r in data), "at_risk": sum(1 for r in data if r["status"] == "active" and r["health"] < 60)}
    return {"items": page, "next_cursor": encode_cursor({"o": off + lim}) if off + lim < len(data) else None, "totals": tot}


@router.get("/analytics/gyms.csv", summary="Export the gym performance table as CSV", responses={200: {"content": {"text/csv": {}}}})
def analytics_gyms_csv(rng: Range = Query("30d", alias="range"), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    data = sorted(gym_rows(db, rng), key=SORTS["gmv"])
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["Gym", "Code", "City", "Area", "Status", "Owner", "Owner phone", "Members", "Active members", "On app %",
                 f"Check-ins ({rng})", "Check-ins change %", f"GMV ₹ ({rng})", "GMV change %", "Visits/active member/week",
                 "Staff", "UPI IDs", "Health", "Flags", "Last check-in", "Registered"])
    for r in data:
        wr.writerow([r["name"], r["code_prefix"], r["city"], r["area"], r["status"], r["owner_name"], r["owner_phone"], r["members"],
                     r["active_members"], r["app_adoption_pct"], r["checkins"], r["checkins_change_pct"], f"{r['gmv'] / 100:.2f}",
                     r["gmv_change_pct"], r["visits_per_active_week"], r["staff"], r["upi_ids"], r["health"], "; ".join(r["flags"]),
                     r["last_checkin_at"] or "", r["created_at"] or ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="dumbbel-gyms-{rng}-{date.today().isoformat()}.csv"'})


@router.get("/analytics/gyms/{gymId}", summary="Gym deep-dive: KPIs, daily series, plans mix, busiest hours, staff, recent activity")
def analytics_gym(gymId: str, rng: Range = Query("30d", alias="range"), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    base = [r for r in gym_rows(db, rng) if r["id"] == gymId]
    if not base:
        raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
    w = _window(rng)
    P = {"g": gymId, "s": w["start_utc"], "sd": w["start"], "td": w["today"]}
    days = [w["start"] + timedelta(days=i) for i in range(w["n"])]
    ci_d = {r["d"]: r["n"] for r in rows(db, "SELECT local_date d, COUNT(*) n FROM checkins WHERE gym_id=:g AND result='success' AND local_date >= :sd GROUP BY local_date", **P)}
    gmv_d = {r["d"]: int(r["v"]) for r in rows(db, f"SELECT {PAY_DAY} d, SUM(amount_paise) v FROM payments p WHERE gym_id=:g AND status='recorded' AND source<>'import' AND paid_at >= :s GROUP BY d", **P)}
    by_hour = [0] * 24
    for r in rows(db, "SELECT HOUR(DATE_ADD(at, INTERVAL 330 MINUTE)) h, COUNT(*) n FROM checkins WHERE gym_id=:g AND result='success' AND local_date >= :sd GROUP BY h", **P):
        by_hour[int(r["h"])] = r["n"]
    plans = rows(db, """SELECT m.plan_name name, COUNT(*) sold, SUM(m.price_paise) value FROM memberships m
                        WHERE m.gym_id=:g AND m.created_at >= :s AND m.state<>'cancelled' GROUP BY m.plan_name ORDER BY sold DESC LIMIT 8""", **P)
    status_mix = rows(db, """SELECT CASE WHEN suspended_at IS NOT NULL AND end_date >= :td THEN 'suspended' WHEN state='frozen' THEN 'frozen'
                             WHEN start_date > :td THEN 'upcoming' WHEN end_date < :td THEN 'expired' ELSE 'active' END st, COUNT(*) n
                             FROM memberships WHERE gym_id=:g AND state<>'cancelled' GROUP BY st""", **P)
    staff = rows(db, """SELECT u.name, u.phone, s.role, s.staff_role, s.status, u.last_login_at FROM gym_staff s JOIN users u ON u.id=s.user_id
                        WHERE s.gym_id=:g ORDER BY s.role, u.name""", **P)
    for s in staff:
        s["last_login_at"] = iso(s["last_login_at"].replace(tzinfo=timezone.utc)) if s["last_login_at"] else None
    recent = rows(db, "SELECT at, action, summary FROM audit_log WHERE gym_id=:g ORDER BY id DESC LIMIT 15", **P)
    for r in recent:
        r["at"] = iso(r["at"].replace(tzinfo=timezone.utc))
    return {"gym": base[0], "range": rng, "dues_outstanding_paise": int(q1(db, DUES_SQL.format(extra="AND m.gym_id = :g"), g=gymId)),
            "series": [{"date": d.isoformat(), "checkins": ci_d.get(d, 0), "gmv_paise": gmv_d.get(d, 0)} for d in days],
            "by_hour": by_hour, "plans": [{**p, "value": int(p["value"] or 0)} for p in plans],
            "membership_status": {r["st"]: r["n"] for r in status_mix}, "staff": staff, "recent_activity": recent,
            "documents": [{"id": d["id"], "type": d["type"], "status": d["status"], "url": storage.signed_get_url(d["file_key"], 300)}
                          for d in rows(db, "SELECT id, type, status, file_key FROM gym_documents WHERE gym_id=:g", **P)]}


@router.get("/analytics/cities", summary="Gyms, members, check-ins and GMV by city")
def analytics_cities(rng: Range = Query("30d", alias="range"), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    agg: dict[str, dict] = {}
    for r in gym_rows(db, rng):
        c = (r["city"] or "Unknown").strip().title()
        a = agg.setdefault(c, {"city": c, "gyms": 0, "active_gyms": 0, "members": 0, "on_app": 0, "checkins": 0, "gmv": 0})
        a["gyms"] += 1
        a["active_gyms"] += r["status"] == "active"
        for k in ("members", "on_app", "checkins", "gmv"):
            a[k] += r[k]
    out = sorted(agg.values(), key=lambda x: -x["gmv"])
    for a in out:
        a["app_adoption_pct"] = round(a["on_app"] / a["members"] * 100, 1) if a["members"] else 0
    return {"items": out}


@router.get("/attention", summary="What needs the team's attention now: approvals queue, at-risk gyms, UPI backlog")
def attention(admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    data = gym_rows(db, "30d")
    pending = sorted([r for r in data if r["status"] == "pending"], key=lambda r: r["created_at"] or "")
    risk = sorted([r for r in data if r["status"] == "active" and r["health"] < 60], key=lambda r: r["health"])
    upi = rows(db, """SELECT g.id, g.name, COUNT(*) n, MIN(r.claimed_at) oldest FROM upi_payment_requests r JOIN gyms g ON g.id=r.gym_id
                      WHERE r.status='claimed' GROUP BY g.id, g.name ORDER BY n DESC LIMIT 10""")
    for r in upi:
        r["oldest"] = iso(r["oldest"].replace(tzinfo=timezone.utc)) if r["oldest"] else None
    disputes = rows(db, """SELECT gm.id member_id, gm.display_name, g.name gym, gm.claim_disputed_at at FROM gym_members gm JOIN gyms g ON g.id=gm.gym_id
                           WHERE gm.claim_disputed_at IS NOT NULL ORDER BY gm.claim_disputed_at DESC LIMIT 10""")
    for r in disputes:
        r["at"] = iso(r["at"].replace(tzinfo=timezone.utc))
    keep = ("id", "name", "city", "status", "created_at", "health", "flags", "members", "checkins")
    return {"pending_approvals": [{k: r[k] for k in keep} for r in pending],
            "at_risk": [{k: r[k] for k in keep} for r in risk[:15]], "upi_backlog": upi, "claim_disputes": disputes}


@router.get("/users", summary="Find any user by name or phone: their gyms (staff) and memberships")
def users_search(q: str = Query(..., min_length=3), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    digits = "".join(ch for ch in q if ch.isdigit())
    like = f"%{q.strip()}%"
    found = rows(db, """SELECT id, name, phone, email, is_platform_admin, first_login_at, last_login_at, created_at, deletion_requested_at
                        FROM users WHERE deleted_at IS NULL AND (name LIKE :q OR email LIKE :q OR phone LIKE :d) ORDER BY last_login_at DESC LIMIT 25""",
                 q=like, d=f"%{digits[-10:]}%" if len(digits) >= 4 else "__none__")
    for u in found:
        u["staff_of"] = rows(db, """SELECT g.id gym_id, g.name gym, s.role, s.staff_role, s.status FROM gym_staff s JOIN gyms g ON g.id=s.gym_id
                                    WHERE s.user_id=:u""", u=u["id"])
        u["memberships"] = rows(db, """SELECT gm.id member_id, g.id gym_id, g.name gym, gm.member_code, gm.status, gm.last_checkin_at
                                       FROM gym_members gm JOIN gyms g ON g.id=gm.gym_id WHERE gm.user_id=:u""", u=u["id"])
        for k in ("first_login_at", "last_login_at", "created_at", "deletion_requested_at"):
            u[k] = iso(u[k].replace(tzinfo=timezone.utc)) if u[k] else None
        for m in u["memberships"]:
            m["last_checkin_at"] = iso(m["last_checkin_at"].replace(tzinfo=timezone.utc)) if m["last_checkin_at"] else None
        u["is_platform_admin"] = bool(u["is_platform_admin"])
    return {"items": found}


@router.get("/activity", summary="Platform-wide activity (every audited write, all gyms)")
def platform_activity(gym_id: str | None = None, action: str | None = None, limit: int = Query(50, ge=1, le=100),
                      cursor: str | None = None, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    where, P = ["1=1"], {}
    if gym_id:
        where.append("a.gym_id = :g")
        P["g"] = gym_id
    if action:
        where.append("a.action LIKE :a")
        P["a"] = f"{action}%"
    c = decode_cursor(cursor)
    if c:
        where.append("a.id < :cid")
        P["cid"] = int(c["id"])
    lim = clamp_limit(limit)
    rs = rows(db, f"""SELECT a.id, a.at, a.action, a.summary, a.entity_type, a.gym_id, g.name gym, u.name actor
                      FROM audit_log a LEFT JOIN gyms g ON g.id=a.gym_id LEFT JOIN users u ON u.id=a.actor_user_id
                      WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT {lim + 1}""", **P)
    nxt = encode_cursor({"id": rs[lim - 1]["id"]}) if len(rs) > lim else None
    for r in rs:
        r["at"] = iso(r["at"].replace(tzinfo=timezone.utc))
    return {"items": rs[:lim], "next_cursor": nxt}


@router.get("/system", summary="System status: database, integrations configured, queues, data volumes")
def system(admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        dbok = True
    except Exception:
        dbok = False
    counts = {t: q1(db, f"SELECT COUNT(*) FROM {t}") for t in ("users", "gyms", "gym_members", "memberships", "payments",
                                                              "checkins", "audit_log", "food_logs", "upi_payment_requests")}
    return {"database": {"ok": dbok, "tables": counts}, "environment": settings.env,
            "integrations": {"auth_provider": settings.auth_provider, "firebase": bool(settings.firebase_project_id),
                             "twofactor_otp": bool(settings.twofactor_api_key), "sms_gym_status_template": bool(settings.twofactor_tpl_gym_status),
                             "ai_groq": ai.enabled(), "object_storage": "s3" if storage.use_s3() else "local (ephemeral on Cloud Run!)",
                             "redis": bool(settings.redis_url), "expo_push": True, "cors_origins": settings.cors_origins or "(dumbbel.fit + localhost)"},
            "queues": {"upi_waiting_verification": q1(db, "SELECT COUNT(*) FROM upi_payment_requests WHERE status='claimed'"),
                       "imports_running": q1(db, "SELECT COUNT(*) FROM imports WHERE status IN ('parsing','committing')"),
                       "deletion_requests": q1(db, "SELECT COUNT(*) FROM users WHERE deletion_requested_at IS NOT NULL AND deleted_at IS NULL")},
            "checked_at": utcnow().isoformat()}


@router.get("/ai/summary", summary="AI executive summary of the platform (Groq), cached 30 min")
def ai_summary(rng: Range = Query("30d", alias="range"), refresh: bool = False, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    key = f"exec:ai:{rng}"
    if not refresh:
        hit = cache.get(key)
        if hit:
            return json.loads(hit)
    ov = overview(rng=rng, admin=admin, db=db)
    att = attention(admin=admin, db=db)
    facts = {k: ov[k] for k in ("gyms", "members", "users", "checkins", "money", "memberships", "upi", "engagement", "funnel")}
    facts["at_risk_gyms"] = [{"name": r["name"], "flags": r["flags"]} for r in att["at_risk"][:5]]
    facts["pending_approvals"] = len(att["pending_approvals"])
    try:
        data = ai.chat_json([
            {"role": "system", "content": "You are the chief-of-staff for DUMBBEL.fit, a gym-management SaaS in India (gyms pay "
             "nothing per transaction; members pay gyms by UPI/cash). From the JSON metrics for the period, write an executive "
             "summary. Money values are in paise: convert to ₹ (lakh/crore where natural). Reply ONLY JSON {\"headline\": string "
             "(max 16 words), \"wins\": [2-3 strings], \"risks\": [2-3 strings], \"actions\": [3 specific next steps for the team]}. "
             "Each item max 25 words, cite numbers, never invent data."},
            {"role": "user", "content": json.dumps(facts, default=str)}], max_tokens=1800, temperature=0.3)
        out = {k: data.get(k) for k in ("headline", "wins", "risks", "actions")}
        out["kind"] = "ai"
    except ai.AiUnavailable:
        out = {"headline": f"{ov['gyms']['by_status'].get('active', 0)} active gyms · {ov['checkins']['total']} check-ins",
               "wins": [], "risks": [f"{len(att['at_risk'])} gyms at risk", f"{len(att['pending_approvals'])} gyms waiting for approval"],
               "actions": ["Review the approvals queue", "Call at-risk gyms"], "kind": "rule"}
    out["range"] = rng
    out["generated_at"] = utcnow().isoformat()
    cache.set(key, json.dumps(out, default=str), ex=1800)
    return out
