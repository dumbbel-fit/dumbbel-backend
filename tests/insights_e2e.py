"""Executive console insights: call list + follow-ups, forecast, cohorts, adoption, benchmarks, impact, demand map.
Seeds one gym with known memberships / payments / messages and checks every number the console shows for it.

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/insights_e2e.py
"""
import os, random, sys, time, uuid
from datetime import date, datetime, timedelta, timezone
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from app.db import engine

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
B = os.environ.get("API", "http://localhost:8000/v1")
I = f"{B}/internal/insights"
c = httpx.Client(timeout=60)
fails: list[str] = []
IST = timezone(timedelta(hours=5, minutes=30))
td = datetime.now(IST).date()
now = datetime.now(timezone.utc).replace(tzinfo=None)


def H(t, idem=False):
    h = {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}
    if idem: h["Idempotency-Key"] = str(uuid.uuid4())
    return h
def chk(n, r, code=None):
    ok = r.status_code == code if code else r.status_code < 400
    print("OK  " if ok else "FAIL", r.status_code, n, "" if ok else r.text[:300]); ok or fails.append(n)
    try: return r.json()
    except Exception: return None
def expect(n, got, want):
    ok = got == want; print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}"); ok or fails.append(n)
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))
def login():
    r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()
def sql(q, **p):
    with engine.begin() as cn: return cn.execute(text(q), p)
def find(items, gid, key="gym_id"):
    return next((x for x in items if x[key] == gid), None)

# ---------- seed ----------
sa, sb, sc_ = login(), login(), login()
TA, TB, TC = sa["access_token"], sb["access_token"], sc_["access_token"]
tag = rnd()[-4:]
LAT, LNG = 17.45, 78.39
GA = chk("gym A", c.post(f"{B}/gyms", headers=H(TA), json={"name": f"Ins Alpha {tag}", "city": "Hyderabad", "area": "Madhapur"}))["id"]
GB = chk("gym B", c.post(f"{B}/gyms", headers=H(TB), json={"name": f"Ins Beta {tag}", "city": "Hyderabad"}))["id"]
GC = chk("gym C", c.post(f"{B}/gyms", headers=H(TC), json={"name": f"Ins Gamma {tag}", "city": "Pune"}))["id"]
sql("UPDATE gyms SET status='active', approved_at=:t, lat=:la, lng=:ln WHERE id=:g", g=GA, t=now - timedelta(days=100), la=LAT, ln=LNG)
sql("UPDATE gyms SET status='active', approved_at=:t WHERE id=:g", g=GB, t=now - timedelta(days=10))
sql("UPDATE gyms SET created_at=:t WHERE id=:g", g=GC, t=now - timedelta(days=3, hours=2))
PLAN = chk("plan", c.post(f"{B}/gyms/{GA}/plans", headers=H(TA), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}))["id"]
joined = (td.replace(day=1) - timedelta(days=40)).replace(day=1)  # first day, two months back
names = ["Old1", "Old2", "Old3", "Ren1", "Ren2", "Ren3", "Lap1", "Lap2", "Sure1"]
M = {}
for nm in names:
    M[nm] = c.post(f"{B}/gyms/{GA}/members", headers=H(TA, True), json={"name": nm, "phone": rnd()}).json()["member"]["id"]
sql("UPDATE gym_members SET joined_on=:j WHERE gym_id=:g", j=joined, g=GA)
MS = []  # (member, start, end) for the cohort cross-check
def ms(member, start, end, created=None, state="active"):
    mid = str(uuid.uuid4())
    sql("""INSERT INTO memberships (id, gym_id, member_id, plan_id, plan_name, price_paise, discount_paise, start_date, end_date, state,
           source, joining_fee_paise, gst_paise, created_at) VALUES (:i,:g,:m,:p,'Monthly',100000,0,:s,:e,:st,'manual',0,0,:c)""",
        i=mid, g=GA, m=M[member], p=PLAN, s=start, e=end, st=state, c=created or now - timedelta(days=60))
    MS.append((member, start, end)); return mid
