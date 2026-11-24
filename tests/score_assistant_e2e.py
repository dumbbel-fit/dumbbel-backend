"""Gym performance score and Ask DUMBBEL.

  * score: five parts, overall 0–100, grade; parts without enough data are left out
  * a gym that earned more last month: the assistant explains the fall using the gym's own numbers
  * how-to questions answered from the guide; feedback saved; questions visible to the platform team
  * trainers (no money permission) can't see the score or ask; nothing about members' names goes to the model
Run against a TEST database with the API on :8000 (NVIDIA_API_KEY set):  python tests/score_assistant_e2e.py"""
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
c = httpx.Client(timeout=120)
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
H = lambda t, i=False: {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0", **({"Idempotency-Key": str(uuid.uuid4())} if i else {})}  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


def login(phone):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


T = login(rnd())
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Score Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
P = (lambda x: x.get("plan", x)["id"])(c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 150000}).json())
today = date.today()
names = ["Ravi Kumar", "Sneha Reddy", "Arjun", "Kiran", "Meghana", "Suresh", "Priya", "Mahesh", "Divya", "Rahul", "Anil", "Kavya"]
ids = []
for i, n in enumerate(names):  # 12 members, each with a plan that started 40 days ago, paid last month
    r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": n, "phone": rnd(),
               "membership": {"plan_id": P, "start_date": (today - timedelta(days=40)).isoformat()},
               "payment": {"amount_paise": 150000, "method": "cash"}}).json()
    ids.append((r["member"]["id"], r["membership"]["id"]))
with engine.begin() as cn:   # make it history: joined and paid last month
    for mid, msid in ids:
        cn.execute(text("UPDATE gym_members SET created_at = DATE_SUB(NOW(), INTERVAL 40 DAY) WHERE id=:m"), {"m": mid})
        cn.execute(text("UPDATE memberships SET created_at = DATE_SUB(NOW(), INTERVAL 40 DAY) WHERE id=:m"), {"m": msid})
        cn.execute(text("UPDATE payments SET paid_at = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 33 DAY), created_at = DATE_SUB(NOW(), INTERVAL 40 DAY) WHERE membership_id=:m"), {"m": msid})
# only 3 renew this month (start after the old plan ends), paid this month
for mid, _ in ids[:3]:
    c.post(f"{B}/gyms/{G}/members/{mid}/memberships", headers=H(T, True), json={"plan_id": P, "start_date": (today - timedelta(days=10)).isoformat(),
           "replace": True, "payment": {"amount_paise": 150000, "method": "upi", "reference": str(random.randint(10**11, 10**12 - 1))}})
# check-ins: the 3 renewed members came in each of the last 4 weeks
with engine.begin() as cn:
    for mid, _ in ids[:3]:
        for d in (2, 9, 16, 23):
            cn.execute(text("INSERT INTO checkins (id, gym_id, member_id, at, local_date, method, result) VALUES (UUID(), :g, :m, NOW(), :d, 'manual', 'success')"),
                       {"g": G, "m": mid, "d": today - timedelta(days=d)})

s = c.get(f"{B}/gyms/{G}/score", headers=H(T)).json()
expect("score has five parts", [p["key"] for p in s["parts"]], ["retention", "renewal", "attendance", "collection", "acquisition"])
expect("overall is 0–100 with a grade", (isinstance(s["score"], int) and 0 <= s["score"] <= 100, bool(s["grade"])), (True, True))
print("   score", s["score"], s["grade"], {p["key"]: p["score"] for p in s["parts"]})

info = c.get(f"{B}/gyms/{G}/assistant", headers=H(T)).json()
expect("assistant is on", info["enabled"], True)
r = c.post(f"{B}/gyms/{G}/assistant", headers=H(T), json={"question": "Why did my revenue decrease this month?"})
a = r.json()
expect("revenue question answered", r.status_code, 200)
ans = a.get("answer", "")
print("   ANSWER (%s ms):" % a.get("ms"), ans.replace("\n", " ")[:700])
expect("answer uses the gym's own numbers (renewals / visits / rupees)", any(k in ans for k in ("renew", "₹", "visit")), True)
expect("no member names leak into the answer", any(n.split()[0] in ans for n in names), False)
r = c.post(f"{B}/gyms/{G}/assistant", headers=H(T), json={"question": "How do I add a couple on one plan?",
           "history": [{"role": "user", "content": "Why did my revenue decrease this month?"}, {"role": "assistant", "content": ans}]})
how = r.json().get("answer", "")
print("   HOW-TO:", how.replace("\n", " ")[:400])
expect("how-to answered from the guide (Custom)", "Custom" in how or "custom" in how, True)
expect("feedback saved", c.post(f"{B}/gyms/{G}/assistant/{a['id']}/feedback", headers=H(T), json={"helpful": True}).status_code, 204)

tp = rnd()
c.post(f"{B}/gyms/{G}/staff", headers=H(T), json={"name": "Trainer T", "phone": tp, "staff_role": "trainer"})
TT = login(tp)
expect("trainer can't see the score", c.get(f"{B}/gyms/{G}/score", headers=H(TT)).status_code, 403)
expect("trainer can't ask", c.post(f"{B}/gyms/{G}/assistant", headers=H(TT), json={"question": "revenue?"}).status_code, 403)

with engine.begin() as cn:
    cn.execute(text("UPDATE users SET is_platform_admin=1 WHERE id=(SELECT owner_user_id FROM gyms WHERE id=:g)"), {"g": G})
sc = c.get(f"{B}/internal/insights/scores", headers=H(T)).json()
expect("platform team sees every gym's score", any(g["gym_id"] == G for g in sc["gyms"]), True)
qs = c.get(f"{B}/internal/insights/assistant-questions", headers=H(T)).json()
mine = [i for i in qs["items"] if i["gym"].startswith("Score Gym")]
expect("platform team sees what gyms asked", len(mine) >= 2, True)
expect("feedback shows up", any(i["helpful"] is True for i in mine), True)
with engine.begin() as cn:
    cn.execute(text("UPDATE users SET is_platform_admin=0 WHERE id=(SELECT owner_user_id FROM gyms WHERE id=:g)"), {"g": G})

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
