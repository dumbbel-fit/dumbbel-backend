"""DUMBBEL.fit executive console: how much the system can take, and where new income could come from.

  capacity   measured traffic and response times against the limits of the setup (Cloud Run, threads, database)
  revenue    who the gyms and members are, plus sized ways DUMBBEL.fit could earn

Measured numbers come from the request meter (app/services/metrics.py) and the tables. Everything that is a
guess (a price, a take-up rate, requests per visit) is returned as a named assumption the page lets you change."""
import json
import os
import resource
import sys
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import platform_admin
from app.models import User
from app.responses import DocRoute, ERRORS
from app.routers.insights import (build_forecast, build_impact, cached, pct, rows, today_ist, utc_start)
from app.services import metrics
from app.utils import iso
from datetime import datetime, timezone

router = APIRouter(prefix="/internal/insights", tags=["Executive console"], route_class=DocRoute, responses=ERRORS)
HEADROOM = 0.6          # plan to run at 60% of the ceiling; the rest absorbs bursts
ASSUMED_MS = 200        # used until the meter has seen enough requests
MIN_SAMPLES = 50
JOB_THREADS = 4         # app/services/jobs.py


def _one(db: Session, sql: str, **p):
    try:
        return db.execute(text(sql), p).first()
    except Exception:
        db.rollback()
        return None


def _threads() -> int:
    """Worker threads FastAPI runs the (blocking) endpoints on."""
    try:
        import anyio
        return int(anyio.from_thread.run(_limiter_tokens))
    except Exception:
        return 40


async def _limiter_tokens():
    import anyio.to_thread
    return anyio.to_thread.current_default_thread_limiter().total_tokens


def _read_int(path: str):
    try:
        v = open(path).read().strip()
        return int(v) if v.isdigit() else None
    except Exception:
        return None


def _memory() -> dict:
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    used = _read_int("/sys/fs/cgroup/memory.current") or _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if limit and limit > 1 << 50:   # "no limit" sentinel
        limit = None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
    mb = lambda b: round(b / 1048576) if b else None
    return {"limit_mb": mb(limit), "used_mb": mb(used), "process_peak_mb": round(rss_mb),
            "used_pct": pct(used, limit) if used and limit else None}


def _ts(epoch):
    return iso(datetime.fromtimestamp(epoch, timezone.utc)) if epoch else None


def _status(used, limit, watch=60, limit_at=85) -> str:
    if not used or not limit:
        return "ok"
    p = used / limit * 100
    return "limit" if p >= limit_at else "watch" if p >= watch else "ok"


