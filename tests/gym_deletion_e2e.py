"""Gym deletion: owner asks → console archives and deletes → the owner can register again.

Checks who may ask, every request state, that no row of the gym survives in any table, that other gyms and
people who use the app are untouched, that files shared with a branch are kept, and what the archive holds.

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/gym_deletion_e2e.py"""
import json
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
    try:
        return r.json()
    except Exception:
        return None


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731


def login(phone=None):
    r = c.post(f"{B}/auth/otp/send", json={"phone": phone or rnd(), "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()


def sql(q, **p):
    with engine.begin() as cn:
        return cn.execute(text(q), p)


def one(q, **p):
    return sql(q, **p).scalar()


def upload(tok, purpose, gid=None):
    p = chk(f"presign {purpose}", c.post(f"{B}/uploads/presign", headers=H(tok), json={"purpose": purpose, "content_type": "image/png", "size": 68}))
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b4944415478da63000100000500010d0a2db40000000049454e44ae426082")
    chk(f"put {purpose}", c.put(p["upload_url"], content=png, headers={"Content-Type": "image/png"}))
    return p["file_key"]


# ---------------- seed ----------------
so, sb = login(), login()
TO, TB = so["access_token"], sb["access_token"]
OWNER_ID = so["user"]["id"]
tag = rnd()[-5:]
GA = chk("gym A", c.post(f"{B}/gyms", headers=H(TO), json={"name": f"Del  Alpha {tag}", "city": "Hyderabad"}))["id"]
GB = chk("gym B (other owner)", c.post(f"{B}/gyms", headers=H(TB), json={"name": f"Del Beta {tag}", "city": "Pune"}))["id"]
sql("UPDATE gyms SET status='active', approved_at=NOW() WHERE id IN (:a,:b)", a=GA, b=GB)
CODE = one("SELECT code_prefix FROM gyms WHERE id=:g", g=GA)
PLAN = chk("plan", c.post(f"{B}/gyms/{GA}/plans", headers=H(TO), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}))["id"]

P_ONLY, P_SHARED, P_APP, P_STAFF = rnd(), rnd(), rnd(), rnd()
def add(gid, tok, name, phone, pay=False):
    body = {"name": name, "phone": phone}
    if pay:
        body |= {"membership": {"plan_id": PLAN}, "payment": {"amount_paise": 100000, "method": "cash"}}
    return chk(f"member {name}", c.post(f"{B}/gyms/{gid}/members", headers=H(tok, True), json=body))["member"]["id"]
M_ONLY = add(GA, TO, "Only Here", P_ONLY, pay=True)
M_SHARED = add(GA, TO, "Also At B", P_SHARED)
add(GB, TB, "Also At B", P_SHARED)
M_APP = add(GA, TO, "Uses App", P_APP, pay=True)
chk("check-in", c.post(f"{B}/gyms/{GA}/checkins/manual", headers=H(TO, True), json={"member_id": M_ONLY}))
U_ONLY = one("SELECT id FROM users WHERE phone=:p", p="+91" + P_ONLY)
U_SHARED = one("SELECT id FROM users WHERE phone=:p", p="+91" + P_SHARED)
U_APP = login(P_APP)["user"]["id"]                       # this person logs in: keeps the account
chk("staff added", c.post(f"{B}/gyms/{GA}/staff", headers=H(TO), json={"name": "Desk", "phone": P_STAFF, "staff_role": "manager"}))
TS = login(P_STAFF)["access_token"]
U_STAFF = one("SELECT id FROM users WHERE phone=:p", p="+91" + P_STAFF)

PHOTO = upload(TO, "gym_photo")
chk("photo", c.post(f"{B}/gyms/{GA}/photos", headers=H(TO), json={"file_key": PHOTO}))
LOGO = upload(TO, "gym_logo")
chk("logo", c.patch(f"{B}/gyms/{GA}", headers=H(TO), json={"logo_key": LOGO}))
BR = chk("branch (copies the logo)", c.post(f"{B}/gyms/{GA}/branches", headers=H(TO), json={"name": f"Del Branch {tag}"}), 201)["id"]
sql("""INSERT INTO gym_whatsapp (gym_id, provider, status, created_at, updated_at) VALUES (:g,'cloud','connected',NOW(),NOW())""", g=GA)
sql("""INSERT INTO wa_messages (id, gym_id, member_id, phone, event, category, template_key, status, attempts, `trigger`, created_at, updated_at)
       VALUES (:i,:g,:m,:p,'welcome','UTILITY','welcome','sent',1,'auto',NOW(),NOW())""", i=str(uuid.uuid4()), g=GA, m=M_ONLY, p="+91" + P_ONLY)
B_MEMBERS = one("SELECT COUNT(*) FROM gym_members WHERE gym_id=:g", g=GB)

sa = login()
TA = sa["access_token"]
sql("UPDATE users SET is_platform_admin=1 WHERE id=:u", u=sa["user"]["id"])
D = f"{B}/internal/gym-deletions"

# ---------------- who may ask ----------------
chk("staff cannot ask", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TS), json={"reason": "x" * 5, "confirm_name": f"Del Alpha {tag}"}), 403)
chk("other gym's owner cannot ask", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TB), json={"reason": "bye bye", "confirm_name": f"Del Alpha {tag}"}), 403)
chk("staff cannot see it", c.get(f"{B}/gyms/{GA}/deletion-request", headers=H(TS)), 403)
chk("not the console", c.get(D, headers=H(TO)), 403)
chk("not the console: approve", c.post(f"{D}/x/approve", headers=H(TO), json={"confirm_code": "AAA"}), 403)
chk("not the console: archives", c.get(f"{B}/internal/gym-archives", headers=H(TO)), 403)
g0 = chk("owner sees nothing yet", c.get(f"{B}/gyms/{GA}/deletion-request", headers=H(TO)))
expect("no request yet", g0["request"], None)
expect("preview counts members", g0["gym"]["members"], 3)
expect("preview counts active memberships", g0["gym"]["active_memberships"], 2)
expect("preview sees WhatsApp", g0["gym"]["whatsapp_linked"], True)
chk("wrong name is refused", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TO), json={"reason": "closing", "confirm_name": "Some Other Gym"}), 422)
chk("reason is required", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TO), json={"reason": "", "confirm_name": f"Del Alpha {tag}"}), 422)

