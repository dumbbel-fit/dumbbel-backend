"""End-to-end test of the WhatsApp integration against a MOCK Meta Graph API (no real messages are sent).

Run (API must be started with the same env, pointing at a test database):
    META_GRAPH_BASE=http://localhost:9099 META_APP_SECRET=testsecret META_WEBHOOK_VERIFY_TOKEN=vt \
    AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev RUN_SCHEDULER=false uvicorn app.main:app --port 8000
    META_GRAPH_BASE=http://localhost:9099 python tests/whatsapp_e2e.py
"""
import hashlib, hmac, json, os, random, sys, threading, time, uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
B = os.environ.get("API", "http://localhost:8000/v1")
SECRET = os.environ.get("META_APP_SECRET", "testsecret")
c = httpx.Client(timeout=60)
fails: list[str] = []

# ------------------------------------------------------------------ mock Meta
MOCK = {"sent": [], "templates": {}, "approve": False, "fail_next": None, "subscribed": []}


class Meta(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _auth_ok(self):
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if tok.startswith("BAD"):
            self._send(401, {"error": {"message": "Invalid OAuth access token.", "code": 190, "type": "OAuthException"}})
            return False
        return True

    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if not self._auth_ok(): return
        if parts[-1] == "message_templates":
            self._send(200, {"data": [{"name": n, "id": t["id"], "language": "en", "category": t["category"],
                                       "status": "APPROVED" if MOCK["approve"] else "PENDING"}
                                      for n, t in MOCK["templates"].get(parts[1], {}).items()]}); return
        self._send(200, {"display_phone_number": "+91 90000 0" + parts[1][-4:], "verified_name": "Test Gym " + parts[1][-3:],
                         "quality_rating": "GREEN", "id": parts[1]})

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self._auth_ok(): return
        if parts[-1] == "subscribed_apps":
            MOCK["subscribed"].append(parts[1]); self._send(200, {"success": True}); return
        if parts[-1] == "message_templates":
            tid = str(random.randint(10**14, 10**15))
            MOCK["templates"].setdefault(parts[1], {})[body["name"]] = {"id": tid, "category": body["category"], "body": body}
            self._send(200, {"id": tid, "status": "PENDING", "category": body["category"]}); return
        if parts[-1] == "messages":
            mode = MOCK["fail_next"]
            if mode == "transient":
                MOCK["fail_next"] = None
                self._send(500, {"error": {"message": "Internal error", "code": 131000}}); return
            if mode == "permanent":
                MOCK["fail_next"] = None
                self._send(400, {"error": {"message": "Recipient is not a valid WhatsApp user", "code": 131026}}); return
            wamid = "wamid." + uuid.uuid4().hex
            MOCK["sent"].append({"phone_number_id": parts[1], "wamid": wamid, "payload": body})
            self._send(200, {"messaging_product": "whatsapp", "messages": [{"id": wamid}]}); return
        self._send(200, {"success": True})


threading.Thread(target=HTTPServer(("127.0.0.1", 9099), Meta).serve_forever, daemon=True).start()


# ------------------------------------------------------------------ helpers
def H(t, idem=False, app="admin"):
    h = {"Authorization": f"Bearer {t}", "X-App": app, "X-App-Version": "1.0.0"}
    if idem: h["Idempotency-Key"] = str(uuid.uuid4())
    return h
def chk(n, r, code=None):
    ok = r.status_code == code if code else r.status_code < 400
    print("OK  " if ok else "FAIL", r.status_code, n, "" if ok else r.text[:300]); ok or fails.append(n)
    try: return r.json()
    except Exception: return None
def expect(n, got, want):
    ok = got == want; print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}"); ok or fails.append(n)
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))
def login(ph, app="web"):
    r = c.post(f"{B}/auth/otp/send", json={"phone": ph, "app": app}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
def webhook(payload, secret=SECRET):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return c.post(f"{B}/webhooks/whatsapp", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
def status_event(pid, wamid, st, err=None):
    s = {"id": wamid, "status": st, "timestamp": str(int(time.time())), "recipient_id": "91"}
    if err: s["errors"] = [{"code": err[0], "title": err[1]}]
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": pid}, "statuses": [s]}}]}]}
def wait_batch(T, G, bid, secs=40):
    for _ in range(secs * 2):
        b = c.get(f"{B}/gyms/{G}/whatsapp/batches/{bid}", headers=H(T)).json()
        if b.get("done"): return b
        time.sleep(0.5)
    return b

from sqlalchemy import text
from app.db import engine
from app.services import whatsapp as wa


# Clean slate, and a hard stop if this isn't a test database (this script writes test gyms and members).
if "test" not in (engine.url.database or ""):
    sys.exit(f"Refusing to run against database {engine.url.database!r} — point DATABASE_URL at a *test* database.")
with engine.begin() as cn:
    cn.execute(text("UPDATE wa_messages SET status='cancelled' WHERE status IN ('queued','sending')"))
    cn.execute(text("UPDATE gym_whatsapp SET status='disconnected', token_enc=NULL"))


def make_gym(name):
    T = login(rnd())
    G = chk(f"gym {name}", c.post(f"{B}/gyms", headers=H(T), json={"name": name, "city": "Hyderabad"}))["id"]
    c.patch(f"{B}/gyms/{G}", headers=H(T), json={"phone": rnd()})
    with engine.begin() as cn: cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
    plan = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 150000}).json()
    return T, G, plan