# =====================================================================================================
# Capacity
# =====================================================================================================
def build_capacity(db: Session) -> dict:
    td = today_ist()
    m = metrics.snapshot()
    threads = _threads()
    pool = settings.db_pool_size + 10            # pool_size + max_overflow (app/db.py)
    inst = max(1, settings.run_max_instances)
    conc = max(1, settings.run_concurrency)
    slots_each = min(threads, pool, conc)
    slots = slots_each * inst
    binding = "database connections" if slots_each == pool else "worker threads" if slots_each == threads else "Cloud Run concurrency"

    measured = (m["sampled"] or 0) >= MIN_SAMPLES and m["avg_ms"]
    avg_ms = m["avg_ms"] if measured else ASSUMED_MS
    ceiling_rpm = round(slots / (avg_ms / 1000) * 60)
    safe_rpm = round(ceiling_rpm * HEADROOM)

    # ---- people ----
    people = rows(db, """SELECT
        (SELECT COUNT(*) FROM gyms WHERE status='active') gyms,
        (SELECT COUNT(DISTINCT m.member_id) FROM memberships m JOIN gyms g ON g.id=m.gym_id AND g.status='active'
          WHERE m.state <> 'cancelled' AND m.start_date <= :d AND m.end_date >= :d) active_members,
        (SELECT COUNT(*) FROM gym_members gm JOIN gyms g ON g.id=gm.gym_id AND g.status='active' WHERE gm.status='active') members,
        (SELECT COUNT(*) FROM users WHERE deleted_at IS NULL) users,
        (SELECT COUNT(*) FROM users WHERE last_login_at >= :u30) users_30d,
        (SELECT COUNT(DISTINCT user_id) FROM sessions WHERE last_used_at >= :u1) users_today""",
                  d=td, u30=utc_start(td - timedelta(days=30)), u1=utc_start(td))[0]
    people = {k: int(v or 0) for k, v in people.items()}
    busy = _one(db, """SELECT local_date d, HOUR(DATE_ADD(at, INTERVAL 330 MINUTE)) h, COUNT(*) n FROM checkins
                       WHERE result='success' AND local_date >= :a GROUP BY d, h ORDER BY n DESC LIMIT 1""", a=td - timedelta(days=30))
    ci30 = _one(db, "SELECT COUNT(*) n, COUNT(DISTINCT local_date) days FROM checkins WHERE result='success' AND local_date >= :a",
                a=td - timedelta(days=30))
    peak_hour = {"checkins": int(busy.n), "date": str(busy.d), "hour": int(busy.h)} if busy else None
    peak_share = round(min(100.0, busy.n / people["active_members"] * 100), 1) if busy and people["active_members"] else None

    # ---- database ----
    maxc = _one(db, "SHOW VARIABLES LIKE 'max_connections'")
    now_c = _one(db, "SHOW GLOBAL STATUS LIKE 'Threads_connected'")
    top_c = _one(db, "SHOW GLOBAL STATUS LIKE 'Max_used_connections'")
    max_conn = int(maxc[1]) if maxc else None
    tables = []
    try:
        tables = rows(db, """SELECT table_name AS name, table_rows AS approx_rows, data_length + index_length AS bytes
                             FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY bytes DESC""")
    except Exception:
        db.rollback()
    size_mb = round(sum(int(t["bytes"] or 0) for t in tables) / 1048576, 1)
    files = _one(db, "SELECT COUNT(*) n, COALESCE(SUM(size), 0) b FROM stored_files")
    files_mb = round(int(files.b) / 1048576, 1) if files else 0.0
    limit_mb = settings.db_storage_limit_mb
    pool_total = pool * inst
    mem = _memory()
    redis = bool(settings.redis_url)

    layers = [
        {"key": "frontend", "name": "Website (Hostinger)", "status": "ok", "measured": False,
         "limit": "Static files", "now": "Not measured here",
         "note": "The site is a set of static files. A visitor downloads them once, then every action goes to the API, "
                 "so the website is not what runs out first. Hostinger's own usage page shows bandwidth and file counts."},
        {"key": "instances", "name": "API servers (Cloud Run)", "measured": True,
         "limit": f"{inst} server{'s' if inst > 1 else ''} × {conc} requests at once",
         "now": f"{m['inflight']} in flight now, {m['peak_inflight']} at the busiest moment",
         "status": _status(m["peak_inflight"], slots),
         "note": "One server means a short pause for everyone during a restart or deploy." if inst == 1 else
                 "Requests are spread across the servers."},
        {"key": "threads", "name": "Worker threads per server", "measured": True, "limit": f"{threads} threads",
         "now": f"{m['peak_inflight']} busiest", "status": _status(m["peak_inflight"], threads * inst),
         "note": "Each request that talks to the database holds one thread until it finishes."},
        {"key": "db_conn", "name": "Database connections", "measured": max_conn is not None,
         "limit": f"{pool} per server" + (f", database allows {max_conn}" if max_conn else ""),
         "now": (f"{int(now_c[1])} open now" if now_c else "Not readable") + (f", {int(top_c[1])} at most so far" if top_c else ""),
         "status": "limit" if max_conn and pool_total > max_conn else _status(int(top_c[1]) if top_c else 0, max_conn),
         "note": (f"The app can open up to {pool_total} connections; the database plan allows {max_conn}. "
                  "This is the first limit a request hits, so it sets the ceiling below." if max_conn else
                  "The app can open up to %d connections." % pool_total)},
        {"key": "db_size", "name": "Database storage", "measured": True,
         "limit": f"{limit_mb:,} MB in the plan" if limit_mb else "Plan limit not set (DB_STORAGE_LIMIT_MB)",
         "now": f"{size_mb:,} MB used, {files_mb:,} MB of it uploaded files",
         "status": _status(size_mb, limit_mb, 60, 85) if limit_mb else ("watch" if files_mb > 500 else "ok"),
         "note": "Photos and documents are stored inside the database. They grow faster than everything else; "
                 "moving them to a storage bucket keeps the database small."},
        {"key": "memory", "name": "Server memory", "measured": mem["limit_mb"] is not None,
         "limit": f"{mem['limit_mb']:,} MB" if mem["limit_mb"] else "Not readable on this machine",
         "now": f"{mem['used_mb']:,} MB in use" if mem["used_mb"] else f"{mem['process_peak_mb']:,} MB peak for the app",
         "status": _status(mem["used_mb"], mem["limit_mb"], 70, 88), "note": "Uploads and imports are the heaviest users of memory."},
        {"key": "cache", "name": "Shared cache (rate limits, OTP locks)", "measured": True,
         "limit": "Redis" if redis else "Kept inside the one server",
         "now": "Shared between servers" if redis else "Not shared",
         "status": "ok" if redis or inst == 1 else "limit",
         "note": "Ready for more than one server." if redis else
                 "Rate limits, OTP attempt counts and QR replay checks live in the server's memory. "
                 "Add Redis (REDIS_URL) before raising max instances above 1, or those checks stop being reliable."},
        {"key": "jobs", "name": "Background jobs (WhatsApp, notifications)", "measured": False,
         "limit": f"{JOB_THREADS} at a time", "now": "Queue length is not measured", "status": "ok",
         "note": "Jobs run inside the API server and are lost if it restarts mid-send; reminders are retried on the next run."},
    ]

    steps = []
    if not redis:
        steps.append({"when": "Before adding a second server", "do": "Add Redis and set REDIS_URL, then raise --max-instances.",
                      "why": "Without it each server keeps its own rate limits and OTP locks."})
    if max_conn and pool_total > max_conn * 0.8:
        steps.append({"when": "Now", "do": f"Lower DB_POOL_SIZE or move to a database plan with more than {max_conn} connections.",
                      "why": f"The servers can ask for {pool_total} connections; the database allows {max_conn}."})
    if files_mb > 200 or (size_mb and files_mb / size_mb > 0.5):
        steps.append({"when": "Soon", "do": "Move uploaded photos and documents to a storage bucket (S3_BUCKET).",
                      "why": f"Files are {files_mb:,} MB of the {size_mb:,} MB database."})
    if m["p95_ms"] and m["p95_ms"] > 1500:
        steps.append({"when": "Now", "do": "Speed up the slowest endpoints listed below.",
                      "why": f"1 request in 20 takes longer than {round(m['p95_ms'] / 1000, 1)} s."})
    if m["requests_24h"] and m["errors_24h"] / m["requests_24h"] > 0.01:
        steps.append({"when": "Now", "do": "Check the server logs for errors.",
                      "why": f"{m['errors_24h']} of {m['requests_24h']} requests failed in the last 24 hours."})
    util = pct(m["peak_per_min"], safe_rpm)
    if util and util >= 70:
        steps.append({"when": "Now", "do": "Raise --max-instances (after adding Redis) or the database pool.",
                      "why": f"The busiest minute used {util}% of the safe load."})
    steps.append({"when": "Before a big launch", "do": "Run a load test against a staging copy.",
                  "why": "The ceiling here is arithmetic from measured response times; only a load test proves it."})

    return {
        "setup": {"instances": inst, "concurrency": conc, "threads": threads, "db_pool": pool, "db_max_connections": max_conn,
                  "cpus": os.cpu_count(), "memory": mem, "cache": "redis" if redis else "memory",
                  "service": os.environ.get("K_SERVICE"), "revision": os.environ.get("K_REVISION"),
                  "config_note": "Servers and concurrency are read from RUN_MAX_INSTANCES and RUN_CONCURRENCY; "
                                 "keep them equal to the deploy flags."},
        "traffic": {**{k: m[k] for k in ("requests_total", "requests_24h", "errors_24h", "slow_24h", "avg_ms", "p50_ms", "p95_ms",
                                         "p99_ms", "sampled", "last_min", "peak_per_min", "inflight", "peak_inflight", "uptime_sec")},
                    "since": _ts(m["since"]), "peak_minute_at": _ts(m["peak_minute_at"]),
                    "hours": [{**h, "t": _ts(h["t"])} for h in m["hours"]], "routes": m["routes"]},
        "ceiling": {"slots": slots, "bound_by": binding, "avg_ms": avg_ms, "basis": "measured" if measured else "assumed",
                    "ceiling_per_min": ceiling_rpm, "safe_per_min": safe_rpm, "headroom_pct": round(HEADROOM * 100),
                    "peak_used_pct": util,
                    "method": "requests at once ÷ average response time, with 40% kept free for bursts"},
        "people": {**people, "peak_hour": peak_hour, "peak_hour_share_pct": peak_share,
                   "checkins_per_day": round(int(ci30.n) / int(ci30.days)) if ci30 and ci30.days else 0,
                   "members_per_gym": round(people["active_members"] / people["gyms"]) if people["gyms"] else None},
        "assumptions": {"requests_per_visit": 15, "peak_hour_share_pct": peak_share or 15.0,
                        "peak_share_basis": "measured" if peak_share else "assumed"},
        "database": {"size_mb": size_mb, "files_mb": files_mb, "files": int(files.n) if files else 0, "limit_mb": limit_mb,
                     "connections_now": int(now_c[1]) if now_c else None, "connections_peak": int(top_c[1]) if top_c else None,
                     "tables": [{"name": t["name"], "approx_rows": int(t["approx_rows"] or 0),
                                 "mb": round(int(t["bytes"] or 0) / 1048576, 2)} for t in tables[:10]]},
        "layers": layers, "next_steps": steps,
    }