# ---------------- withdraw / reject / ask again ----------------
r1 = chk("owner asks (spacing and case don't matter)", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TO),
                                                        json={"reason": "Closing down", "confirm_name": f"  del alpha {tag} "}), 201)
expect("pending", r1["request"]["status"], "pending")
chk("asking twice is refused", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TO), json={"reason": "again", "confirm_name": f"Del Alpha {tag}"}), 409)
lst = chk("console lists it", c.get(D, headers=H(TA)))
mine = next((x for x in lst["items"] if x["id"] == r1["request"]["id"]), None)
expect("console sees the request", bool(mine), True)
expect("console sees live member count", mine and mine["gym"]["members"], 3)
expect("console sees who asked", mine and mine["requested_by"]["id"], OWNER_ID)
att = chk("attention feed", c.get(f"{B}/internal/attention", headers=H(TA)))
expect("attention lists the request", any(x["id"] == r1["request"]["id"] for x in att["deletion_requests"]), True)
w = chk("owner withdraws", c.delete(f"{B}/gyms/{GA}/deletion-request", headers=H(TO)))
expect("withdrawn", w["request"]["status"], "cancelled")
chk("withdrawing twice: nothing waiting", c.delete(f"{B}/gyms/{GA}/deletion-request", headers=H(TO)), 404)
chk("cannot approve a withdrawn one", c.post(f"{D}/{r1['request']['id']}/approve", headers=H(TA), json={"confirm_code": CODE}), 409)

r2 = chk("owner asks again", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TO), json={"reason": "Closing", "confirm_name": f"Del Alpha {tag}"}), 201)["request"]
chk("reject needs a reason", c.post(f"{D}/{r2['id']}/reject", headers=H(TA), json={"reason": ""}), 422)
rj = chk("console rejects", c.post(f"{D}/{r2['id']}/reject", headers=H(TA), json={"reason": "Members have paid ahead, settle first"}))
expect("rejected", rj["request"]["status"], "rejected")
seen = chk("owner sees the answer", c.get(f"{B}/gyms/{GA}/deletion-request", headers=H(TO)))
expect("owner sees rejected + note", (seen["request"]["status"], seen["request"]["decision_note"]), ("rejected", "Members have paid ahead, settle first"))
expect("gym still there after reject", one("SELECT COUNT(*) FROM gyms WHERE id=:g", g=GA), 1)