TA, GA, PA = make_gym("WA Gym A " + rnd()[-4:])
TB, GB, PB = make_gym("WA Gym B " + rnd()[-4:])
PID_A, WABA_A = "1" + rnd(), "2" + rnd()
PID_B, WABA_B = "1" + rnd(), "2" + rnd()

print("\n== connection")
d = chk("status before connect", c.get(f"{B}/gyms/{GA}/whatsapp", headers=H(TA)))
expect("starts disconnected", (d["connected"], d["status"]), (False, "disconnected"))
chk("connect with a bad token is refused", c.post(f"{B}/gyms/{GA}/whatsapp/connect", headers=H(TA), json={"waba_id": WABA_A, "phone_number_id": PID_A, "access_token": "BAD" + "x" * 40}), 422)
RAW_TOKEN = "EAAG" + uuid.uuid4().hex * 3
d = chk("connect", c.post(f"{B}/gyms/{GA}/whatsapp/connect", headers=H(TA), json={"waba_id": WABA_A, "phone_number_id": PID_A, "access_token": RAW_TOKEN, "consent_confirmed": True}))
expect("connected", (d["connected"], d["status"], bool(d["phone"])), (True, "connected", True))
expect("token never returned", RAW_TOKEN in json.dumps(d), False)
expect("webhooks subscribed for the account", WABA_A in MOCK["subscribed"], True)
with engine.connect() as cn: enc = cn.execute(text("SELECT token_enc FROM gym_whatsapp WHERE gym_id=:g"), {"g": GA}).scalar()
expect("token encrypted at rest", (RAW_TOKEN in enc, wa.decrypt(enc) == RAW_TOKEN), (False, True))
chk("another gym can't take the same number", c.post(f"{B}/gyms/{GB}/whatsapp/connect", headers=H(TB), json={"waba_id": WABA_B, "phone_number_id": PID_A, "access_token": RAW_TOKEN}), 409)
chk("gym B connects its own number", c.post(f"{B}/gyms/{GB}/whatsapp/connect", headers=H(TB), json={"waba_id": WABA_B, "phone_number_id": PID_B, "access_token": "EAAGB" + uuid.uuid4().hex * 3, "consent_confirmed": True}))
chk("test connection", c.post(f"{B}/gyms/{GA}/whatsapp/test", headers=H(TA)))
# this test is about reminders: switch the member-event messages (welcome, receipts…) off so counts stay exact
OFF = {k: False for k in ("welcome", "payment_received", "payment_failed", "membership_cancelled", "membership_frozen", "membership_resumed")}
for T_, G_ in ((TA, GA), (TB, GB)):
    chk("member-event messages off", c.patch(f"{B}/gyms/{G_}/whatsapp/settings", headers=H(T_), json={"automations": OFF}))

