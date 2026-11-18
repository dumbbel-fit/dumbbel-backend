"""People coming in on an expired plan are visible; who came in first; the home-screen tiles can be opened as lists.

  * a check-in refused because the plan ran out shows up in /checkins/denied (one row per person, attempts counted)
  * the home summary counts them and puts them first under “Needs attention”; renewing takes them off
  * the feed can be read in arrival order and marks the first person in today and each person's first-ever visit
  * members list filters new_month / renewed_month match the home-screen numbers
  * the console gets the same numbers per gym and in total

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/expired_checkins_e2e.py"""
import os
import random
import sys
import uuid
from datetime import date, timedelta

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails: list[str] = []


def H(t, idem=False, app="admin"):
    h = {"Authorization": f"Bearer {t}", "X-App": app, "X-App-Version": "1.0.0"}
    if idem:
        h["Idempotency-Key"] = str(uuid.uuid4())
    return h


def chk(n, r, code=None):
    ok = r.status_code == code if code else r.status_code < 400
    print("OK  " if ok else "FAIL", r.status_code, n, "" if ok else r.text[:300])
    ok or fails.append(n)
    try:
        return r.json()
    except Exception:
        return None


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))


def login(phone, app="web"):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": app}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


T = login(rnd())
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": "Expired Gym " + rnd()[-4:], "city": "Hyderabad"}))["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json()
P = P.get("plan", P)["id"]


def add(name, paid=100000):
    r = chk(f"add {name}", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={
        "name": name, "phone": rnd(), "membership": {"plan_id": P}, "payment": {"amount_paise": paid, "method": "cash"}}), 201)
    return r["member"]["id"], r["membership"]["id"]


def checkin(mid):
    return c.post(f"{B}/gyms/{G}/checkins/manual", headers=H(T, True), json={"member_id": mid}).json()


ok_m, _ = add("Early Bird")
late_m, _ = add("Late Comer")
x_m, x_ms = add("Expired Guy", 60000)
with engine.begin() as cn:  # plan ran out 5 days ago
    cn.execute(text("UPDATE memberships SET start_date=:s, end_date=:e WHERE id=:id"),
               {"s": date.today() - timedelta(days=35), "e": date.today() - timedelta(days=5), "id": x_ms})

print("\n== check-ins")
expect("first in", checkin(ok_m)["result"], "success")
expect("expired plan is refused", checkin(x_m)["result"], "expired")
expect("tries again", checkin(x_m)["result"], "expired")
expect("second in", checkin(late_m)["result"], "success")

f = chk("feed, arrival order", c.get(f"{B}/gyms/{G}/checkins", headers=H(T), params={"order": "earliest"}))
expect("order: first in at the top", [i["member"]["name"] for i in f["items"]], ["Early Bird", "Expired Guy", "Expired Guy", "Late Comer"])
expect("first in today is flagged once", [i["member"]["name"] for i in f["items"] if i["first_in_today"]], ["Early Bird"])
expect("both are first-ever visits", sorted(i["member"]["name"] for i in f["items"] if i["first_visit"]), ["Early Bird", "Late Comer"])
expect("counts", f["counts"], {"success": 2, "denied": 2, "plan_denied": 2})
x = [i for i in f["items"] if i["result"] == "expired"][0]
expect("refusal says how long ago the plan ended", (x["days_since_expiry"], x["plan_name"]), (5, "Monthly"))
pf = chk("feed, plan refusals only", c.get(f"{B}/gyms/{G}/checkins", headers=H(T), params={"result": "plan"}))
expect("only the expired ones", {i["result"] for i in pf["items"]}, {"expired"})

print("\n== who came in on an expired plan")
d = chk("denied report", c.get(f"{B}/gyms/{G}/checkins/denied", headers=H(T), params={"range": "today"}))
expect("one person, two tries", [(i["member"]["name"], i["attempts"], i["last_result"], i["renewed"]) for i in d["items"]],
       [("Expired Guy", 2, "expired", False)])
expect("plan, days ago, dues", (d["items"][0]["plan_end_date"], d["items"][0]["days_since_expiry"], d["items"][0]["due_paise"]),
       ((date.today() - timedelta(days=5)).isoformat(), 5, 40000))
expect("totals", (d["totals"]["members"], d["totals"]["attempts"], d["totals"]["not_renewed"]), (1, 2, 1))
s = chk("summary", c.get(f"{B}/gyms/{G}/summary", headers=H(T)))
expect("summary numbers", (s["numbers"]["plan_denied_today"], s["numbers"]["expired_tried_7d"]), (1, 1))
expect("first under Needs attention", (s["insights"][0]["key"], s["insights"][0]["count"]), ("expired_checkins", 1))
expect("first in today on the summary", s["first_in_today"]["name"], "Early Bird")

print("\n== renewing takes them off the alert")
chk("renew", c.post(f"{B}/gyms/{G}/members/{x_m}/memberships", headers=H(T, True), json={"plan_id": P}))
d = c.get(f"{B}/gyms/{G}/checkins/denied", headers=H(T), params={"range": "today"}).json()
expect("still listed, marked renewed", [(i["member"]["name"], i["renewed"]) for i in d["items"]], [("Expired Guy", True)])
s = c.get(f"{B}/gyms/{G}/summary", headers=H(T)).json()
expect("alert gone", [i["key"] for i in s["insights"] if i["key"] == "expired_checkins"], [])

print("\n== tiles open as lists")
nm = c.get(f"{B}/gyms/{G}/members", headers=H(T), params={"status": "new_month"}).json()
expect("new this month = summary number", nm["filtered_total"], s["numbers"]["new_members_month"])
rm = c.get(f"{B}/gyms/{G}/members", headers=H(T), params={"status": "renewed_month"}).json()
expect("renewed this month lists the renewal", [i["name"] for i in rm["items"]], ["Expired Guy"])

print("\n== console")
ADMIN = rnd()
A = login(ADMIN)
with engine.begin() as cn:
    cn.execute(text("UPDATE users SET is_platform_admin=1 WHERE phone LIKE :p"), {"p": f"%{ADMIN}"})
g = chk("console gyms", c.get(f"{B}/internal/analytics/gyms", headers=H(A, app="web"), params={"q": G[:0] + s["gym"]["name"], "range": "7d"}))
row = g["items"][0]
expect("per-gym: tried on a plan that ran out, sold", (row["plan_denied"], row["plan_denied_today"], row["sold"]), (1, 1, 4))
ov = chk("console overview", c.get(f"{B}/internal/overview", headers=H(A, app="web"), params={"range": "7d"}))
expect("overview has the platform number", ov["checkins"]["plan_denied_today"] >= 1, True)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
