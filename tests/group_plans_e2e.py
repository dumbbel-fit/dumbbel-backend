"""Group plans: a plan for N people at a per-person price; the group joins together and pays one total.

  * plan with group_size 3 shows per-person and group total
  * enrolling 3 people (2 new + 1 existing member) gives each their own linked membership at the per-person price
  * the total paid is split equally; each person's dues are right; the UPI reference is used once
  * wrong head count, a person twice, overpaying → refused, nothing saved
  * group_size 0 turns it back into a normal plan
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/group_plans_e2e.py"""
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
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731


def H(t, idem=False):
    h = {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}
    if idem:
        h["Idempotency-Key"] = str(uuid.uuid4())
    return h


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Group Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
pl = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Trio Monthly", "duration_days": 30, "price_paise": 150000, "group_size": 3}).json()
pl = pl.get("plan", pl)
expect("group plan: per person and total", (pl["group_size"], pl["effective_price_paise"], pl["group_total_paise"]), (3, 150000, 450000))
solo = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Solo", "duration_days": 30, "price_paise": 200000, "group_size": 1}).json()
expect("size 1 is a normal plan", solo.get("group_size"), None)
old = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Existing Ravi", "phone": rnd()}).json()["member"]
a, b = rnd(), rnd()
GP = f"{B}/gyms/{G}/group-memberships"
three = [{"name": "Anil", "phone": a}, {"name": "Bala", "phone": b}, {"member_id": old["id"]}]

expect("two people for a trio plan is refused", c.post(GP, headers=H(T, True), json={"plan_id": pl["id"], "people": three[:2]}).status_code, 422)
expect("same person twice is refused", c.post(GP, headers=H(T, True), json={"plan_id": pl["id"], "people": [three[0], three[2], three[2]]}).status_code, 422)
expect("overpaying is refused", c.post(GP, headers=H(T, True), json={"plan_id": pl["id"], "people": three, "payment": {"amount_paise": 500000, "method": "cash"}}).status_code, 422)
with engine.begin() as cn:
    expect("nothing saved by the refused tries", cn.execute(text("SELECT COUNT(*) FROM memberships WHERE gym_id=:g"), {"g": G}).scalar(), 0)

res = c.post(GP, headers=H(T, True), json={"plan_id": pl["id"], "people": three, "payment": {"amount_paise": 300001, "method": "upi", "reference": "412345678901"}})
expect("group enrolled", res.status_code, 201)
d = res.json()
expect("three memberships, same group", (len(d["members"]), len({x["membership"]["group_id"] for x in d["members"]})), (3, 1))
expect("each at the per-person price", [x["membership"]["price_paise"] for x in d["members"]], [150000] * 3)
expect("total split equally", sorted(x["payment"]["amount_paise"] for x in d["members"]), [100000, 100000, 100001])
expect("UPI reference used once", [x["payment"]["reference"] for x in d["members"]].count("412345678901"), 1)
expect("group dues", d["due_paise"], 149999)
expect("existing member reused", d["members"][2]["member"]["id"], old["id"])
det = c.get(f"{B}/gyms/{G}/members/{old['id']}", headers=H(T)).json()
ms = (det.get("memberships") or det.get("member", {}).get("memberships") or [])
ms = ms[0] if ms else {}
expect("member page shows who they joined with", sorted(x["name"] for x in ms.get("group", [])), ["Anil", "Bala"])
lst = c.get(f"{B}/gyms/{G}/members", headers=H(T)).json()
expect("new people are members", sorted(i["name"] for i in lst["items"]), ["Anil", "Bala", "Existing Ravi"])
p2 = c.patch(f"{B}/gyms/{G}/plans/{pl['id']}", headers=H(T), json={"group_size": 0}).json()
expect("back to a normal plan", p2.get("group_size"), None)
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