@router.get("/capacity", summary="How much load the setup can take: measured traffic and response times against each limit")
def capacity(admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    out = build_capacity(db)   # live numbers: not cached
    out["generated_at"] = iso(datetime.now(timezone.utc))
    return out


# =====================================================================================================
# Revenue ideas
# =====================================================================================================
BANDS = [("0", "No active members", 0, 0, 0), ("1-50", "1 to 50 members", 1, 50, 0),
         ("51-150", "51 to 150 members", 51, 150, 49900), ("151-300", "151 to 300 members", 151, 300, 99900),
         ("300+", "More than 300 members", 301, 10 ** 9, 199900)]
TERMS = [(45, "1 month"), (135, "3 months"), (270, "6 months"), (10 ** 6, "12 months")]


def build_revenue(db: Session) -> dict:
    td = today_ist()
    u30, u90 = utc_start(td - timedelta(days=30)), utc_start(td - timedelta(days=90))
    gyms = rows(db, """SELECT g.id gym_id, g.name, g.area, g.city,
          (SELECT COUNT(DISTINCT m.member_id) FROM memberships m WHERE m.gym_id=g.id AND m.state <> 'cancelled'
            AND m.start_date <= :d AND m.end_date >= :d) active_members,
          (SELECT COALESCE(SUM(p.amount_paise), 0) FROM payments p WHERE p.gym_id=g.id AND p.status='recorded'
            AND p.source <> 'import' AND p.paid_at >= :u30) collected_paise
        FROM gyms g WHERE g.status='active'""", d=td, u30=u30)
    for g in gyms:
        g["active_members"], g["collected_paise"] = int(g["active_members"] or 0), int(g["collected_paise"] or 0)
    active_members = sum(g["active_members"] for g in gyms)
    collected = sum(g["collected_paise"] for g in gyms)

    bands = []
    for key, label, lo, hi, price in BANDS:
        gs = [g for g in gyms if lo <= g["active_members"] <= hi]
        bands.append({"key": key, "label": label, "gyms": len(gs), "members": sum(g["active_members"] for g in gs),
                      "collected_paise": sum(g["collected_paise"] for g in gs), "default_price_paise": price})

    # ---- who the members are ----
    sold = rows(db, """SELECT DATEDIFF(m.end_date, m.start_date) + 1 days, m.price_paise FROM memberships m
                       JOIN gyms g ON g.id=m.gym_id AND g.status='active'
                       WHERE m.source <> 'import' AND m.state <> 'cancelled' AND m.created_at >= :u90""", u90=u90)
    terms = []
    for i, (cap_days, label) in enumerate(TERMS):
        lo = TERMS[i - 1][0] if i else 0
        ms = [r for r in sold if lo < int(r["days"]) <= cap_days]
        value = sum(int(r["price_paise"] or 0) for r in ms)
        months = sum(max(1, round(int(r["days"]) / 30)) for r in ms)
        terms.append({"label": label, "sold": len(ms), "share_pct": pct(len(ms), len(sold)), "value_paise": value,
                      "per_month_paise": round(value / months) if months else None})
    methods = rows(db, """SELECT p.method, COUNT(*) n, COALESCE(SUM(p.amount_paise), 0) v FROM payments p
                          JOIN gyms g ON g.id=p.gym_id AND g.status='active'
                          WHERE p.status='recorded' AND p.source <> 'import' AND p.paid_at >= :u90 GROUP BY p.method""", u90=u90)
    mtotal = sum(int(r["v"]) for r in methods)
    methods = sorted([{"method": r["method"], "payments": int(r["n"]), "value_paise": int(r["v"]),
                       "share_pct": pct(int(r["v"]), mtotal)} for r in methods], key=lambda r: -r["value_paise"])
    hrs = {int(r["h"]): int(r["n"]) for r in rows(db, """SELECT HOUR(DATE_ADD(at, INTERVAL 330 MINUTE)) h, COUNT(*) n FROM checkins
                                                          WHERE result='success' AND local_date >= :a GROUP BY h""", a=td - timedelta(days=30))}
    hours = [{"hour": h, "checkins": hrs.get(h, 0)} for h in range(24)]
    who = rows(db, """SELECT u.gender, TIMESTAMPDIFF(YEAR, u.dob, :d) age FROM users u WHERE u.id IN (
                        SELECT gm.user_id FROM gym_members gm JOIN memberships m ON m.member_id=gm.id
                        JOIN gyms g ON g.id=gm.gym_id AND g.status='active'
                        WHERE m.state <> 'cancelled' AND m.start_date <= :d AND m.end_date >= :d)""", d=td)
    gender = {"female": 0, "male": 0, "other": 0, "unknown": 0}
    ages = {"Under 25": 0, "25 to 34": 0, "35 to 44": 0, "45 and over": 0, "Not given": 0}
    for r in who:
        gender[r["gender"] or "unknown"] += 1
        a = r["age"]
        ages["Not given" if a is None else "Under 25" if a < 25 else "25 to 34" if a < 35 else "35 to 44" if a < 45 else "45 and over"] += 1
    app = rows(db, """SELECT
        (SELECT COUNT(DISTINCT s.user_id) FROM sessions s WHERE s.app='member' AND s.last_used_at >= :u30) app_users_30d,
        (SELECT COUNT(DISTINCT f.user_id) FROM food_logs f WHERE f.local_date >= :d30) food_users_30d,
        (SELECT COUNT(*) FROM gym_members gm JOIN gyms g ON g.id=gm.gym_id AND g.status='active'
          WHERE gm.status='active' AND gm.claimed_at IS NOT NULL) claimed,
        (SELECT COUNT(DISTINCT w.gym_id) FROM wa_messages w WHERE w.status IN ('sent','delivered','read') AND w.created_at >= :u30) wa_gyms,
        (SELECT COUNT(*) FROM wa_messages w WHERE w.status IN ('sent','delivered','read') AND w.created_at >= :u30) wa_sent,
        (SELECT COUNT(*) FROM gym_visits v WHERE v.created_at >= :u30) visits,
        (SELECT COUNT(*) FROM gym_visits v WHERE v.created_at >= :u30 AND v.status='joined') visits_joined,
        (SELECT COALESCE(SUM(p.amount_paise), 0) FROM payments p JOIN gyms g ON g.id=p.gym_id AND g.status='active'
          WHERE p.status='recorded' AND p.source <> 'import' AND p.method='upi' AND p.paid_at >= :u30) upi_paise""",
               u30=u30, d30=td - timedelta(days=30))[0]
    app = {k: int(v or 0) for k, v in app.items()}
    listed = 0
    for r in rows(db, "SELECT settings FROM gyms WHERE status='active'"):
        st = r["settings"]
        if isinstance(st, str):
            try:
                st = json.loads(st)
            except ValueError:
                st = None
        listed += 0 if isinstance(st, dict) and st.get("explore_listed") is False else 1
    fc = cached("ins:fc:30", False, lambda: build_forecast(db, 30))["totals"]
    im = cached("ins:impact:30d", False, lambda: build_impact(db, "30d"))
    wa_earned = im["whatsapp"]["after_reminder_paise"]
    ex_earned = im["explore"]["earned_paise"]
    paying_gyms = sum(b["gyms"] for b in bands if b["default_price_paise"])

    def inr(p):
        return f"₹{round(p / 100):,}"

    ideas = [
        {"key": "subscription", "title": "Monthly plan for gyms, priced by size", "who_pays": "Gym owner", "effort": "medium",
         "why": f"{paying_gyms} of {len(gyms)} live gyms have more than 50 active members. Gyms collected "
                f"{inr(collected)} through DUMBBEL.fit in the last 30 days and pay nothing for it.",
         "needs": "A billing screen, a free tier for small gyms, and a way to collect (UPI AutoPay or a payment gateway).",
         "take_pct": 60, "take_label": "Gyms that agree to pay",
         "rows": [{"label": b["label"], "kind": "count", "base": b["gyms"], "unit": "gyms", "price_paise": b["default_price_paise"]}
                  for b in bands if b["key"] != "0"]},
        {"key": "whatsapp", "title": "WhatsApp reminders as a paid add-on", "who_pays": "Gym owner", "effort": "low",
         "why": f"{app['wa_gyms']} gyms sent {app['wa_sent']:,} WhatsApp messages in 30 days; {inr(wa_earned)} was renewed "
                "within 14 days of a reminder. The gym can see the money it brought in, which makes the price easy to justify.",
         "needs": "A switch per gym and a monthly charge. The feature already exists.",
         "take_pct": 70, "take_label": "Gyms that keep it when it is paid",
         "rows": [{"label": "Gyms sending reminders now", "kind": "count", "base": app["wa_gyms"], "unit": "gyms", "price_paise": 29900},
                  {"label": "Other live gyms that could start", "kind": "count", "base": max(0, len(gyms) - app["wa_gyms"]),
                   "unit": "gyms", "price_paise": 0}]},
        {"key": "leads", "title": "Charge per Explore visit booking", "who_pays": "Gym owner", "effort": "low",
         "why": f"{app['visits']} visits were booked through Explore in 30 days and {app['visits_joined']} became members, "
                f"worth {inr(ex_earned)} to the gyms.",
         "needs": "A per-lead charge on the gym's bill, and a refund rule for no-shows.",
         "take_pct": 100, "take_label": "Bookings that are billable",
         "rows": [{"label": "Visits booked a month", "kind": "count", "base": app["visits"], "unit": "visits", "price_paise": 5000}]},
        {"key": "featured", "title": "Featured listing in Explore", "who_pays": "Gym owner", "effort": "low",
         "why": f"{listed} gyms are listed in Explore. A top slot in an area is worth most where several gyms compete; "
                "the Demand map shows which areas those are.",
         "needs": "A 'featured' flag, a label on the card so it is honest, and a cap of one or two per area.",
         "take_pct": 15, "take_label": "Listed gyms that buy a slot",
         "rows": [{"label": "Gyms listed in Explore", "kind": "count", "base": listed, "unit": "gyms", "price_paise": 49900}]},
        {"key": "online_fee", "title": "Small fee on online payments", "who_pays": "Gym owner or member", "effort": "high",
         "why": f"{inr(app['upi_paise'])} was paid by UPI in 30 days. Today that money goes straight to the gym's own UPI ID, "
                "so DUMBBEL.fit cannot take a share of it.",
         "needs": "A payment gateway with split settlement, and KYC for every gym. The largest build on this list.",
         "take_pct": 50, "take_label": "UPI money that moves to the gateway",
         "rows": [{"label": "UPI collected a month", "kind": "money", "base": app["upi_paise"], "rate_pct": 1.0}]},
        {"key": "winback", "title": "Win-back campaigns, paid on success", "who_pays": "Gym owner", "effort": "medium",
         "why": f"{fc['lapsed_recent']} members worth {inr(fc['lapsed_recent_paise'])} lapsed in the last 14 days and have not renewed. "
                "DUMBBEL.fit sends the offer and takes a share only of what comes back.",
         "needs": "A campaign that messages lapsed members with an offer, and tracking of who returned (the renewal link already does this).",
         "take_pct": 20, "take_label": "Lapsed value that is won back",
         "rows": [{"label": "Lapsed in 14 days, not renewed", "kind": "money", "base": fc["lapsed_recent_paise"], "rate_pct": 10.0}]},
        {"key": "member_premium", "title": "Premium for members (AI diet, progress)", "who_pays": "Member", "effort": "medium",
         "why": f"{app['app_users_30d']} members used the app in 30 days and {app['food_users_30d']} logged food. "
                "Paid member features usually convert a small share, so the take-up starts low.",
         "needs": "A paywall on the AI features, and in-app purchase or UPI AutoPay.",
         "take_pct": 3, "take_label": "App users who subscribe",
         "rows": [{"label": "Members using the app a month", "kind": "count", "base": app["app_users_30d"], "unit": "members", "price_paise": 9900}]},
        {"key": "partners", "title": "Partner offers (supplements, gear, insurance)", "who_pays": "Partner brand", "effort": "medium",
         "why": f"{active_members:,} active members open a fitness app regularly. There is no data here yet on what they would buy; "
                "the value per member is a pure guess until one partner is tested.",
         "needs": "One partner, one offer placement in the member app, and click tracking.",
         "take_pct": 100, "take_label": "Members who see offers",
         "rows": [{"label": "Active members", "kind": "count", "base": active_members, "unit": "members", "price_paise": 300}]},
    ]
    top_gyms = sorted(gyms, key=lambda g: -g["collected_paise"])[:10]
    return {
        "today": {"gyms": len(gyms), "active_members": active_members, "collected_30d_paise": collected,
                  "platform_income_paise": 0,
                  "note": "DUMBBEL.fit has no billing yet, so it records no income of its own. Every idea below is new income."},
        "bands": bands, "terms": terms, "terms_sold": len(sold), "methods": methods, "hours": hours,
        "gender": gender, "ages": ages,
        "usage": {**app, "listed": listed, "wa_earned_paise": wa_earned, "explore_earned_paise": ex_earned,
                  "claimed_pct": pct(app["claimed"], active_members) if active_members else None},
        "top_gyms": top_gyms, "ideas": ideas,
        "method": "Each idea = the measured number × a price or rate × the share that takes it up. "
                  "The measured numbers are real; the prices and shares are starting guesses to change.",
    }


@router.get("/revenue", summary="Who the gyms and members are, and sized ways DUMBBEL.fit could earn (prices are assumptions)")
def revenue(refresh: bool = False, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    return cached("ins:revenue", refresh, lambda: build_revenue(db))
