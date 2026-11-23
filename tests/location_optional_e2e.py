"""Location is optional by default: members and staff check in with the QR alone; owners can still switch the check on.

  * new gym: QR settings say location is off
  * member scans with no location -> checked in; far away -> still checked in, distance kept
  * staff scans with no location -> shift started (place: not shared)
  * owner switches it on -> no location / far away refused as before; switched off again -> works
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/location_optional_e2e.py"""
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
H = lambda t, i=False, app="admin": {"Authorization": f"Bearer {t}", "X-App": app, "X-App-Version": "1.0.0", **({"Idempotency-Key": str(uuid.uuid4())} if i else {})}  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


def login(phone):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


T = login(rnd())
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Loc Off Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active', lat=17.448300, lng=78.391500 WHERE id=:g"), {"g": G})
P = (lambda x: x.get("plan", x)["id"])(c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json())
qr = c.get(f"{B}/gyms/{G}/checkin-qr", headers=H(T)).json()
QR = qr["qr"]["url"]
expect("new gym: location check is off", qr["settings"]["require_location"], False)

mp, mp2 = rnd(), rnd()
for ph in (mp, mp2):
    c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Member " + ph[-3:], "phone": ph,
           "membership": {"plan_id": P, "start_date": date.today().isoformat()}})
M, M2 = login(mp), login(mp2)
MC = f"{B}/me/checkin"
r = c.post(MC, headers=H(M, app="member"), json={"code": QR}).json()
expect("member with location off: checked in", r["result"], "success")
r = c.post(MC, headers=H(M2, app="member"), json={"code": QR, "lat": 17.5, "lng": 78.5, "accuracy_m": 30}).json()
expect("member far away: still checked in", r["result"], "success")
expect("distance kept when shared", (r.get("distance_m") or 0) > 1000, True)

sp = rnd()
c.post(f"{B}/gyms/{G}/staff", headers=H(T), json={"name": "Desk " + sp[-3:], "phone": sp, "staff_role": "receptionist"})
S = login(sp)
SC = f"{B}/gyms/{G}/staff/me/scan"
r = c.post(SC, headers=H(S), json={"code": QR})
expect("staff with location off: shift started", (r.status_code, r.json().get("action"), r.json().get("shift", {}).get("in", {}).get("place")),
       (200, "in", "not_shared"))
expect("fake QR still refused", c.post(SC, headers=H(S), json={"code": QR[:-4] + "abcd"}).json()["error"]["code"], "WRONG_QR")

print("== owner switches the check on")
c.patch(f"{B}/gyms/{G}", headers=H(T), json={"settings": {"checkin_require_location": True}})
expect("QR settings say on", c.get(f"{B}/gyms/{G}/checkin-qr", headers=H(T)).json()["settings"]["require_location"], True)
with engine.begin() as cn:   # let them scan again today
    cn.execute(text("DELETE FROM checkins WHERE gym_id=:g"), {"g": G})
r = c.post(MC, headers=H(M, app="member"), json={"code": QR}).json()
expect("member without location refused", r["result"], "location_required")
r = c.post(MC, headers=H(M, app="member"), json={"code": QR, "lat": 17.5, "lng": 78.5}).json()
expect("member far away refused", r["result"], "too_far")
r = c.post(MC, headers=H(M, app="member"), json={"code": QR, "lat": 17.4484, "lng": 78.3916, "accuracy_m": 20}).json()
expect("member at the gym checked in", r["result"], "success")
sp2 = rnd()
c.post(f"{B}/gyms/{G}/staff", headers=H(T), json={"name": "Trainer " + sp2[-3:], "phone": sp2, "staff_role": "trainer"})
S2 = login(sp2)
expect("staff without location refused", c.post(SC, headers=H(S2), json={"code": QR}).json()["error"]["code"], "LOCATION_REQUIRED")
c.patch(f"{B}/gyms/{G}", headers=H(T), json={"settings": {"checkin_require_location": False}})
expect("switched off again: staff marked", c.post(SC, headers=H(S2), json={"code": QR}).status_code, 200)

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
