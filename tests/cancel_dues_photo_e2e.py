"""Cancelling takes the money out of collections, dues show on payments, and member photos always save.

  * cancelling a paid membership voids its payments (kept, shown as voided): today / month totals drop
  * cancelling with remove_payments=false keeps them counted; staff without payments.void can't remove them
  * an older cancelled membership's payments can be removed afterwards (only for cancelled ones)
  * the payments list says how much is still due on each payment's membership
  * a photo added with a new member is saved even when that person already uses the app

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/cancel_dues_photo_e2e.py"""
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
fails: list[str] = []
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x08" * 64 + bytes.fromhex("ffd9")


def H(t, idem=False, app="admin"):
    h = {"Authorization": f"Bearer {t}", "X-App": app, "X-App-Version": "1.0.0"}
    if idem:
        h["Idempotency-Key"] = str(uuid.uuid4())
    return h


def chk(n, r, code=None):
    ok = r.status_code == code if code else r.status_code < 400
    print("OK  " if ok else "FAIL", r.status_code, n, "" if ok else r.text[:300])
    ok or fails.append(n)
    try:
        return r.json()
    except Exception:
        return None


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))


def login(phone, app="web"):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone, "app": app}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


T = login(rnd())
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": "Cancel Gym " + rnd()[-4:], "city": "Hyderabad"}))["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json()
P = P.get("plan", P)["id"]


def add(name, paid):
    r = chk(f"add {name}", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={
        "name": name, "phone": rnd(), "membership": {"plan_id": P},
        **({"payment": {"amount_paise": paid, "method": "cash"}} if paid else {})}), 201)
    return r["member"]["id"], r["membership"]["id"]


def money():
    s = c.get(f"{B}/gyms/{G}/summary", headers=H(T)).json()["numbers"]
    t = c.get(f"{B}/gyms/{G}/payments", headers=H(T), params={"status": "recorded"}).json()["totals"]
    return s["today_collected_paise"], s["collected_month_paise"], t["total_paise"]


print("\n== dues on the payments list")
a_m, a_ms = add("Part Payer", 40000)
pl = c.get(f"{B}/gyms/{G}/payments", headers=H(T)).json()["items"]
row = [p for p in pl if p["member"]["id"] == a_m][0]
expect("payment row says ₹600 still due", (row["due_paise"], row["membership"]["due_paise"], row["membership"]["status"]), (60000, 60000, "active"))

print("\n== cancel removes the money from collections")
b_m, b_ms = add("Full Payer", 100000)
before = money()
r = chk("cancel (default: remove payments)", c.post(f"{B}/memberships/{b_ms}/cancel", headers=H(T), json={"reason": "Entered by mistake"}))
expect("says what was removed", (r["payments_voided"], r["voided_paise"]), (1, 100000))
after = money()
expect("today, month and list totals all drop by ₹1,000", tuple(b - a for b, a in zip(before, after)), (100000, 100000, 100000))
v = c.get(f"{B}/gyms/{G}/payments", headers=H(T), params={"status": "voided"}).json()["items"]
expect("payment kept, shown as voided with the reason", [(p["member"]["id"], p["void_reason"]) for p in v],
       [(b_m, "Membership cancelled: Entered by mistake")])
d = c.get(f"{B}/gyms/{G}/members/{b_m}", headers=H(T)).json()
expect("no dues and no credit left behind", (d["due_paise"], d["credit_paise"]), (0, 0))

print("\n== keep the money if asked")
k_m, k_ms = add("No Refund", 100000)
before = money()
r = chk("cancel, keep payments", c.post(f"{B}/memberships/{k_ms}/cancel", headers=H(T), json={"reason": "Quit", "remove_payments": False}))
expect("nothing voided", r["payments_voided"], 0)
expect("totals unchanged", money(), before)

print("\n== older cancelled memberships can be cleaned up afterwards")
before = money()
r = chk("remove the kept payments now", c.post(f"{B}/memberships/{k_ms}/remove-payments", headers=H(T)))
expect("removed ₹1,000", r["voided_paise"], 100000)
expect("totals drop", before[0] - money()[0], 100000)
chk("not for an active membership", c.post(f"{B}/memberships/{a_ms}/remove-payments", headers=H(T)), 409)

print("\n== staff without payments.void")
sp = rnd()
chk("add receptionist", c.post(f"{B}/gyms/{G}/staff", headers=H(T, True), json={"name": "Desk", "phone": sp, "staff_role": "receptionist",
                                                                              "permissions": ["members.view", "members.edit", "memberships.cancel", "payments.view"]}))
S = login(sp, "admin")
s_m, s_ms = add("Staff Cancel", 100000)
chk("can't ask to remove payments", c.post(f"{B}/memberships/{s_ms}/cancel", headers=H(S), json={"reason": "x", "remove_payments": True}), 403)
before = money()
r = chk("default cancel works, keeps the money", c.post(f"{B}/memberships/{s_ms}/cancel", headers=H(S), json={"reason": "x"}))
expect("nothing voided by staff", (r.get("payments_voided"), money()), (0, before))
chk("staff can't remove afterwards either", c.post(f"{B}/memberships/{s_ms}/remove-payments", headers=H(S)), 403)

print("\n== member photo always saves")
JPEG_KEY = None
for label, on_app in [("new person", False), ("already on the app", True)]:
    ph = rnd()
    if on_app:
        login(ph, "member")
    p = c.post(f"{B}/uploads/presign", headers=H(T), json={"purpose": "member_photo", "content_type": "image/jpeg", "size": len(JPEG)}).json()
    chk(f"{label}: upload", c.put(p["upload_url"], content=JPEG, headers={"Content-Type": "image/jpeg"}))
    m = chk(f"{label}: add with photo", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Photo " + label, "phone": ph, "photo_key": p["file_key"]}), 201)
    url = m["member"].get("photo_url")
    expect(f"{label}: photo saved and loads", bool(url) and c.get(url).status_code, 200)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