D = lambda n: td + timedelta(days=n)
for nm in ["Old1", "Old2", "Old3"]: ms(nm, D(-69), D(-40))                       # ended 40 days ago, never renewed
REN = {}
for nm, end in [("Ren1", 5), ("Ren2", 10), ("Ren3", 20)]:
    ms(nm, D(-69), D(-40))                                                         # ended 40 days ago…
    REN[nm] = ms(nm, D(-39), D(end), created=now - timedelta(days=39))             # …and renewed
for nm in ["Lap1", "Lap2"]: ms(nm, D(-34), D(-5))                                  # lapsed 5 days ago
SURE1 = ms("Sure1", D(-26), D(3)); ms("Sure1", D(4), D(33), state="scheduled")     # ends in 3 days, next one already bought
def pay(member, membership, amt=100000):
    sql("""INSERT INTO payments (id, gym_id, member_id, membership_id, amount_paise, method, paid_at, source, receipt_no, status)
           VALUES (:i,:g,:m,:ms,:a,'cash',:t,'manual',:r,'recorded')""", i=str(uuid.uuid4()), g=GA, m=M[member], ms=membership, a=amt,
        t=now - timedelta(hours=2), r="T" + uuid.uuid4().hex[:10])
pay("Ren1", REN["Ren1"]); pay("Sure1", SURE1)
def checkins(n, days_ago):
    for i in range(n):
        sql("INSERT INTO checkins (id, gym_id, member_id, at, local_date, method, result) VALUES (:i,:g,:m,:a,:d,'manual','success')",
            i=str(uuid.uuid4()), g=GA, m=M["Ren1"], a=now - timedelta(days=days_ago, minutes=i), d=D(-days_ago))
checkins(25, 20); checkins(5, 1)
def visit(status, member=None):
    sql("""INSERT INTO gym_visits (id, gym_id, user_id, name, phone, visit_date, slot, status, member_id, created_at, handled_at)
           VALUES (:i,:g,:u,'Walk In','+919000000000',:d,'18:00',:s,:m,:c,:h)""", i=str(uuid.uuid4()), g=GA, u=sa["user"]["id"], d=D(1),
        s=status, m=member, c=now - timedelta(days=2), h=now - timedelta(days=2) + timedelta(minutes=30) if member else None)
visit("requested"); visit("joined", M["Sure1"])
def wa(member, status, created):
    i = str(uuid.uuid4())
    sql("""INSERT INTO wa_messages (id, gym_id, member_id, phone, event, category, template_key, status, attempts, `trigger`, created_at)
           VALUES (:i,:g,:m,'+919000000001','expiry_reminder','UTILITY','expiry_reminder',:s,1,'auto',:c)""", i=i, g=GA, m=M[member], s=status, c=created)
    return i
w1 = wa("Ren1", "delivered", now - timedelta(days=41)); wa("Lap1", "failed", now - timedelta(days=6))
sql("""INSERT INTO renewal_tokens (id, token_hash, gym_id, member_id, message_id, source, expires_at, opened_at, used_at, result_membership_id, created_at)
       VALUES (:i,:h,:g,:m,:w,'whatsapp',:x,:o,:o,:r,:c)""", i=str(uuid.uuid4()), h=uuid.uuid4().hex + uuid.uuid4().hex, g=GA, m=M["Ren1"], w=w1,
    x=now, o=now - timedelta(days=39), r=REN["Ren1"], c=now - timedelta(days=41))
sql("UPDATE users SET is_platform_admin=1 WHERE id=:u", u=sa["user"]["id"])
A = H(TA)
R = {"refresh": "true"}

print("\n== access")
for path in ["actions", "forecast", "cohorts", "adoption", "benchmarks", "impact", "demand", "headline", "capacity", "revenue", f"forecast/{GA}", f"followups?gym_id={GA}"]:
    chk(f"non-admin refused: {path}", c.get(f"{I}/{path}", headers=H(TB)), 403)
chk("no token refused", c.get(f"{I}/actions"), 401)
chk("non-admin can't log a follow-up", c.post(f"{I}/followups", headers=H(TB), json={"gym_id": GA, "action": "called"}), 403)

