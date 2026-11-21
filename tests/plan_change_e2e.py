"""Renew / upgrade / change plan with full control.

  * a plan that overlaps the current one is refused unless you choose Replace (upgrade / change)
  * replace from the same start: the old plan is cancelled and what was paid for it counts towards the new one
  * replace part-way: the old plan ends the day before; optional credit for unused days comes off the new price
  * credit more than what was paid, or more than the new price, is refused; nothing is saved
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/plan_change_e2e.py"""
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
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
H = lambda t, i=False: {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0", **({"Idempotency-Key": str(uuid.uuid4())} if i else {})}  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Change Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
plan = lambda n, d, p: (lambda x: x.get("plan", x)["id"])(c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": n, "duration_days": d, "price_paise": p}).json())  # noqa: E731
MON, QTR = plan("Monthly", 30, 100000), plan("Quarterly", 90, 270000)


def member(start):
    r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "M " + rnd()[-4:], "phone": rnd(), "membership": {"plan_id": MON, "start_date": start.isoformat()},
                                                               "payment": {"amount_paise": 100000, "method": "cash"}}).json()
    return r["member"]["id"], r["membership"]["id"]


def detail(mid):
    d = c.get(f"{B}/gyms/{G}/members/{mid}", headers=H(T)).json()
    return {x["plan_name"]: x for x in (d.get("memberships") or d.get("member", {}).get("memberships"))}


A = lambda mid, body: c.post(f"{B}/gyms/{G}/members/{mid}/memberships", headers=H(T, True), json=body)  # noqa: E731
today = date.today()

print("== same start: Monthly → Quarterly")
m1, ms1 = member(today - timedelta(days=10))
s0 = (today - timedelta(days=10)).isoformat()
r = A(m1, {"plan_id": QTR, "start_date": s0})
expect("overlap refused without Replace", (r.status_code, r.json()["error"]["code"]), (409, "OVERLAPPING_MEMBERSHIP"))
r = A(m1, {"plan_id": QTR, "start_date": s0, "replace": True, "discount_paise": 50000, "payment": {"amount_paise": 200000, "method": "cash"}})
expect("overpaying the difference refused", (r.status_code, r.json()["error"]["code"]), (422, "OVERPAYMENT"))
with engine.begin() as cn:
    expect("refusal changed nothing", cn.execute(text("SELECT state FROM memberships WHERE id=:i"), {"i": ms1}).scalar(), "active")
r = A(m1, {"plan_id": QTR, "start_date": s0, "replace": True, "discount_paise": 50000, "payment": {"amount_paise": 120000, "method": "upi"}})
expect("upgraded", r.status_code, 201)
d = detail(m1)
expect("Monthly cancelled, Quarterly live", (d["Monthly"]["status"], d["Quarterly"]["status"]), ("cancelled", "active"))
expect("Quarterly: ₹2,700 − ₹500 discount, ₹1,000 already paid + ₹1,200 now → nothing due", (d["Quarterly"]["price_paise"], d["Quarterly"]["due_paise"]), (220000, 0))

print("\n== part-way: Monthly started 10 days ago → Quarterly from today")
m2, ms2 = member(today - timedelta(days=10))
r = A(m2, {"plan_id": QTR, "start_date": today.isoformat(), "replace": True, "carry_paise": 150000})
expect("credit more than paid refused", (r.status_code, "carry_paise" in r.json()["error"]["details"]["fields"]), (422, True))
r = A(m2, {"plan_id": QTR, "start_date": today.isoformat(), "replace": True, "carry_paise": 66000, "payment": {"amount_paise": 204000, "method": "cash"}})
expect("changed", r.status_code, 201)
d = detail(m2)
expect("Monthly now ends yesterday", (d["Monthly"]["end_date"], d["Monthly"]["due_paise"]), ((today - timedelta(days=1)).isoformat(), 0))
expect("Quarterly: ₹2,700 − ₹660 credit, paid ₹2,040", (d["Quarterly"]["price_paise"], d["Quarterly"]["due_paise"], d["Quarterly"]["start_date"]), (204000, 0, today.isoformat()))

print("\n== normal renewal still works")
m3, _ = member(today - timedelta(days=5))
r = A(m3, {"plan_id": MON})
expect("renewal starts after the current plan", (r.status_code, r.json()["membership"]["start_date"]), (201, (today + timedelta(days=25)).isoformat()))
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
