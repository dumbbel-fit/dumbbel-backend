"""Console gym page: every list a gym has, record by record (read only), plus CSV.
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/console_gym_raw_e2e.py"""
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


def H(t, idem=False, app="admin"):
    h = {"Authorization": f"Bearer {t}", "X-App": app, "X-App-Version": "1.0.0"}
    if idem:
        h["Idempotency-Key"] = str(uuid.uuid4())
    return h


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


def login(phone):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


T = login(rnd())
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Raw Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json()
P = P.get("plan", P)["id"]
m = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Ravi Raw", "phone": rnd(), "membership": {"plan_id": P},
                                                            "payment": {"amount_paise": 60000, "method": "cash"}}).json()["member"]
c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Sita Raw", "phone": rnd()})
c.post(f"{B}/gyms/{G}/checkins/manual", headers=H(T, True), json={"member_id": m["id"]})

ADMIN = rnd()
A = login(ADMIN)
with engine.begin() as cn:
    cn.execute(text("UPDATE users SET is_platform_admin=1 WHERE phone LIKE :p"), {"p": f"%{ADMIN}"})
R = lambda k, **p: c.get(f"{B}/internal/gyms/{G}/raw/{k}", headers=H(A, app="web"), params=p)  # noqa: E731
for k in ["members", "memberships", "payments", "checkins", "upi", "whatsapp", "plans", "staff", "activity"]:
    r = R(k)
    expect(f"{k} loads", r.status_code, 200)
d = R("members").json()
expect("both members listed", (d["total"], sorted(i["name"] for i in d["items"])), (2, ["Ravi Raw", "Sita Raw"]))
ravi = [i for i in d["items"] if i["name"] == "Ravi Raw"][0]
expect("member row has plan, dues, visits", (ravi["plan"], ravi["due_paise"], ravi["paid_all_paise"], ravi["visits"]), ("Monthly", 40000, 60000, 1))
expect("search", [i["name"] for i in R("members", q="Sita").json()["items"]], ["Sita Raw"])
expect("paging", (len(R("members", limit=1).json()["items"]), R("members", limit=1).json()["next_offset"]), (1, 1))
expect("payment row", [(i["name"], i["amount_paise"], i["method"]) for i in R("payments").json()["items"]], [("Ravi Raw", 60000, "cash")])
expect("check-in row", [(i["name"], i["result"]) for i in R("checkins").json()["items"]], [("Ravi Raw", "success")])
csv_r = c.get(f"{B}/internal/gyms/{G}/raw-csv/payments", headers=H(A, app="web"))
expect("csv", (csv_r.status_code, "600.00" in csv_r.text), (200, True))
expect("owner can't open the console", R("members").status_code if False else c.get(f"{B}/internal/gyms/{G}/raw/members", headers=H(T, app="web")).status_code, 403)
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
