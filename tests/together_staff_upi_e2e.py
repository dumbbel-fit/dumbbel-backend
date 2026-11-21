"""Add member → Custom (several people, one plan together), staff at work with location, console UPI payments.

  * several people added at once; a phone that is already a member is reused, never duplicated
  * one plan assigned to all of them: price × people, discount per member or off the total, one payment split equally
  * staff check in at the gym / away / without location, check out; owner sees days, hours, place and work done
  * console lists every UPI payment made through DUMBBEL.fit with totals per status and per gym
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/together_staff_upi_e2e.py"""
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
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Together Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active', lat=17.448300, lng=78.391500 WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 200000}).json()
P = P.get("plan", P)["id"]

print("== Add member → Custom")
old = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Old Ravi", "phone": (op := rnd())}).json()["member"]
a, b = rnd(), rnd()
r = c.post(f"{B}/gyms/{G}/members/bulk", headers=H(T, True), json={"people": [{"name": "Anil", "phone": a}, {"name": "Bala", "phone": b}, {"name": "Ravi again", "phone": op}]})
expect("added", r.status_code, 201)
ms = r.json()["members"]
expect("existing phone reused", [(x["member"]["name"], x["existing"]) for x in ms], [("Anil", False), ("Bala", False), ("Old Ravi", True)])
expect("same phone twice refused", c.post(f"{B}/gyms/{G}/members/bulk", headers=H(T, True), json={"people": [{"name": "X Y", "phone": a}, {"name": "Z W", "phone": a}]}).status_code, 422)
ids = [x["member"]["id"] for x in ms]
TG = f"{B}/gyms/{G}/memberships/together"
expect("overpaying refused", c.post(TG, headers=H(T, True), json={"plan_id": P, "member_ids": ids, "payment": {"amount_paise": 700000, "method": "cash"}}).status_code, 422)
expect("discount above one person's price refused", c.post(TG, headers=H(T, True), json={"plan_id": P, "member_ids": ids, "discount": {"mode": "per_member", "amount_paise": 250000}}).status_code, 422)
with engine.begin() as cn:
    expect("refusals saved nothing", cn.execute(text("SELECT COUNT(*) FROM memberships WHERE gym_id=:g"), {"g": G}).scalar(), 0)
r = c.post(TG, headers=H(T, True), json={"plan_id": P, "member_ids": ids, "discount": {"mode": "total", "amount_paise": 60000},
                                        "payment": {"amount_paise": 540000, "method": "upi", "reference": "512345678901"}})
expect("assigned together", r.status_code, 201)
d = r.json()
expect("price each after a ₹600 total discount", [x["membership"]["price_paise"] for x in d["members"]], [180000] * 3)
expect("total and nothing due", (d["total_paise"], d["due_paise"]), (540000, 0))
expect("payment split equally", [x["payment"]["amount_paise"] for x in d["members"]], [180000] * 3)
expect("one group", len({x["membership"]["group_id"] for x in d["members"]}), 1)
det = c.get(f"{B}/gyms/{G}/members/{ids[0]}", headers=H(T)).json()
mm = (det.get("memberships") or det.get("member", {}).get("memberships") or [{}])[0]
expect("member page: assigned together with", sorted(x["name"] for x in mm.get("group", [])), ["Bala", "Old Ravi"])
two = [ids[0], ids[1]]
r = c.post(TG, headers=H(T, True), json={"plan_id": P, "member_ids": two, "discount": {"mode": "per_member", "amount_paise": 10000},
                                        "payment": {"amount_paise": 200000, "method": "cash"}})
d = r.json()
expect("renewal: per-member discount", [x["membership"]["price_paise"] for x in d["members"]], [190000, 190000])
expect("part payment split, rest due", ([x["payment"]["amount_paise"] for x in d["members"]], d["due_paise"]), ([100000, 100000], 180000))
expect("renewal starts after the current plan", d["members"][0]["membership"]["start_date"] > mm["end_date"], True)

