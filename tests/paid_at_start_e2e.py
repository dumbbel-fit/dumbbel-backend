"""Payment taken with a plan gets the plan's start date when the plan started earlier; the old-records backfill.

  * add member with a back-dated start + payment -> payment dated the start date (time of day kept)
  * plan starting today, or in the future (renew ahead) -> payment dated now
  * a date typed by the desk still wins; a due paid later on its own keeps its own date
  * backfill: preview changes nothing; apply moves only plan+payment saved together; undo puts dates back
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/paid_at_start_e2e.py"""
import os
import random
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
H = lambda t, i=False: {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0", **({"Idempotency-Key": str(uuid.uuid4())} if i else {})}  # noqa: E731
IST = timezone(timedelta(hours=5, minutes=30))


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Paid Date Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
MON = (lambda x: x.get("plan", x)["id"])(c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json())
today = datetime.now(IST).date()
day = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(IST).date()  # noqa: E731


def add(start, **pay):
    r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "M " + rnd()[-4:], "phone": rnd(),
               "membership": {"plan_id": MON, "start_date": start.isoformat()},
               "payment": {"amount_paise": 50000, "method": "cash", **pay}})
    assert r.status_code == 201, r.text
    j = r.json()
    return j["member"]["id"], j["membership"]["id"], j["payment"]


print("== new payments")
m1, ms1, p1 = add(today - timedelta(days=12))
expect("back-dated start: payment on the start date", day(p1["paid_at"]), today - timedelta(days=12))
expect("time of day kept (within a minute of now)", abs((datetime.fromisoformat(p1["paid_at"].replace("Z", "+00:00")) + timedelta(days=12)
                                                          - datetime.now(timezone.utc)).total_seconds()) < 60, True)
_, _, p2 = add(today)
expect("starts today: payment today", day(p2["paid_at"]), today)
typed = (datetime.now(IST) - timedelta(days=3)).replace(microsecond=0).isoformat()
_, _, p3 = add(today - timedelta(days=12), paid_at=typed)
expect("date typed by the desk wins", day(p3["paid_at"]), today - timedelta(days=3))
_, _, p4 = add(today - timedelta(days=200))
expect("start older than 30 days is still fine", day(p4["paid_at"]), today - timedelta(days=200))
r = c.post(f"{B}/gyms/{G}/members/{m1}/memberships", headers=H(T, True), json={"plan_id": MON, "payment": {"amount_paise": 100000, "method": "cash"}}).json()
expect("renewal starting in the future: payment today", day(r["payment"]["paid_at"]), today)
r = c.post(f"{B}/gyms/{G}/payments", headers=H(T, True), json={"member_id": m1, "membership_id": ms1, "amount_paise": 50000, "method": "cash", "confirm_duplicate": True}).json()
expect("due paid later on its own: today", day(r["payment"]["paid_at"]), today)
LATER = r["payment"]["id"]

print("== old records")
m5, ms5, p5 = add(today - timedelta(days=20))
with engine.begin() as cn:   # make it look like before the fix: paid "today" although the plan began 20 days ago
    cn.execute(text("UPDATE payments SET paid_at = UTC_TIMESTAMP(6) WHERE id = :i"), {"i": p5["id"]})
    cn.execute(text("UPDATE payments SET paid_at = UTC_TIMESTAMP(6) WHERE id = :i"), {"i": p1["id"]})
pa = lambda i: engine.connect().execute(text("SELECT paid_at FROM payments WHERE id=:i"), {"i": i}).scalar()  # noqa: E731
before = {i: pa(i) for i in (p5["id"], p1["id"], LATER, p2["id"])}
run = lambda *a: subprocess.run([sys.executable, "scripts/backfill_paid_at_start.py", *a], cwd=ROOT, capture_output=True, text=True)  # noqa: E731
out = run()
expect("preview runs", out.returncode, 0)
expect("preview lists the old record", p5["id"] and "Paid Date Gym" in out.stdout, True)
expect("preview changes nothing", pa(p5["id"]), before[p5["id"]])
out = run("--apply")
expect("apply runs", out.returncode, 0)
nd = lambda i: pa(i).replace(tzinfo=timezone.utc).astimezone(IST).date()  # noqa: E731
expect("old record moved to plan start", nd(p5["id"]), today - timedelta(days=20))
expect("time of day kept", pa(p5["id"]).time(), before[p5["id"]].time())
expect("other old record moved too", nd(p1["id"]), today - timedelta(days=12))
expect("due paid later untouched", pa(LATER), before[LATER])
expect("plan starting today untouched", pa(p2["id"]), before[p2["id"]])
expect("apply again: nothing more", "Total: 0" in run("--apply").stdout, True)
expect("undo runs", run("--undo").returncode, 0)
expect("undo puts the date back", pa(p5["id"]), before[p5["id"]])

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