print("\n== members")
today = date.today()
def add(T, G, plan, name, days_left, phone=None):
    start = today - timedelta(days=29 - days_left)
    r = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": name, "phone": phone or rnd(), "membership": {"plan_id": plan["id"], "start_date": start.isoformat()}})
    if r.status_code >= 300: print("member fail", r.text[:200]); fails.append("add " + name)
    return r.json()["member"]
m3 = add(TA, GA, PA, "Asha Three", 3)
m1 = add(TA, GA, PA, "Bala One", 1)
m7 = add(TA, GA, PA, "Chitra Seven", 7)
m20 = add(TA, GA, PA, "Dev Twenty", 20)
mexp = add(TA, GA, PA, "Esha Expired", -1)
mB = add(TB, GB, PB, "Gym B Member", 2)
chk("upi account", c.post(f"{B}/gyms/{GA}/upi-accounts", headers=H(TA), json={"vpa": "gyma@okhdfcbank", "payee_name": "WA Gym A"}), 201)

print("\n== templates")
d = c.get(f"{B}/gyms/{GA}/whatsapp", headers=H(TA)).json()
expect("default templates are drafts", {t["status"] for t in d["templates"]}, {"draft"})
chk("template can't start with a variable", c.patch(f"{B}/gyms/{GA}/whatsapp/templates/expiry_reminder", headers=H(TA), json={"body": "{{member_name}} your membership at the gym is ending soon, please renew it now."}), 422)
chk("unknown variable rejected", c.patch(f"{B}/gyms/{GA}/whatsapp/templates/expiry_reminder", headers=H(TA), json={"body": "Hi {{member_name}}, your {{secret_field}} membership is ending soon, please renew."}), 422)
r = chk("reminders before approval", c.get(f"{B}/gyms/{GA}/whatsapp/reminders", headers=H(TA)))
expect("blocked until template approved", r["blocked"], "template_not_approved")
expect("expiring list = 7,3,1 day members", sorted(i["days_left"] for i in r["items"]), [1, 3, 7])
chk("send before approval refused", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TA), json={"event": "expiry_reminder", "view": "expiring"}), 409)
s = chk("submit all templates", c.post(f"{B}/gyms/{GA}/whatsapp/templates/submit-all", headers=H(TA)))
expect("all pending", ({t["status"] for t in s["items"]}, s["errors"]), ({"pending"}, {}))
sub = MOCK["templates"][WABA_A]["dmbl_expiry_reminder_v1"]["body"]
expect("submitted with numbered vars + url button", ("{{1}}" in sub["components"][0]["text"], sub["components"][1]["buttons"][0]["url"].endswith("/{{1}}"), "{{member_name}}" in sub["components"][0]["text"]), (True, True, False))
MOCK["approve"] = True
s = chk("sync after Meta approves", c.post(f"{B}/gyms/{GA}/whatsapp/templates/sync", headers=H(TA)))
expect("all approved", {t["status"] for t in s["items"]}, {"approved"})
c.post(f"{B}/gyms/{GB}/whatsapp/templates/submit-all", headers=H(TB)); c.post(f"{B}/gyms/{GB}/whatsapp/templates/sync", headers=H(TB))
p = chk("preview", c.post(f"{B}/gyms/{GA}/whatsapp/preview", headers=H(TA), json={"event": "expiry_reminder", "member_id": m3["id"]}))
expect("preview is personalised", ("Asha" in p["text"], "{{" in p["text"]), (True, False))