print("\n== staff at work")
sp = rnd()
st = c.post(f"{B}/gyms/{G}/staff", headers=H(T), json={"name": "Suresh Desk", "phone": sp, "staff_role": "receptionist"}).json()
S = login(sp)
me = c.get(f"{B}/gyms/{G}/staff/me/shift", headers=H(S)).json()
expect("not on shift yet", (me["on_shift"], me["gym_location_set"]), (False, True))
QR = c.get(f"{B}/gyms/{G}/checkin-qr", headers=H(T)).json()["qr"]["url"]
SC = f"{B}/gyms/{G}/staff/me/scan"
at_gym = {"lat": 17.4484, "lng": 78.3916, "accuracy_m": 20}
expect("old buttons are gone", c.post(f"{B}/gyms/{G}/staff/me/check-in", headers=H(S), json=at_gym).status_code, 404)
expect("fake QR refused", c.post(SC, headers=H(S), json={"code": QR[:-4] + "abcd", **at_gym}).json()["error"]["code"], "WRONG_QR")
expect("no location refused", c.post(SC, headers=H(S), json={"code": QR}).json()["error"]["code"], "LOCATION_REQUIRED")
far = c.post(SC, headers=H(S), json={"code": QR, "lat": 17.5, "lng": 78.5})
expect("from far away refused", (far.status_code, far.json()["error"]["code"]), (422, "TOO_FAR"))
r = c.post(SC, headers=H(S), json={"code": QR, **at_gym})
expect("scan at the gym starts the shift", (r.status_code, r.json()["action"], r.json()["shift"]["in"]["place"]), (200, "in", "at_gym"))
expect("double scan doesn't end it", c.post(SC, headers=H(S), json={"code": QR, **at_gym}).json()["error"]["code"], "JUST_STARTED")
mid = c.post(f"{B}/gyms/{G}/members", headers=H(S, True), json={"name": "Desk Added", "phone": rnd()}).json()["member"]["id"]
c.post(f"{B}/gyms/{G}/checkins/manual", headers=H(S, True), json={"member_id": ids[0]})
with engine.begin() as cn:
    cn.execute(text("UPDATE staff_shifts SET in_at = DATE_SUB(in_at, INTERVAL 3 HOUR) WHERE gym_id=:g"), {"g": G})
r = c.post(SC, headers=H(S), json={"code": QR, **at_gym})
expect("next scan ends it", (r.json()["action"], r.json()["shift"]["minutes"] >= 180), ("out", True))
r = c.post(SC, headers=H(S), json={"code": QR, **at_gym})
expect("and the next starts a new one", r.json()["action"], "in")
with engine.begin() as cn:
    g2 = cn.execute(text("SELECT id FROM gyms WHERE id<>:g AND checkin_secret IS NOT NULL LIMIT 1"), {"g": G}).scalar()
if g2:
    expect("another branch's QR refused", c.post(SC, headers=H(S), json={"code": QR.replace(G, g2), **at_gym}).json()["error"]["code"], "WRONG_QR")
w = c.get(f"{B}/gyms/{G}/staff/work", headers=H(T), params={"range": "today"}).json()
row = [x for x in w["items"] if x["name"] == "Suresh Desk"][0]
expect("owner sees: on shift, 1 day, at the gym", (row["on_shift"], row["days_present"], row["today"]["place"], row["minutes"] >= 180), (True, 1, "at_gym", True))
expect("owner sees the work done", (row["work"]["members_added"], row["work"]["checkins_handled"]), (1, 1))
expect("owner's own row: payments he recorded", [x["work"]["collected_paise"] for x in w["items"] if x["role"] == "owner"], [740000])
log = c.get(f"{B}/gyms/{G}/staff/{st['id']}/shifts", headers=H(T)).json()
expect("shift log with map links", (len(log["items"]), log["items"][-1]["in"]["point"]["map_url"].startswith("https://maps.google.com/?q=17.4484")), (2, True))
expect("staff can't see everyone", c.get(f"{B}/gyms/{G}/staff/work", headers=H(S)).status_code, 403)

print("\n== console: UPI payments through DUMBBEL.fit")
acc = c.post(f"{B}/gyms/{G}/upi-accounts", headers=H(T), json={"vpa": "together@okhdfcbank", "payee_name": "Together Gym"})
expect("upi id added", acc.status_code, 201)
req = c.post(f"{B}/gyms/{G}/upi-requests", headers=H(T, True), json={"member_id": mid, "amount_paise": 50000}).json()
c.post(f"{B}/gyms/{G}/upi-requests/{req['id']}/verify", headers=H(T), json={"utr": "612345678901"})
c.post(f"{B}/gyms/{G}/upi-requests", headers=H(T, True), json={"member_id": ids[1], "amount_paise": 20000})
ADMIN = rnd()
A = login(ADMIN)
with engine.begin() as cn:
    cn.execute(text("UPDATE users SET is_platform_admin=1 WHERE phone LIKE :p"), {"p": f"%{ADMIN}"})
u = c.get(f"{B}/internal/upi-payments", headers=H(A, app="web"), params={"gym_id": G, "range": "7d"}).json()
expect("totals", (u["totals"]["verified"], u["totals"]["pending"]["count"]), ({"count": 1, "paise": 50000}, 1))
expect("who paid", [(i["member_name"], i["status"], i["amount_paise"], i["utr"], bool(i["receipt_no"])) for i in u["items"] if i["status"] == "verified"],
       [("Desk Added", "verified", 50000, "612345678901", True)])
expect("per gym", (u["by_gym"][0]["verified"], u["by_gym"][0]["verified_paise"]), (1, 50000))
expect("filter verified", {i["status"] for i in c.get(f"{B}/internal/upi-payments", headers=H(A, app="web"), params={"gym_id": G, "status": "verified"}).json()["items"]}, {"verified"})
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
