"""“Why didn't this member get the welcome?” — GET /gyms/{g}/members/{m}/whatsapp answers it.

  * added with a plan while WhatsApp is connected → welcome queued/sent
  * added without a plan → no_plan (it goes out when the first plan is added; then it does)
  * welcome automation switched off → not_sent / automation_off, and the skip is logged
  * WhatsApp needs relinking → disconnected (kept waiting), not silently lost
  * gym that never linked WhatsApp → not_linked
Run against a TEST database with the API on :8000 (EVOLUTION_API_URL set, RUN_SCHEDULER=false):
    DATABASE_URL=...dumbbel_test python tests/wa_welcome_why_e2e.py"""
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


def owner_gym(linked=True):
    r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
    T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
    G = c.post(f"{B}/gyms", headers=H(T), json={"name": f"Why Test {rnd()[-4:]}", "city": "Hyderabad"}).json()["id"]
    with engine.begin() as cn:
        cn.execute(text("UPDATE gyms SET status='active', approved_at=NOW() WHERE id=:g"), {"g": G})
        if linked:
            cn.execute(text("""INSERT INTO gym_whatsapp (gym_id, provider, instance_name, status, connected_at, created_at, updated_at)
                               VALUES (:g,'web',:n,'connected',DATE_SUB(NOW(), INTERVAL 1 DAY),NOW(),NOW())"""), {"g": G, "n": "dmbl-" + G})
    P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 100000}).json()
    return T, G, P.get("plan", P)["id"]


def add(T, G, name, plan=None):
    body = {"name": name, "phone": rnd(), **({"membership": {"plan_id": plan}} if plan else {})}
    return chk(f"add {name}", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json=body), 201)["member"]["id"]


def why(T, G, mid, wait=4.0):
    time.sleep(wait)  # triggers run 1.5 s after the request
    return chk("member whatsapp", c.get(f"{B}/gyms/{G}/members/{mid}/whatsapp", headers=H(T)))


T, G, P = owner_gym()
print("\n== connected, added with a plan")
w = why(T, G, add(T, G, "Ravi", P))["welcome"]
expect("welcome went into the outbox", w["state"] in ("sending", "sent") or (w["state"] == "not_sent" and w["message_id"] is not None), True)

print("\n== added without a plan")
mid = add(T, G, "No Plan")
d = why(T, G, mid)
expect("reason: no plan", (d["welcome"]["state"], d["welcome"]["code"]), ("not_sent", "no_plan"))
expect("they still got the 'gym is on DUMBBEL' message", [m["event"] for m in d["messages"]], ["gym_live"])
chk("first plan added later", c.post(f"{B}/gyms/{G}/members/{mid}/memberships", headers=H(T, True), json={"plan_id": P}), 201)
expect("now the welcome goes out", why(T, G, mid)["welcome"]["message_id"] is not None, True)

print("\n== welcome automation switched off")
with engine.begin() as cn:
    s = json.loads(cn.execute(text("SELECT settings FROM gym_whatsapp WHERE gym_id=:g"), {"g": G}).scalar() or "{}")
    s.setdefault("automations", {})["welcome"] = False
    cn.execute(text("UPDATE gym_whatsapp SET settings=:s WHERE gym_id=:g"), {"s": json.dumps(s), "g": G})
d = why(T, G, add(T, G, "Switched Off", P))
expect("reason: automation off", (d["welcome"]["state"], d["welcome"]["code"]), ("not_sent", "automation_off"))
expect("the skip is logged", [x["reason"] for x in d["not_sent_log"] if x["event"] == "welcome"], ["automation_off"])

print("\n== WhatsApp needs relinking")
with engine.begin() as cn:
    s["automations"]["welcome"] = True
    cn.execute(text("UPDATE gym_whatsapp SET settings=:s, status='error' WHERE gym_id=:g"), {"s": json.dumps(s), "g": G})
d = why(T, G, add(T, G, "While Down", P))
expect("reason: disconnected, kept waiting", (d["welcome"]["state"], d["welcome"]["code"], d["connection"]["connected"]),
       ("not_sent", "disconnected", False))

print("\n== never linked")
T2, G2, P2 = owner_gym(linked=False)
d = why(T2, G2, add(T2, G2, "Nobody Linked", P2))
expect("reason: not linked", (d["welcome"]["state"], d["welcome"]["code"]), ("not_sent", "not_linked"))

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