r3 = chk("owner asks a third time", c.post(f"{B}/gyms/{GA}/deletion-request", headers=H(TO), json={"reason": "Closed for good", "confirm_name": f"Del Alpha {tag}"}), 201)["request"]
det = chk("console detail", c.get(f"{D}/{r3['id']}", headers=H(TA)))
expect("detail counts rows per table", det["rows"].get("gym_members"), 3)
expect("detail counts check-ins", det["rows"].get("checkins"), 1)
chk("wrong code is refused", c.post(f"{D}/{r3['id']}/approve", headers=H(TA), json={"confirm_code": "ZZZ" if CODE != "ZZZ" else "YYY"}), 422)
expect("still there after wrong code", one("SELECT COUNT(*) FROM gyms WHERE id=:g", g=GA), 1)

# ---------------- delete ----------------
before = {t: one(f"SELECT COUNT(*) FROM `{t}` WHERE gym_id=:g", g=GA) for (t,) in sql(
    """SELECT TABLE_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME='gym_id'
       AND TABLE_NAME NOT IN ('gym_deletion_requests','gym_archives')""").all()}
ap = chk("console approves", c.post(f"{D}/{r3['id']}/approve", headers=H(TA), json={"confirm_code": CODE.lower(), "note": "Owner confirmed by phone"}))
expect("completed", ap["request"]["status"], "completed")
ARC = ap["archive"]["id"]
expect("request points at the archive", ap["request"]["archive_id"], ARC)
chk("approving twice is refused", c.post(f"{D}/{r3['id']}/approve", headers=H(TA), json={"confirm_code": CODE}), 409)

left = {t: one(f"SELECT COUNT(*) FROM `{t}` WHERE gym_id=:g", g=GA) for t in before}
expect("no row with this gym_id is left in any table", {t: n for t, n in left.items() if n and t != "audit_log"}, {})
expect("only the deletion's own audit line is left", one("SELECT COUNT(*) FROM audit_log WHERE gym_id=:g AND action<>'gym.deleted'", g=GA), 0)
expect("the deletion is in the audit log", one("SELECT COUNT(*) FROM audit_log WHERE gym_id=:g AND action='gym.deleted'", g=GA), 1)
expect("gym row gone", one("SELECT COUNT(*) FROM gyms WHERE id=:g", g=GA), 0)
expect("membership freezes / import rows not orphaned", one(
    "SELECT COUNT(*) FROM membership_freezes f LEFT JOIN memberships m ON m.id=f.membership_id WHERE m.id IS NULL"), 0)
expect("gym B untouched", one("SELECT COUNT(*) FROM gym_members WHERE gym_id=:g", g=GB), B_MEMBERS)
expect("branch untouched", one("SELECT COUNT(*) FROM gyms WHERE id=:g", g=BR), 1)
expect("account used only by this gym, never logged in: removed", one("SELECT COUNT(*) FROM users WHERE id=:u", u=U_ONLY), 0)
expect("account also at gym B: kept", one("SELECT COUNT(*) FROM users WHERE id=:u", u=U_SHARED), 1)
expect("account that logged in: kept", one("SELECT COUNT(*) FROM users WHERE id=:u", u=U_APP), 1)
expect("staff who logged in: kept", one("SELECT COUNT(*) FROM users WHERE id=:u", u=U_STAFF), 1)
expect("owner kept", one("SELECT COUNT(*) FROM users WHERE id=:u", u=OWNER_ID), 1)
expect("archive counts people removed", ap["archive"]["counts"]["accounts_removed"], 1)

for _ in range(20):            # files go in the background after the commit
    if not one("SELECT COUNT(*) FROM stored_files WHERE `key`=:k", k=PHOTO):
        break
    time.sleep(0.5)
expect("gym photo file deleted", one("SELECT COUNT(*) FROM stored_files WHERE `key`=:k", k=PHOTO), 0)
expect("logo kept: the branch still uses it", one("SELECT COUNT(*) FROM stored_files WHERE `key`=:k", k=LOGO), 1)