print("\n== call list")
a = chk("actions", c.get(f"{I}/actions", headers=A, params=R))
ra, rb, rc = find(a["items"], GA), find(a["items"], GB), find(a["items"], GC)
expect("A reasons", sorted(x["key"] for x in ra["reasons"]), ["checkins_drop", "renewals_no_whatsapp", "visits_waiting"])
expect("A is urgent, top reason is the unanswered visit", (ra["level"], ra["reasons"][0]["key"]), ("urgent", "visits_waiting"))
expect("A renewals alert carries ₹1,000", next(x for x in ra["reasons"] if x["key"] == "renewals_no_whatsapp")["impact_paise"], 100000)
expect("B reasons (stuck onboarding)", sorted(x["key"] for x in rb["reasons"]), ["no_members", "no_plans"])
expect("C reasons (pending 3 days)", [x["key"] for x in rc["reasons"]], ["pending_approval"])
expect("sorted by score", [r["score"] for r in a["items"]], sorted((r["score"] for r in a["items"]), reverse=True))
expect("owner phone present for the call", bool(ra["owner_phone"]), True)
chk("snooze needs a duration", c.post(f"{I}/followups", headers=A, json={"gym_id": GA, "action": "snoozed", "reason": "checkins_drop"}), 422)
chk("unknown alert refused", c.post(f"{I}/followups", headers=A, json={"gym_id": GA, "action": "called", "reason": "nope"}), 422)
chk("unknown gym", c.post(f"{I}/followups", headers=A, json={"gym_id": str(uuid.uuid4()), "action": "called"}), 404)
chk("empty note refused", c.post(f"{I}/followups", headers=A, json={"gym_id": GA, "action": "note", "note": "  "}), 422)
chk("snooze one alert", c.post(f"{I}/followups", headers=A, json={"gym_id": GA, "action": "snoozed", "reason": "checkins_drop", "snooze_days": 7}), 201)
chk("log a call", c.post(f"{I}/followups", headers=A, json={"gym_id": GA, "action": "called", "reason": "visits_waiting", "note": "Owner will reply today"}), 201)
chk("snooze whole gym B", c.post(f"{I}/followups", headers=A, json={"gym_id": GB, "action": "snoozed", "snooze_days": 3}), 201)
a2 = chk("actions again (cache was cleared)", c.get(f"{I}/actions", headers=A))
ra = find(a2["items"], GA)
expect("snoozed alert hidden", sorted(x["key"] for x in ra["reasons"]), ["renewals_no_whatsapp", "visits_waiting"])
expect("last contact shown", (ra["last_contact"]["action"], ra["last_contact"]["note"]), ("called", "Owner will reply today"))
expect("snoozed gym hidden", find(a2["items"], GB), None)
expect("snoozed counter grew", a2["snoozed"] >= a["snoozed"] + 2, True)
h = chk("history", c.get(f"{I}/followups", headers=A, params={"gym_id": GA}))
expect("history newest first", [x["action"] for x in h["items"]], ["called", "snoozed"])
chk("bring B back", c.delete(f"{I}/followups/snooze", headers=A, params={"gym_id": GB}), 204)
expect("B is back", find(c.get(f"{I}/actions", headers=A).json()["items"], GB) is not None, True)

print("\n== forecast")
f = chk("forecast 30", c.get(f"{I}/forecast", headers=A, params=R))
fa = find(f["items"], GA)
expect("A renewal rate 3 of 6 = 50% from its own history", (fa["rate_pct"], fa["rate_basis"], fa["rate_sample"]), (50.0, "gym", 6))
expect("A due / value / expected", (fa["due"], fa["due_paise"], fa["expected_paise"]), (3, 300000, 150000))
expect("A already renewed (not counted as due)", fa["already_renewed"], 1)
expect("A lapsed: 2 recent, 3 older", (fa["lapsed_recent"], fa["lapsed_recent_paise"], fa["lapsed_old"], fa["lapsed_old_paise"]), (2, 200000, 3, 300000))
expect("A new sales (first-time payment only) and actual", (fa["new_sales_paise"], fa["actual_prev_paise"]), (100000, 200000))
t = f["totals"]
expect("totals add up", (t["due_paise"], t["expected_paise"], t["forecast_paise"]),
       (sum(x["due_paise"] for x in f["items"]), sum(x["expected_paise"] for x in f["items"]), t["expected_paise"] + t["new_sales_paise"]))
