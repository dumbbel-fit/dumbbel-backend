"""Only the “Your gym is on DUMBBEL.fit” automation (gym_live) is banned; every other automation works as before.

  * gym_live is always off and can't be switched on (409 AUTOMATION_BANNED)
  * other automations still switch on and off
  * gym_live messages still waiting are cancelled (kept, not deleted); other waiting messages are untouched
Run against a TEST database with the API on :8000:  DATABASE_URL=...dumbbel_test python tests/wa_gym_live_ban_e2e.py"""
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
from app.services import whatsapp as wa  # noqa: E402

B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731
H = lambda t: {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = c.post(f"{B}/gyms", headers=H(T), json={"name": "Live Ban Gym " + rnd()[-4:], "city": "Hyderabad"}).json()["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
    cn.execute(text("INSERT INTO gym_whatsapp (gym_id, provider, status, instance_name, connected_at, settings) "
                    "VALUES (:g, 'web', 'connected', :i, NOW(), :s)"),
               {"g": G, "i": f"t-{G}", "s": '{"automations": {"gym_live": true}}'})  # was on before the ban


def settings():
    d = c.get(f"{B}/gyms/{G}/whatsapp", headers=H(T)).json()
    return d.get("settings") or d.get("config", {}).get("settings")


s = settings()
expect("gym_live off even if it was saved on", s["automations"]["gym_live"], False)
expect("other automations as before (expiry reminder on by default)", s["automations"]["expiry_reminder"], True)
r = c.patch(f"{B}/gyms/{G}/whatsapp/settings", headers=H(T), json={"automations": {"gym_live": True}})
expect("switching gym_live on refused", (r.status_code, r.json().get("error", {}).get("code")), (409, "AUTOMATION_BANNED"))
r = c.patch(f"{B}/gyms/{G}/whatsapp/settings", headers=H(T), json={"automations": {"expiry_reminder": False, "welcome": True}})
expect("other automations still switch", (r.status_code, settings()["automations"]["expiry_reminder"]), (200, False))
c.patch(f"{B}/gyms/{G}/whatsapp/settings", headers=H(T), json={"automations": {"expiry_reminder": True}})
expect("and back on", settings()["automations"]["expiry_reminder"], True)

live_id, rem_id = str(uuid.uuid4()), str(uuid.uuid4())
with engine.begin() as cn:
    for mid, ev in ((live_id, "gym_live"), (rem_id, "expiry_reminder")):
        cn.execute(text("INSERT INTO wa_messages (id, gym_id, phone, event, category, template_key, body, status, "
                        "next_attempt_at, `trigger`, attempts, created_at, updated_at) VALUES (:id, :g, '919999999999', "
                        ":e, 'UTILITY', :e, 'x', 'queued', DATE_ADD(NOW(), INTERVAL 1 DAY), 'auto', 0, NOW(), NOW())"),
                   {"id": mid, "g": G, "e": ev})
wa.process_queue()
with engine.begin() as cn:
    st = dict(cn.execute(text("SELECT id, CONCAT(status, '/', COALESCE(error_code, '')) FROM wa_messages WHERE id IN (:a, :b)"),
                         {"a": live_id, "b": rem_id}).all())
expect("waiting gym_live message cancelled", st[live_id], "cancelled/banned")
expect("waiting expiry reminder untouched", st[rem_id], "queued/")
expect("onboarding queues nothing", wa.onboard_gym(G), 0)

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
