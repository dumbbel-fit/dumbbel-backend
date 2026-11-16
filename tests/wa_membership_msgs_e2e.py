"""Desk actions → the right WhatsApp message, through the real API.

New member with a plan → welcome; payment → receipt; cancel → cancelled; a new plan for someone whose plan was
cancelled → "membership confirmed" (not a skipped second welcome); freeze → on hold; unfreeze → resumed.
Run against a TEST database with the API on :8000 (EVOLUTION_API_URL set, RUN_SCHEDULER=false):
    DATABASE_URL=...dumbbel_test python tests/wa_membership_msgs_e2e.py"""
import os
import random
import sys
import time
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


def H(t, idem=False):
    h = {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}
    if idem:
        h["Idempotency-Key"] = str(uuid.uuid4())
    return h


def chk(n, r, code=None):
    ok = r.status_code == code if code else r.status_code < 400
    print("OK  " if ok else "FAIL", r.status_code, n, "" if ok else r.text[:300])
    ok or fails.append(n)
    return r.json() if r.content else None


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": f"Msg Test {rnd()[-4:]}", "city": "Hyderabad"}))["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active', approved_at=NOW() WHERE id=:g"), {"g": G})
    cn.execute(text("""INSERT INTO gym_whatsapp (gym_id, provider, instance_name, status, connected_at, created_at, updated_at)
                       VALUES (:g,'web',:n,'connected',NOW(),NOW(),NOW())"""), {"g": G, "n": "dmbl-" + G})
P = chk("plan", c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}))["id"]


def events(member_id, settle=3.0, limit=12.0):
    """Triggers run 1.5 s after the request (a gym's first ones also create its templates): wait until the list
    stops changing."""
    end, last, stable_since = time.time() + limit, None, time.time()
    while time.time() < end:
        with engine.connect() as cn:
            cur = sorted(e for (e,) in cn.execute(text("SELECT event FROM wa_messages WHERE gym_id=:g AND member_id=:m"), {"g": G, "m": member_id}))
        if cur != last:
            last, stable_since = cur, time.time()
        elif cur and time.time() - stable_since >= settle:
            break
        time.sleep(0.5)
    return last


m = chk("new member with plan + payment", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={
    "name": "Ravi", "phone": rnd(), "membership": {"plan_id": P}, "payment": {"amount_paise": 100000, "method": "cash"}}), 201)
MID, MS = m["member"]["id"], (m.get("membership") or {}).get("id")
expect("welcome + receipt + go-live", events(MID), ["gym_live", "payment_received", "welcome"])

chk("cancel", c.post(f"{B}/memberships/{MS}/cancel", headers=H(T), json={"reason": "Moving away"}))
expect("cancelled message", "membership_cancelled" in events(MID), True)

n = chk("new plan after the cancel, with payment", c.post(f"{B}/gyms/{G}/members/{MID}/memberships", headers=H(T, True), json={
    "plan_id": P, "payment": {"amount_paise": 100000, "method": "upi", "reference": rnd() + "12"}}), 201)
ev = events(MID)
expect("returning member gets 'membership confirmed' (not a skipped welcome)", ev.count("renewal_confirmed"), 1)
expect("and a second receipt", ev.count("payment_received"), 2)
expect("still only one welcome", ev.count("welcome"), 1)
MS2 = n["membership"]["id"]

chk("freeze", c.post(f"{B}/memberships/{MS2}/freeze", headers=H(T), json={"days": 7, "reason": "travel"}))
expect("on hold message", "membership_frozen" in events(MID), True)
chk("unfreeze", c.post(f"{B}/memberships/{MS2}/unfreeze", headers=H(T)))
expect("resumed message", "membership_resumed" in events(MID), True)

with engine.connect() as cn:
    body = cn.execute(text("SELECT body FROM wa_messages WHERE gym_id=:g AND member_id=:m AND event='renewal_confirmed'"), {"g": G, "m": MID}).scalar()
expect("confirmation has plan and validity", ("Monthly" in body, "valid until" in body, "{{" in body), (True, True, False))

print("\nFAILS:", fails or "none")
sys.exit(1 if fails else 0)