expect("weeks add up to the total", (sum(w["due_paise"] for w in f["weeks"]), sum(w["expected_paise"] for w in f["weeks"]), len(f["weeks"])),
       (t["due_paise"], t["expected_paise"], 5))
f7 = chk("forecast 90", c.get(f"{I}/forecast", headers=A, params={"horizon": 90, **R}))
expect("90 days: Sure1's next membership now falls due", find(f7["items"], GA)["due"], 4)
chk("bad horizon", c.get(f"{I}/forecast", headers=A, params={"horizon": 45}), 422)
g = chk("forecast gym", c.get(f"{I}/forecast/{GA}", headers=A))
expect("due members, soonest first", [(x["name"], x["days"]) for x in g["due"]], [("Ren1", 5), ("Ren2", 10), ("Ren3", 20)])
expect("lapsed members, latest first", [x["name"] for x in g["lapsed"]][:2] in (["Lap1", "Lap2"], ["Lap2", "Lap1"]), True)
expect("lapsed count", len(g["lapsed"]), 5)
expect("no phone numbers in the drill-down", any("phone" in x for x in g["due"] + g["lapsed"]), False)
chk("unknown gym", c.get(f"{I}/forecast/{uuid.uuid4()}", headers=A), 404)

print("\n== cohorts")
ymi = lambda d: d.year * 12 + d.month - 1
cur, c0 = ymi(td), ymi(joined)
want = [len({m for m, s, e in MS if ymi(s) <= c0 + k <= ymi(e)}) for k in range(cur - c0 + 1)]
co = chk("member cohorts (paying)", c.get(f"{I}/cohorts", headers=A, params={"gym_id": GA, **R}))
row = next(x for x in co["cohorts"] if x["cohort"] == joined.strftime("%Y-%m"))
expect("cohort size", (co["total"], row["size"]), (9, 9))
expect("still paying each month", [x["n"] for x in row["cells"]], want)
expect("current month marked partial", [x["partial"] for x in row["cells"]], [False] * (len(want) - 1) + [True])
cv = chk("member cohorts (visiting)", c.get(f"{I}/cohorts", headers=A, params={"gym_id": GA, "metric": "visiting", **R}))
vrow = next(x for x in cv["cohorts"] if x["cohort"] == joined.strftime("%Y-%m"))
months_seen = {ymi(D(-20)) - c0, ymi(D(-1)) - c0}
expect("visiting: only Ren1 checked in", [x["n"] for x in vrow["cells"]], [1 if k in months_seen else 0 for k in range(cur - c0 + 1)])
cg = chk("gym cohorts", c.get(f"{I}/cohorts", headers=A, params={"kind": "gyms", "months": 12, **R}))
grow = next(x for x in cg["cohorts"] if x["cohort"] == (td - timedelta(days=100)).strftime("%Y-%m"))
expect("gym cohort contains A and it is active this month", (grow["size"] >= 1, grow["cells"][-1]["n"] >= 1), (True, True))
expect("12 cohorts", len(cg["cohorts"]), 12)

print("\n== adoption")
ad = chk("adoption", c.get(f"{I}/adoption", headers=A, params=R))
expect("A features", find(ad["items"], GA)["features"],
       {"plans": 2, "members": 2, "checkins": 2, "gym_qr": 0, "payments": 2, "upi": 0, "member_app": 0, "whatsapp": 0, "explore": 1, "visits": 2, "staff": 0})
expect("A score = 11 of 22", find(ad["items"], GA)["score"], 50)
expect("B uses nothing", sum(find(ad["items"], GB)["features"].values()), 1)  # listed on Explore by default
expect("pending gym not in the grid", find(ad["items"], GC), None)
expect("feature totals cover every gym", all(x["active"] + x["setup"] + x["none"] == ad["gyms"] for x in ad["features"]), True)

