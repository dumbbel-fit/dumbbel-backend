"""“Not a member” for a real member, and deleting a member permanently.

  * a member who once tapped “This isn't mine” gets told so at check-in, with a way to undo it ("Yes, this is me")
  * the owner can clear that flag too
  * delete permanently: owner only, name typed to confirm; memberships, payments, check-ins, notes all go;
    the same phone can be added again as a brand-new member; the other members are untouched
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/dispute_delete_e2e.py"""
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
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Dispute Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active', lat=17.448300, lng=78.391500 WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 150000}).json()
P = P.get("plan", P)["id"]
QR = c.get(f"{B}/gyms/{G}/checkin-qr", headers=H(T)).json()["qr"]["url"]
at_gym = {"lat": 17.4484, "lng": 78.3916, "accuracy_m": 20}

print("== “This isn't mine” by mistake")
ap = rnd()
r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Aravind Test", "phone": ap, "membership": {"plan_id": P},
                                                               "payment": {"amount_paise": 150000, "method": "cash"}}).json()
AID = r["member"]["id"]
M = login(ap)
expect("member taps “This isn't mine”", c.post(f"{B}/me/claims/{AID}/dispute", headers=H(M, app="member"), json={}).status_code, 204)
r = c.post(f"{B}/me/checkin", headers=H(M, app="member"), json={"code": QR, **at_gym}).json()
expect("check-in says why, with a way to undo", (r["result"], r.get("disputed"), r.get("disputed_member_id")), ("not_member", True, AID))
expect("“Yes, this is me”", c.post(f"{B}/me/claims/{AID}/confirm", headers=H(M, app="member")).status_code, 204)
r = c.post(f"{B}/me/checkin", headers=H(M, app="member"), json={"code": QR, **at_gym}).json()
expect("then check-in works", r["result"], "success")
expect("someone else can't confirm it", c.post(f"{B}/me/claims/{AID}/confirm", headers=H(login(rnd()), app="member")).status_code, 404)
c.post(f"{B}/me/claims/{AID}/dispute", headers=H(M, app="member"), json={})
expect("owner clears the flag", c.post(f"{B}/gyms/{G}/members/{AID}/clear-dispute", headers=H(T)).status_code, 204)
d = c.get(f"{B}/gyms/{G}/members/{AID}", headers=H(T)).json()
expect("flag gone", (d.get("member") or d).get("claim_disputed_at"), None)

print("== delete permanently")
other = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Other Member", "phone": rnd(), "membership": {"plan_id": P},
                                                                   "payment": {"amount_paise": 150000, "method": "cash"}}).json()["member"]["id"]
c.post(f"{B}/gyms/{G}/members/{AID}/notes", headers=H(T), json={"body": "test note"})
c.post(f"{B}/gyms/{G}/checkins/manual", headers=H(T, True), json={"member_id": AID})
sp = rnd()
c.post(f"{B}/gyms/{G}/staff", headers=H(T), json={"name": "Desk Staff", "phone": sp, "staff_role": "receptionist"})
S = login(sp)
expect("staff can't delete", c.delete(f"{B}/gyms/{G}/members/{AID}", headers=H(S), params={"confirm": "Aravind Test"}).status_code, 403)
expect("wrong name refused", c.delete(f"{B}/gyms/{G}/members/{AID}", headers=H(T), params={"confirm": "Aravind"}).json()["error"]["code"], "CONFIRM_MISMATCH")
r = c.delete(f"{B}/gyms/{G}/members/{AID}", headers=H(T), params={"confirm": "aravind  test"})
expect("owner deletes", (r.status_code, r.json().get("deleted"), r.json().get("payments")), (200, True, 1))
expect("member gone", c.get(f"{B}/gyms/{G}/members/{AID}", headers=H(T)).status_code, 404)
with engine.begin() as cn:
    left = {t: cn.execute(text(f"SELECT COUNT(*) FROM {t} WHERE member_id=:m"), {"m": AID}).scalar()
            for t in ("memberships", "payments", "checkins", "member_notes")}
expect("nothing of theirs left", left, {"memberships": 0, "payments": 0, "checkins": 0, "member_notes": 0})
r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Aravind Again", "phone": ap})
expect("same phone can be added again as new", r.status_code, 201)
r = c.post(f"{B}/me/checkin", headers=H(M, app="member"), json={"code": QR, **at_gym}).json()
expect("their app account still works (new member, no plan)", r["result"] in ("expired", "not_started", "cancelled", "no_plan", "not_member", "success"), True)
d = c.get(f"{B}/gyms/{G}/members/{other}", headers=H(T)).json()
expect("other member untouched", len(d.get("memberships") or (d.get("member") or {}).get("memberships") or []), 1)
log = c.get(f"{B}/gyms/{G}/audit", headers=H(T), params={"action": "member.delete"})
if log.status_code == 200:
    expect("deletion is in the activity log", any(x.get("action") == "member.delete" for x in log.json().get("items", [])), True)

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
