"""Test gyms: the console can mark a gym as a test gym; platform numbers then leave it out, nothing about the gym
changes, and unmarking brings it back. Also calls every console endpoint so a query the test-gym filter can't
handle shows up as a 500.

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/test_gyms_e2e.py"""
import os
import random
import sys
import uuid

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


def gym(name, members, paid_paise):
    T = login(rnd())
    G = chk(f"create {name}", c.post(f"{B}/gyms", headers=H(T), json={"name": name, "city": "Testcity" + SUFFIX}))["id"]
    with engine.begin() as cn:
        cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
    P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json()
    P = P.get("plan", P)["id"]
    for i in range(members):
        chk(f"{name}: member", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={
            "name": f"M{i}", "phone": rnd(), "membership": {"plan_id": P},
            "payment": {"amount_paise": paid_paise, "method": "cash"}}), 201)
    return T, G


SUFFIX = str(random.randint(1000, 9999))
ADMIN = rnd()
A = login(ADMIN, "web")
with engine.begin() as cn:
    cn.execute(text("UPDATE users SET is_platform_admin=1 WHERE phone LIKE :p"), {"p": f"%{ADMIN}"})
AH = H(A, app="web")

T_real, REAL = gym("Real Gym " + SUFFIX, 3, 100000)
T_test, TEST = gym("Test Gym " + SUFFIX, 2, 50000)


def gym_snapshot(g):
    with engine.connect() as cn:
        return {t: cn.execute(text(f"SELECT COUNT(*) FROM {t} WHERE gym_id=:g"), {"g": g}).scalar()
                for t in ("gym_members", "memberships", "payments", "plans", "gym_staff", "audit_log")}


def overview():
    return chk("overview", c.get(f"{B}/internal/overview", headers=AH, params={"range": "30d"}))


def city_row():
    cs = chk("cities", c.get(f"{B}/internal/analytics/cities", headers=AH, params={"range": "30d"}))
    return next((x for x in cs["items"] if x["city"] == ("Testcity" + SUFFIX).title()), None)


before = overview()
snap = gym_snapshot(TEST)
expect("city shows both gyms before marking", (city_row() or {}).get("gyms"), 2)

print("\n== mark as test")
g = chk("mark test", c.post(f"{B}/internal/gyms/{TEST}/test", headers=AH, json={"is_test": True}))
expect("flag set", g["is_test"], True)
chk("only platform admins", c.post(f"{B}/internal/gyms/{TEST}/test", headers=H(T_test), json={"is_test": False}), 403)
after = overview()
expect("overview: one gym fewer", after["gyms"]["total"], before["gyms"]["total"] - 1)
expect("overview: its 2 members left out", after["members"]["total"], before["members"]["total"] - 2)
expect("overview: its money left out", after["money"]["gmv_paise"], before["money"]["gmv_paise"] - 100000)
expect("overview: its dues left out", after["money"]["dues_outstanding_paise"], before["money"]["dues_outstanding_paise"] - 100000)
expect("city: only the real gym", (city_row() or {}).get("gyms"), 1)

live = chk("gyms: live tab", c.get(f"{B}/internal/analytics/gyms", headers=AH, params={"q": SUFFIX}))
expect("live tab: real gym only", [x["id"] for x in live["items"]], [REAL])
expect("live tab totals", (live["totals"]["members"], live["totals"]["collected_all"], live["totals"]["dues_paise"]), (3, 300000, 0))
expect("tab counts", live["test_counts"], {"live": 1, "test": 1, "all": 2})
tst = chk("gyms: test tab", c.get(f"{B}/internal/analytics/gyms", headers=AH, params={"q": SUFFIX, "test": "test"}))
expect("test tab: test gym only", [(x["id"], x["is_test"]) for x in tst["items"]], [(TEST, True)])
expect("test gym row: members, money, dues", (tst["items"][0]["members"], tst["items"][0]["collected_all"], tst["items"][0]["dues_paise"]),
       (2, 100000, 100000))
dd = chk("deep-dive still works for a test gym", c.get(f"{B}/internal/analytics/gyms/{TEST}", headers=AH))
expect("deep-dive shows its own data", (dd["gym"]["members"], dd["gym"]["is_test"], dd["dues_outstanding_paise"]), (2, True, 100000))
fc = chk("forecast for the test gym", c.get(f"{B}/internal/insights/forecast/{TEST}", headers=AH))
expect("nothing about the gym changed", gym_snapshot(TEST), {**snap, "audit_log": snap["audit_log"] + 1})
s = chk("the gym's own dashboard still works", c.get(f"{B}/gyms/{TEST}/summary", headers=H(T_test)))
expect("its own dashboard keeps its numbers", s["numbers"]["total_members"], 2)
d = chk("gym detail says test", c.get(f"{B}/internal/gyms/{TEST}", headers=AH))
expect("detail is_test", d["is_test"], True)

print("\n== every console endpoint answers")
for path, params in [("/internal/overview", {"range": r}) for r in ("7d", "30d", "90d", "365d")] + [
        ("/internal/attention", {}), ("/internal/analytics/gyms", {"test": "all"}), ("/internal/analytics/gyms.csv", {}),
        ("/internal/analytics/cities", {}), ("/internal/users", {"q": SUFFIX[:3] + "9"}), ("/internal/activity", {}),
        ("/internal/system", {}), ("/internal/ai/summary", {}), ("/internal/gyms", {}),
        ("/internal/insights/actions", {"refresh": True}), ("/internal/insights/forecast", {"refresh": True}),
        ("/internal/insights/cohorts", {"refresh": True}), ("/internal/insights/cohorts", {"kind": "gyms", "refresh": True}),
        ("/internal/insights/cohorts", {"gym_id": TEST, "refresh": True}),
        ("/internal/insights/adoption", {"refresh": True}), ("/internal/insights/benchmarks", {"refresh": True}),
        ("/internal/insights/impact", {"refresh": True}), ("/internal/insights/demand", {"refresh": True}),
        ("/internal/insights/headline", {}), ("/internal/insights/revenue", {"refresh": True}),
        ("/internal/insights/capacity", {})]:
    chk(f"GET {path} {params}", c.get(f"{B}{path}", headers=AH, params=params))
bench = c.get(f"{B}/internal/insights/benchmarks", headers=AH).json()
expect("benchmarks skip the test gym", TEST in str(bench), False)
adopt = c.get(f"{B}/internal/insights/adoption", headers=AH).json()
expect("adoption skips the test gym", TEST in str(adopt), False)
csv_text = c.get(f"{B}/internal/analytics/gyms.csv", headers=AH).text
expect("CSV marks the test gym", any(TEST[:0] + "Test Gym " + SUFFIX in line and line.rstrip().endswith("yes") for line in csv_text.splitlines()), True)

print("\n== unmark")
chk("unmark", c.post(f"{B}/internal/gyms/{TEST}/test", headers=AH, json={"is_test": False}))
back = overview()
expect("numbers come back", (back["gyms"]["total"], back["members"]["total"], back["money"]["gmv_paise"]),
       (before["gyms"]["total"], before["members"]["total"], before["money"]["gmv_paise"]))
expect("city has both again", (city_row() or {}).get("gyms"), 2)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