print("\n== benchmarks")
b = chk("benchmarks", c.get(f"{I}/benchmarks", headers=A, params=R))
ba = find(b["items"], GA)
expect("A metrics", (ba["renewal_rate_pct"], ba["active_members"], ba["visits_per_week"], ba["revenue_per_member_paise"], ba["price_month_paise"], ba["app_adoption_pct"]),
       (50.0, 4, 1.75, 50000, 100000, 0.0))
expect("B has no rate (too little history) rather than a made-up one", find(b["items"], GB)["renewal_rate_pct"], None)
expect("percentiles in range", all(v is None or 0 <= v <= 100 for v in ba["percentile"].values()), True)
expect("city medians include Hyderabad", any(x["city"] == "Hyderabad" for x in b["cities"]), True)

print("\n== demand map (logged from real Explore calls)")
rl, rg = round(10 + random.random() * 2, 2), round(72 + random.random(), 2)
term = "zz" + tag
chk("search near A", c.get(f"{B}/discover/gyms", params={"lat": LAT, "lng": LNG, "radius_km": 2}))
chk("search far from any gym", c.get(f"{B}/discover/gyms", params={"lat": rl + 0.001, "lng": rg + 0.001, "radius_km": 5}))
chk("same search again (must not double count)", c.get(f"{B}/discover/gyms", params={"lat": rl + 0.002, "lng": rg, "radius_km": 5}))
chk("search by a term nobody matches", c.get(f"{B}/discover/gyms", params={"q": term}))
chk("plain list (not demand)", c.get(f"{B}/discover/gyms"))
chk("open A's page", c.get(f"{B}/discover/gyms/{GA}"))
chk("open A's page again", c.get(f"{B}/discover/gyms/{GA}"))
time.sleep(2.5)
d = chk("demand", c.get(f"{I}/demand", headers=A, params=R))
far = next((x for x in d["cells"] if (x["lat"], x["lng"]) == (rl, rg)), None)
expect("far area: 1 search, no gym nearby", far and (far["searches"], far["gyms_3km"], far["gap"], far["empty"]), (1, 0, "none", 1))
expect("far area knows the nearest gym is far", far and far["nearest_km"] > 3, True)
near = next((x for x in d["cells"] if (x["lat"], x["lng"]) == (LAT, LNG)), None)
expect("near A: served", near and (near["gyms_3km"] >= 1, near["gap"] != "none"), (True, True))
expect("term logged as empty", next((x for x in d["terms"] if x["term"] == term), None), {"term": term, "searches": 1, "empty": 1})
expect("A viewed once in this run (second open throttled)", sql("SELECT COUNT(*) FROM explore_events WHERE kind='view' AND gym_id=:g", g=GA).scalar(), 1)
expect("stored location is rounded", sql("SELECT COUNT(*) FROM explore_events WHERE lat <> ROUND(lat, 2) OR lng <> ROUND(lng, 2)").scalar(), 0)
expect("A is a supply pin", find(d["gyms"], GA, "id")["listed"], True)

print("\n== impact")
im = chk("impact 90d", c.get(f"{I}/impact", headers=A, params={"range": "90d", **R}))
wa_a, ex_a = find(im["whatsapp"]["gyms"], GA), find(im["explore"]["gyms"], GA)
expect("WhatsApp A: 1 sent, 1 delivered, 1 failed", (wa_a["sent"], wa_a["delivered"], wa_a["failed"]), (1, 1, 1))
expect("WhatsApp A: 1 renewal after a reminder, ₹1,000", (wa_a["after_reminder"], wa_a["after_reminder_paise"]), (1, 100000))
expect("WhatsApp A: 1 link made, opened, used", (wa_a["links"], wa_a["link_opens"], wa_a["link_renewals"], wa_a["link_paise"]), (1, 1, 1, 100000))
expect("Explore A: 1 view, 2 booked, 1 joined, 1 waiting, ₹1,000", (ex_a["views"], ex_a["booked"], ex_a["joined"], ex_a["waiting"], ex_a["earned_paise"]), (1, 2, 1, 1, 100000))
expect("Explore A: replied in 30 min", ex_a["avg_response_min"], 30)
expect("total = WhatsApp + Explore", im["total_paise"], im["whatsapp"]["after_reminder_paise"] + im["explore"]["earned_paise"])
im7 = chk("impact 7d", c.get(f"{I}/impact", headers=A, params={"range": "7d", **R}))
expect("7d: the old reminder is outside the window", (find(im7["whatsapp"]["gyms"], GA) or {}).get("after_reminder", 0), 0)