print("\n== bulk send")
MOCK["sent"].clear()
s = chk("send to everyone expiring", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TA), json={"event": "expiry_reminder", "view": "expiring"}), 202)
expect("3 queued", (s["queued"], len(s["skipped"])), (3, 0))
b = wait_batch(TA, GA, s["batch_id"])
expect("batch finished: 3 accepted, 0 failed", (b["done"], b["accepted"], b["failed"]), (True, 3, 0))
# welcome messages that were waiting for template approval also go out now; look at the expiry reminders only
rem = [x for x in MOCK["sent"] if "expiry_reminder" in str(x["payload"].get("template", {}).get("name", ""))]
expect("Meta got 3 expiry reminders, all from gym A's number", ([x["phone_number_id"] for x in rem], {x["payload"]["type"] for x in rem}), ([PID_A] * 3, {"template"}))
expect("everything sent went from gym A's number", {x["phone_number_id"] for x in MOCK["sent"]}, {PID_A})
asha = next(x for x in rem if x["payload"]["to"] == m3["phone"].lstrip("+"))
params = [p["text"] for p in asha["payload"]["template"]["components"][0]["parameters"]]
expect("body params personalised", params[0], "Asha")
TOKEN = asha["payload"]["template"]["components"][1]["parameters"][0]["text"]
expect("button carries a long random token, not an id", (len(TOKEN) >= 30, m3["id"] in TOKEN), (True, False))
s2 = chk("send again the same day", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TA), json={"event": "expiry_reminder", "view": "expiring"}), 202)
expect("all skipped as duplicates", (s2["queued"], {x["reason"] for x in s2["skipped"]}), (0, {"duplicate"}))
h = chk("history", c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA)))
expect("history shows sent, not delivered yet", ({m["status"] for m in h["items"]}, h["counts"]["delivered"]), ({"sent"}, 0))

print("\n== webhooks")
chk("webhook verify handshake", c.get(f"{B}/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "12345"}))
chk("wrong verify token", c.get(f"{B}/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}), 403)
chk("forged signature rejected", webhook(status_event(PID_A, asha["wamid"], "delivered"), secret="wrong"), 403)
chk("delivered receipt", webhook(status_event(PID_A, asha["wamid"], "delivered")))
row = lambda: next(m for m in c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA)).json()["items"] if m["member"]["id"] == m3["id"])
expect("now delivered", row()["status"], "delivered")
chk("read receipt", webhook(status_event(PID_A, asha["wamid"], "read")))
chk("late duplicate 'delivered'", webhook(status_event(PID_A, asha["wamid"], "delivered")))
expect("stays read (no downgrade)", row()["status"], "read")
other = next(x for x in MOCK["sent"] if x["wamid"] != asha["wamid"])
chk("receipt sent via another gym's number is ignored", webhook(status_event(PID_B, other["wamid"], "read")))
expect("cross-gym receipt changed nothing", next(m for m in c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA)).json()["items"] if m["phone"].endswith(other["payload"]["to"]))["status"], "sent")
chk("failed receipt", webhook(status_event(PID_A, other["wamid"], "failed", (131026, "Message undeliverable"))))
f = next(m for m in c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA), params={"status": "failed"}).json()["items"])
expect("failure reason kept", (f["status"], f["error_code"]), ("failed", "131026"))

print("\n== tenant isolation")
chk("gym B owner can't read gym A history", c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TB)), 403)
chk("gym B owner can't send for gym A", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TB), json={"event": "expiry_reminder", "view": "expiring"}), 403)
chk("gym A can't message gym B's member", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TA), json={"event": "expiry_reminder", "member_ids": [mB["id"]]}), 404)
chk("gym A batch id not visible to B", c.get(f"{B}/gyms/{GB}/whatsapp/batches/{s['batch_id']}", headers=H(TB)), 404)
expect("gym B history is empty", c.get(f"{B}/gyms/{GB}/whatsapp/messages", headers=H(TB)).json()["counts"]["all"], 0)

