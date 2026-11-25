"""UPI: the member pays, enters the UTR, and it's confirmed at once (gym setting, on by default).

  * renew from the member app → claim with a UTR → verified straight away: new plan, payment recorded, receipt
  * the payment waits under "Check bank" for the owner (counted in the sidebar badge)
  * a UTR can be used once across ALL gyms; obviously fake UTRs are refused
  * owner "Not received": payment voided, the renewal it made is cancelled, member told
  * owner "Received": cleared from Check bank
  * setting off: claims wait for the owner to verify, as before
Run against a TEST database with the API on :8000:  python tests/upi_auto_confirm_e2e.py"""
import os
import random
import sys
import uuid
from datetime import date

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
utr = lambda: str(random.randint(3 * 10**11, 9 * 10**11))  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


def login(phone):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


def gym(name):
    T = login(rnd())
    G = c.post(f"{B}/gyms", headers=H(T), json={"name": name + " " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
    with engine.begin() as cn:
        cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
    P = (lambda x: x.get("plan", x)["id"])(c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 150000}).json())
    assert c.post(f"{B}/gyms/{G}/upi-accounts", headers=H(T), json={"vpa": f"gym{rnd()[-5:]}@okaxis", "payee_name": "Test Gym", "account_type": "personal"}).status_code == 201
    return T, G, P


def member(T, G, P):
    ph = rnd()
    r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Member " + ph[-3:], "phone": ph,
               "membership": {"plan_id": P, "start_date": date.today().isoformat()}, "payment": {"amount_paise": 150000, "method": "cash"}}).json()
    return r["member"]["id"], login(ph)


def renew_and_claim(M, MT, P, u):
    r = c.post(f"{B}/me/renew", headers=H(MT, app="member"), json={"member_id": M, "plan_id": P}).json()
    req = r["request"]
    return req, c.post(f"{B}/me/upi-requests/{req['id']}/claim", headers=H(MT, app="member"), json={"utr": u})


def plans_of(T, G, M):
    d = c.get(f"{B}/gyms/{G}/members/{M}", headers=H(T)).json()
    return [x for x in (d.get("memberships") or d.get("member", {}).get("memberships") or [])]


T, G, P = gym("Auto UPI Gym")
M, MT = member(T, G, P)
before = len(plans_of(T, G, M))
U1 = utr()
req, r = renew_and_claim(M, MT, P, U1)
d = r.json()
expect("claim accepted", r.status_code, 200)
expect("confirmed straight away by the member's UTR", (d["status"], d["auto_confirmed"], bool(d["receipt_no"])), ("verified", True, True))
expect("renewal made: one more plan", len(plans_of(T, G, M)), before + 1)
expect("waiting under Check bank", d["to_check"], True)
lst = c.get(f"{B}/gyms/{G}/upi-requests", headers=H(T), params={"to_check": True}).json()
expect("Check bank list and count", (len(lst["items"]), lst["counts"]["to_check"], lst["auto_confirm"]), (1, 1, True))
expect("sidebar badge counts it", c.get(f"{B}/gyms/{G}/summary", headers=H(T)).json().get("numbers", {}).get("upi_to_verify"), 1)

print("== fake / reused UTRs")
T2, G2, P2 = gym("Other Gym")
M2, MT2 = member(T2, G2, P2)
_, r = renew_and_claim(M2, MT2, P2, U1)
expect("same UTR at another gym refused", (r.status_code, r.json()["error"]["code"]), (409, "DUPLICATE_REFERENCE"))
_, r = renew_and_claim(M2, MT2, P2, "111111111111")
expect("obviously fake UTR refused", r.status_code, 422)
_, r = renew_and_claim(M2, MT2, P2, "123456789012")
expect("1-2-3 sequence refused", r.status_code, 422)

print("== owner: not received")
rid = d["id"]
r = c.post(f"{B}/gyms/{G}/upi-requests/{rid}/not-received", headers=H(T), json={"reason": "No credit in bank"})
expect("undone", (r.status_code, r.json()["status"]), (200, "rejected"))
with engine.connect() as cn:
    pay = cn.execute(text("SELECT status FROM payments WHERE member_id=:m AND method='upi'"), {"m": M}).scalars().all()
    ms = cn.execute(text("SELECT state FROM memberships WHERE id=:i"), {"i": r.json()["result_membership_id"] or d["result_membership_id"]}).scalar()
expect("payment voided", pay, ["voided"])
expect("the renewal it made is cancelled", ms, "cancelled")
expect("Check bank is empty again", c.get(f"{B}/gyms/{G}/upi-requests", headers=H(T), params={"to_check": True}).json()["counts"]["to_check"], 0)

print("== owner: received")
_, r = renew_and_claim(M, MT, P, utr())
rid = r.json()["id"]
r = c.post(f"{B}/gyms/{G}/upi-requests/{rid}/received", headers=H(T))
expect("marked received", (r.status_code, r.json()["to_check"], bool(r.json()["checked_at"])), (200, False, True))

print("== setting off: owner verifies, as before")
c.patch(f"{B}/gyms/{G}", headers=H(T), json={"settings": {"upi_auto_confirm": False}})
req, r = renew_and_claim(M, MT, P, utr())
expect("claim waits for the gym", r.json()["status"], "claimed")
r = c.post(f"{B}/gyms/{G}/upi-requests/{req['id']}/verify", headers=H(T), json={})
expect("owner verifies", (r.status_code, r.json()["status"], r.json()["auto_confirmed"]), (200, "verified", False))

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
