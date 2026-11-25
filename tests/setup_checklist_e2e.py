"""Setup checklist ticks itself as the gym is set up; poster printed / hide / show.
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/setup_checklist_e2e.py"""
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
H = lambda t, i=False: {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0", **({"Idempotency-Key": str(uuid.uuid4())} if i else {})}  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Setup Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
st = lambda: c.get(f"{B}/gyms/{G}/setup", headers=H(T)).json()  # noqa: E731
done = lambda d: [x["key"] for x in d["steps"] if x["done"]]  # noqa: E731
d = st()
expect("new gym: nothing done", (done(d), d["complete"], d["required_total"]), ([], False, 5))  # gym location is optional (members never need it)
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json()
m = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "First Member", "phone": rnd(), "membership": {"plan_id": P.get("plan", P)["id"]}})
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET lat=17.4, lng=78.4 WHERE id=:g"), {"g": G})
c.post(f"{B}/gyms/{G}/setup/qr_poster", headers=H(T))
expect("ticks itself", done(st()), ["plans", "members", "memberships", "location", "qr"])
expect("hide", (c.post(f"{B}/gyms/{G}/setup/dismiss", headers=H(T)).status_code, st()["dismissed"]), (204, True))
expect("show again", (c.post(f"{B}/gyms/{G}/setup/show", headers=H(T)).status_code, st()["dismissed"]), (204, False))
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