print("\n== opt-out")
inbound = {"object": "whatsapp_business_account", "entry": [{"id": WABA_A, "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": PID_A}, "messages": [{"from": m7["phone"].lstrip("+"), "type": "text", "text": {"body": "STOP"}}]}}]}]}
chk("member replies STOP", webhook(inbound))
r = c.get(f"{B}/gyms/{GA}/whatsapp/reminders", headers=H(TA)).json()
expect("member now opted out", next(i for i in r["items"] if i["member_id"] == m7["id"])["skip_reason"], "opted_out")
s = chk("manual send to opted-out member", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TA), json={"event": "expired", "member_ids": [m7["id"]]}), 202)
expect("skipped with reason", (s["queued"], s["skipped"][0]["reason"]), (0, "opted_out"))
chk("staff switch messages back on", c.post(f"{B}/gyms/{GA}/members/{m7['id']}/whatsapp-opt", headers=H(TA), json={"opted_out": False}))

print("\n== automatic reminders (scheduler job run directly)")
chk("enable settings", c.patch(f"{B}/gyms/{GA}/whatsapp/settings", headers=H(TA), json={"expiry_days_before": [7, 3, 1, 0], "expired_days_after": [1], "automations": {"expiry_reminder": True, "expired": True}}))
before = c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA)).json()["counts"]["all"]
n1 = wa.run_expiry_automation(force=True)
n2 = wa.run_expiry_automation(force=True)
n3 = wa.run_expiry_automation(force=True)
after = c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA)).json()
print("   queued per run:", n1, n2, n3)
expect("second and third runs queue nothing", (n2, n3), (0, 0))
auto = [m for m in after["items"] if m["trigger"] == "auto" and m["event"] in ("expiry_reminder", "expired")]
# Asha and Chitra were reminded by hand earlier today, so the automatic run doesn't message them again;
# Bala's hand-sent reminder failed, so Bala gets the automatic one; Esha (expired) wasn't reminded yet.
expect("auto reminders: once each, skipping members already reminded by hand today", sorted(m["member"]["name"] for m in auto if m["member"]["id"] in (m3["id"], m1["id"], m7["id"], mexp["id"], m20["id"])), ["Bala One", "Esha Expired"])
expect("20-days-left member gets no reminder", any(m["member"]["id"] == m20["id"] and m["event"] in ("expiry_reminder", "expired") for m in after["items"]), False)
live = [m for m in after["items"] if m["event"] == "gym_live" and m["status"] in ("queued", "sending", "sent", "delivered", "read")]
expect("the go-live message is banned: never sent", len(live), 0)

print("\n== retries")
MOCK["sent"].clear(); MOCK["fail_next"] = "transient"
wa.process_queue()
with engine.connect() as cn:
    st = cn.execute(text("SELECT status, attempts, next_attempt_at > UTC_TIMESTAMP() FROM wa_messages WHERE gym_id=:g AND `trigger`='auto' AND attempts>0 AND status='queued'"), {"g": GA}).fetchall()
expect("temporary Meta error → re-queued with a delay", (len(st), st[0][1] if st else None, bool(st[0][2]) if st else None), (1, 1, True))
with engine.begin() as cn: cn.execute(text("UPDATE wa_messages SET next_attempt_at=UTC_TIMESTAMP() WHERE gym_id=:g AND status='queued'"), {"g": GA})
MOCK["fail_next"] = "permanent"
wa.process_queue()
with engine.connect() as cn:
    perm = cn.execute(text("SELECT id, error_code FROM wa_messages WHERE gym_id=:g AND status='failed' AND error_code='131026' AND wamid IS NULL"), {"g": GA}).fetchall()
expect("permanent error → failed, not retried", len(perm), 1)
chk("owner retries the failed message", c.post(f"{B}/gyms/{GA}/whatsapp/messages/{perm[0][0]}/retry", headers=H(TA)))
time.sleep(3); wa.process_queue()
with engine.connect() as cn: st = cn.execute(text("SELECT status FROM wa_messages WHERE id=:i"), {"i": perm[0][0]}).scalar()
expect("retry went out", st, "sent")

