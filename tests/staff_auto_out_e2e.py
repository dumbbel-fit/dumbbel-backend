"""Staff who forget to scan out: a shift still open 8 hours after it started ends by itself, at exactly 8 h.

  * shift open 9 h -> ended at start + 8 h, marked auto_out, 480 minutes, noted in the activity log
  * shift open 3 h -> left alone (still on shift)
  * after the auto end, the next scan starts a fresh shift (not "end shift?")
  * the every-10-minutes job does the same for all gyms
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/staff_auto_out_e2e.py"""
import os
import random
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
from app.services.scheduled import auto_end_shifts  # noqa: E402

B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
H = lambda t: {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


def login(phone):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


T = login(rnd())
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Auto Out Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active', lat=17.4483, lng=78.3915 WHERE id=:g"), {"g": G})
QR = c.get(f"{B}/gyms/{G}/checkin-qr", headers=H(T)).json()["qr"]["url"]
SC = f"{B}/gyms/{G}/staff/me/scan"
staff = {}
for who in ("vijay", "priya"):
    ph = rnd()
    c.post(f"{B}/gyms/{G}/staff", headers=H(T), json={"name": who.title(), "phone": ph, "staff_role": "trainer"})
    staff[who] = login(ph)
    r = c.post(SC, headers=H(staff[who]), json={"code": QR})
    expect(f"{who} starts a shift", (r.status_code, r.json().get("action")), (200, "in"))
with engine.begin() as cn:   # vijay clocked in 9 h ago and never scanned out; priya 3 h ago
    cn.execute(text("UPDATE staff_shifts s JOIN users u ON u.id=s.user_id SET s.in_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 9 HOUR) "
                    "WHERE s.gym_id=:g AND u.name='Vijay'"), {"g": G})
    cn.execute(text("UPDATE staff_shifts s JOIN users u ON u.id=s.user_id SET s.in_at = DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 3 HOUR) "
                    "WHERE s.gym_id=:g AND u.name='Priya'"), {"g": G})

auto_end_shifts()  # the every-10-minutes job
with engine.connect() as cn:
    v = cn.execute(text("SELECT TIMESTAMPDIFF(MINUTE, s.in_at, s.out_at), s.auto_out FROM staff_shifts s JOIN users u ON u.id=s.user_id "
                        "WHERE s.gym_id=:g AND u.name='Vijay'"), {"g": G}).one()
    p = cn.execute(text("SELECT s.out_at, s.auto_out FROM staff_shifts s JOIN users u ON u.id=s.user_id "
                        "WHERE s.gym_id=:g AND u.name='Priya'"), {"g": G}).one()
    log = cn.execute(text("SELECT summary FROM audit_log WHERE gym_id=:g AND action='staff.auto_check_out'"), {"g": G}).scalars().all()
expect("9 h shift ended at exactly 8 h, marked automatic", (v[0], bool(v[1])), (480, True))
expect("3 h shift left alone", (p[0], bool(p[1])), (None, False))
expect("activity log says why", len(log) == 1 and "automatically after 8 h" in log[0], True)

me = c.get(f"{B}/gyms/{G}/staff/me/shift", headers=H(staff["vijay"])).json()
expect("vijay is no longer on shift", me["on_shift"], False)
sh = (me.get("today") or [{}])[0]
expect("his shift shows 8 h, ended automatically", (sh.get("minutes"), sh.get("auto_out")), (480, True))
r = c.post(SC, headers=H(staff["vijay"]), json={"code": QR})
expect("his next scan starts a fresh shift", (r.status_code, r.json().get("action")), (200, "in"))
expect("priya is still on shift", c.get(f"{B}/gyms/{G}/staff/me/shift", headers=H(staff["priya"])).json()["on_shift"], True)
w = c.get(f"{B}/gyms/{G}/staff/work", headers=H(T), params={"range": "today"})
expect("owner's staff page loads", w.status_code, 200)

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
