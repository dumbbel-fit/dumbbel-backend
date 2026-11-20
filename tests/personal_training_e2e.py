"""Personal training (PT) on members: import columns, add/edit, members filter, Overview numbers, export.

  * a gym's sheet with "Personal Trainer" (Yes/No) and "PT Amount (₹)" columns maps them automatically
  * Yes / a fee above 0 / a trainer's name → PT on; "No" with 0 → off; plan and paid columns unchanged
  * add a member with PT, edit it, switch it off; the "pt" members filter and the summary count follow
  * the PT fee is information only: collections don't change

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/personal_training_e2e.py"""
import os
import random
import sys
import uuid
from types import SimpleNamespace

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.services import importer  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails: list[str] = []


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


print("== import: the gym's own sheet")
headers = ["#", "Member Name", "Phone Number", "Plan", "Plan Amount (₹)", "Start Date", "End Date", "Days", "Status",
           "Personal Trainer", "PT Amount (₹)", "Total Amount (₹)", "Notes", "As saved in phone"]
rows = [dict(zip(headers, r)) for r in [
    ["1", "Anusha", "7036267183", "Yearly", "7999", "2024-06-02", "2025-06-01", "365", "Expired", "No", "0", "7999", "", "Anusha BR's"],
    ["2", "Akhil", "9885800328", "Monthly", "2000", "2024-09-11", "2024-10-10", "30", "Expired", "Yes", "3000", "5000", "", "Akhil 1mt"],
    ["3", "Lokesh", "8374333122", "Monthly", "2000", "2025-01-04", "2025-02-02", "30", "Expired", "No", "1500", "3500", "", "Lokesh"]]]
plans = [SimpleNamespace(id="p-year", name="Yearly", duration_days=365, price_paise=799900, active=True),
         SimpleNamespace(id="p-month", name="Monthly", duration_days=30, price_paise=200000, active=True)]
mapping, _ = importer.auto_map(headers, rows, plans)
expect("PT columns matched", (mapping["Personal Trainer"], mapping["PT Amount (₹)"]), ("pt", "pt_fee"))
expect("plan amount still the paid column", mapping["Plan Amount (₹)"], "paid")
by_id = {p.id: p for p in plans}
got = []
for r in rows:
    n, _, _ = importer.normalize_row(r, mapping, {}, plans, by_id, "ymd")
    got.append((n["name"], n["pt_enabled"], n["pt_fee_paise"], n["paid_paise"]))
expect("Yes → PT; No + 0 → no PT; a fee → PT", got,
       [("Anusha", False, None, 799900), ("Akhil", True, 300000, 200000), ("Lokesh", True, 150000, 200000)])
m2 = {**mapping, "Notes": "pt_trainer"}
n, _, _ = importer.normalize_row({**rows[0], "Notes": "Ravi"}, m2, {}, plans, by_id, "ymd")
expect("a trainer's name with 'No' stays off", n["pt_enabled"], False)
n, _, _ = importer.normalize_row({**rows[0], "Personal Trainer": "", "Notes": "Ravi"}, m2, {}, plans, by_id, "ymd")
expect("a trainer's name alone turns it on", (n["pt_enabled"], n["pt_trainer"]), (True, "Ravi"))

print("\n== members")
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": f"PT Gym {rnd()[-4:]}", "city": "Hyderabad"}))["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 200000}).json()
P = P.get("plan", P)["id"]
before = c.get(f"{B}/gyms/{G}/summary", headers=H(T)).json()["numbers"]
m = chk("add with PT", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={
    "name": "Akhil", "phone": rnd(), "membership": {"plan_id": P}, "payment": {"amount_paise": 200000, "method": "cash"},
    "pt_enabled": True, "pt_fee_paise": 300000, "pt_trainer": "Ravi"}), 201)["member"]
expect("saved on the member", m["pt"], {"enabled": True, "fee_paise": 300000, "trainer": "Ravi"})
chk("add without PT", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Anusha", "phone": rnd(), "membership": {"plan_id": P}}), 201)
lst = c.get(f"{B}/gyms/{G}/members", headers=H(T), params={"status": "pt"}).json()
expect("PT filter lists only Akhil", ([i["name"] for i in lst["items"]], lst["counts"]["pt"]), (["Akhil"], 1))
s = c.get(f"{B}/gyms/{G}/summary", headers=H(T)).json()["numbers"]
expect("Overview counts PT members and their fees", (s["pt_members"], s["pt_active_members"], s["pt_fees_paise"]), (1, 1, 300000))
expect("collections only have the plan payment", s["collected_month_paise"] - before["collected_month_paise"], 200000)
d = chk("edit trainer and fee", c.patch(f"{B}/gyms/{G}/members/{m['id']}", headers=H(T), json={"pt_fee_paise": 250000, "pt_trainer": "Suresh"}))
d = c.get(f"{B}/gyms/{G}/members/{m['id']}", headers=H(T)).json()
pt = (d.get("member") or d).get("pt")
expect("detail shows the change", pt, {"enabled": True, "fee_paise": 250000, "trainer": "Suresh"})
chk("switch PT off", c.patch(f"{B}/gyms/{G}/members/{m['id']}", headers=H(T), json={"pt_enabled": False}))
expect("filter is empty now", c.get(f"{B}/gyms/{G}/members", headers=H(T), params={"status": "pt"}).json()["items"], [])
csv_text = c.get(f"{B}/gyms/{G}/members/export.csv", headers=H(T)).text
expect("export has the PT columns", "Personal training" in csv_text.splitlines()[0] and "Trainer" in csv_text.splitlines()[0], True)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