print("\n== headline")
hd = chk("headline", c.get(f"{I}/headline", headers=A))
expect("headline matches the forecast", (hd["due_paise"], hd["expected_paise"]), (t["due_paise"], t["expected_paise"]))
print("\n== capacity")
cp = chk("capacity", c.get(f"{I}/capacity", headers=A))
expect("capacity: 8 layers, each with a status", (len(cp["layers"]), all(x["status"] in ("ok", "watch", "limit") for x in cp["layers"])), (8, True))
expect("capacity: the meter counted this run", cp["traffic"]["requests_total"] > 20 and cp["traffic"]["sampled"] > 20, True)
expect("capacity: 24 hourly points", len(cp["traffic"]["hours"]), 24)
expect("capacity: routes are templates, never real ids", any(GA in x["route"] for x in cp["traffic"]["routes"]), False)
ce = cp["ceiling"]
expect("capacity: slots = smallest of threads, pool, concurrency", ce["slots"], min(cp["setup"]["threads"], cp["setup"]["db_pool"], cp["setup"]["concurrency"]) * cp["setup"]["instances"])
expect("capacity: ceiling arithmetic", ce["ceiling_per_min"], round(ce["slots"] / (ce["avg_ms"] / 1000) * 60))
expect("capacity: safe load is 60% of the ceiling", ce["safe_per_min"], round(ce["ceiling_per_min"] * 0.6))
expect("capacity: counts the live gyms", cp["people"]["gyms"], sql("SELECT COUNT(*) FROM gyms WHERE status='active'").scalar())
expect("capacity: database size and tables read", cp["database"]["size_mb"] > 0 and len(cp["database"]["tables"]) > 0, True)
expect("capacity: always ends with a load-test step", cp["next_steps"][-1]["when"], "Before a big launch")
chk("capacity needs sign-in", c.get(f"{I}/capacity"), 401)

print("\n== revenue ideas")
rv = chk("revenue", c.get(f"{I}/revenue", headers=A, params=R))
expect("revenue: bands cover every live gym", sum(b["gyms"] for b in rv["bands"]), rv["today"]["gyms"])
expect("revenue: bands add up to the members and money", (sum(b["members"] for b in rv["bands"]), sum(b["collected_paise"] for b in rv["bands"])),
       (rv["today"]["active_members"], rv["today"]["collected_30d_paise"]))
expect("revenue: collected matches the payments table", rv["today"]["collected_30d_paise"], int(sql(
    "SELECT COALESCE(SUM(p.amount_paise),0) FROM payments p JOIN gyms g ON g.id=p.gym_id AND g.status='active' "
    "WHERE p.status='recorded' AND p.source<>'import' AND p.paid_at >= :s",
    s=__import__("app.routers.insights", fromlist=["x"]).utc_start(__import__("app.routers.insights", fromlist=["x"]).today_ist() - __import__("datetime").timedelta(days=30))).scalar()))
expect("revenue: 8 ideas, each with rows and a take-up", (len(rv["ideas"]), all(i["rows"] and 0 < i["take_pct"] <= 100 for i in rv["ideas"])), (8, True))
sub = next(i for i in rv["ideas"] if i["key"] == "subscription")
expect("revenue: plan idea counts gyms with members", sum(r["base"] for r in sub["rows"]), rv["today"]["gyms"] - rv["bands"][0]["gyms"])
expect("revenue: 24 hours, shares of terms add up", (len(rv["hours"]), sum(t["sold"] for t in rv["terms"])), (24, rv["terms_sold"]))
expect("revenue: no platform income is claimed", rv["today"]["platform_income_paise"], 0)
expect("revenue: gender counts cover the active members", sum(rv["gender"].values()) <= rv["today"]["active_members"], True)
chk("revenue needs sign-in", c.get(f"{I}/revenue"), 401)

sql("UPDATE users SET is_platform_admin=0 WHERE id=:u", u=sa["user"]["id"])
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