# ---------------- archive ----------------
al = chk("archives list", c.get(f"{B}/internal/gym-archives", headers=H(TA)))
a = next((x for x in al["items"] if x["id"] == ARC), None)
expect("archive listed", bool(a), True)
expect("archive keeps name and code", a and (a["gym_name"], a["code_prefix"]), (f"Del  Alpha {tag}", CODE))
expect("archive listing has no blob", a and "data" in a, False)
dl = c.get(f"{B}/internal/gym-archives/{ARC}/download", headers=H(TA))
chk("download", dl)
expect("download is an attachment", "attachment" in dl.headers.get("content-disposition", ""), True)
doc = json.loads(dl.content)
expect("archive has every member", len(doc["tables"]["gym_members"]), 3)
expect("archive has the payments", len(doc["tables"]["payments"]), before["payments"])
expect("archive has the check-in", len(doc["tables"]["checkins"]), 1)
expect("archive has the gym row", doc["tables"]["gyms"][0]["id"], GA)
expect("archive has the WhatsApp message", len(doc["tables"]["wa_messages"]), 1)
expect("archive row counts match what was there", {t: len(doc["tables"].get(t, [])) for t in before if t != "audit_log"},
       {t: n for t, n in before.items() if t != "audit_log"})
expect("archive has people's phones", "+91" + P_ONLY in {p["phone"] for p in doc["people"]}, True)
expect("archive lists files", PHOTO in doc["files"], True)
expect("download is audited", one("SELECT COUNT(*) FROM audit_log WHERE action='gym_archive.downloaded' AND entity_id=:a", a=ARC), 1)
done = chk("completed tab", c.get(D, headers=H(TA), params={"status": "completed"}))
expect("completed tab has it", any(x["id"] == r3["id"] for x in done["items"]), True)
expect("tab counts", done["counts"]["completed"] >= 1 and done["counts"]["cancelled"] >= 1 and done["counts"]["rejected"] >= 1, True)

# ---------------- start again ----------------
me = chk("owner /me", c.get(f"{B}/me", headers=H(TO)))
mine_ids = {g["gym_id"] for g in (me.get("gyms") or me.get("staff_of") or [])}
expect("deleted gym no longer on the owner's account", GA in mine_ids, False)
expect("branch still on the owner's account", BR in mine_ids, True)
chk("owner's old gym endpoints now 404", c.get(f"{B}/gyms/{GA}", headers=H(TO)), 404)
new = chk("same code can be registered again", c.post(f"{B}/gyms", headers=H(TB if False else login()["access_token"]),
                                                   json={"name": f"Del Alpha {tag}", "city": "Hyderabad", "code_prefix": CODE}), 201)
expect("new gym got the old code", new and new.get("code_prefix"), CODE)
rb = chk("owner asks to delete the branch too (pending gym)", c.post(f"{B}/gyms/{BR}/deletion-request", headers=H(TO),
                                                                  json={"reason": "Closing", "confirm_name": f"Del Branch {tag}"}), 201)["request"]
bcode = one("SELECT code_prefix FROM gyms WHERE id=:g", g=BR)
chk("console deletes the branch", c.post(f"{D}/{rb['id']}/approve", headers=H(TA), json={"confirm_code": bcode}))
for _ in range(20):
    if not one("SELECT COUNT(*) FROM stored_files WHERE `key`=:k", k=LOGO):
        break
    time.sleep(0.5)
expect("logo deleted once no gym uses it", one("SELECT COUNT(*) FROM stored_files WHERE `key`=:k", k=LOGO), 0)
again = chk("the same owner registers a fresh gym", c.post(f"{B}/gyms", headers=H(TO), json={"name": f"Del Fresh {tag}", "city": "Hyderabad"}), 201)
expect("fresh gym starts empty", one("SELECT COUNT(*) FROM gym_members WHERE gym_id=:g", g=again and again.get("id")), 0)
m2 = login(P_ONLY)
expect("removed member can still log in fresh", bool(m2.get("access_token")), True)

print("\nFAILS:", fails or "none")
sys.exit(1 if fails else 0)