print("\n== renewal link")
chk("made-up token", c.get(f"{B}/renew/{'x' * 32}"), 404)
v = chk("open my link", c.get(f"{B}/renew/{TOKEN}"))
expect("page shows my gym/plan, no ids or full phone", (v["member"]["first_name"], v["current"]["plan_name"], "id" in v["member"], m3["phone"] in json.dumps(v)), ("Asha", "Monthly", False, False))
st = chk("choose plan", c.post(f"{B}/renew/{TOKEN}/start", json={"plan_id": PA["id"]}), 201)
expect("UPI payment created for the plan price", (st["mode"], st["request"]["amount_paise"], st["request"]["status"]), ("upi", 150000, "pending"))
chk("plan from another gym refused", c.post(f"{B}/renew/{TOKEN}/start", json={"plan_id": PB["id"]}), 404)
utr = "".join(random.choices("0123456789", k=12))
cl = chk("submit UTR", c.post(f"{B}/renew/{TOKEN}/claim", json={"utr": utr}))
expect("waiting for the gym", cl["request"]["status"], "claimed")
ms_before = len(c.get(f"{B}/gyms/{GA}/members/{m3['id']}", headers=H(TA)).json().get("memberships", []))
expect("NOT renewed just because the member said so", c.get(f"{B}/renew/{TOKEN}").json()["renewed"], None)
chk("gym B can't verify gym A's payment", c.post(f"{B}/gyms/{GB}/upi-requests/{cl['request']['id']}/verify", headers=H(TB), json={}), 404)
chk("gym verifies the payment", c.post(f"{B}/gyms/{GA}/upi-requests/{cl['request']['id']}/verify", headers=H(TA), json={}))
v = c.get(f"{B}/renew/{TOKEN}").json()
expect("renewed after verification", (v["renewed"] or {}).get("plan_name"), "Monthly")
ms_after = len(c.get(f"{B}/gyms/{GA}/members/{m3['id']}", headers=H(TA)).json().get("memberships", []))
expect("exactly one new membership", ms_after - ms_before, 1)
chk("link can't renew twice", c.post(f"{B}/renew/{TOKEN}/start", json={"plan_id": PA["id"]}), 409)
stt = chk("stats", c.get(f"{B}/gyms/{GA}/whatsapp/stats", headers=H(TA)))
expect("renewal attributed to WhatsApp", (stt["renewals_month"], stt["renewal_revenue_paise"]), (1, 150000))
with engine.begin() as cn: cn.execute(text("UPDATE renewal_tokens SET expires_at = UTC_TIMESTAMP() - INTERVAL 1 DAY WHERE gym_id=:g AND used_at IS NULL"), {"g": GA})
other_tok = next(x for x in [asha] if x)["payload"]["template"]["components"][1]["parameters"][0]["text"]
bala_tok = None
with engine.connect() as cn: n_exp = cn.execute(text("SELECT COUNT(*) FROM renewal_tokens WHERE gym_id=:g AND expires_at < UTC_TIMESTAMP()"), {"g": GA}).scalar()
print("   expired tokens:", n_exp)
time.sleep(3); wa.process_queue()
conf = [m for m in c.get(f"{B}/gyms/{GA}/whatsapp/messages", headers=H(TA), params={"event": "renewal_confirmed"}).json()["items"]]
expect("renewal confirmation queued automatically", len(conf), 1)

print("\n== disconnect")
d = chk("disconnect", c.request("DELETE", f"{B}/gyms/{GA}/whatsapp", headers=H(TA)))
with engine.connect() as cn: enc = cn.execute(text("SELECT token_enc FROM gym_whatsapp WHERE gym_id=:g"), {"g": GA}).scalar()
expect("disconnected and token erased", (d["connected"], enc), (False, None))
chk("sending refused when disconnected", c.post(f"{B}/gyms/{GA}/whatsapp/send", headers=H(TA), json={"event": "expiry_reminder", "view": "expiring"}), 409)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
