"""WhatsApp via QR linking (Evolution API) — end-to-end against a MOCK Evolution server (no phone needed).

Start the API with:
    EVOLUTION_API_URL=http://localhost:9098 EVOLUTION_API_KEY=testkey EVOLUTION_WEBHOOK_BASE=http://localhost:8000 \
    WA_WEB_MIN_GAP_SEC=1 WA_WEB_MAX_GAP_SEC=2 AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev RUN_SCHEDULER=false \
    uvicorn app.main:app --port 8000
Then:  EVOLUTION_API_URL=http://localhost:9098 EVOLUTION_API_KEY=testkey python tests/whatsapp_web_e2e.py
"""
import json, os, random, sys, threading, time, uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails: list[str] = []
EVO = {"state": {}, "sent": [], "hooks": {}, "fail_next": None, "key": os.environ.get("EVOLUTION_API_KEY", "testkey")}


class Evo(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _ok(self):
        if self.headers.get("apikey") != EVO["key"]:
            self._send(401, {"status": 401, "error": "Unauthorized", "response": {"message": "Unauthorized"}}); return False
        return True

    def do_GET(self):
        if not self._ok(): return
        u = urlparse(self.path); parts = u.path.strip("/").split("/")
        if parts[:2] == ["instance", "connectionState"]:
            self._send(200, {"instance": {"instanceName": parts[2], "state": EVO["state"].get(parts[2], "close")}}); return
        if parts[:2] == ["instance", "connect"]:
            q = parse_qs(u.query)
            self._send(200, {"pairingCode": "ABCD1234" if q.get("number") else None, "code": "2@x", "base64": "data:image/png;base64,iVBORw0KGgo=", "count": 1}); return
        if parts[:2] == ["instance", "fetchInstances"]:
            name = parse_qs(u.query)["instanceName"][0]
            self._send(200, [{"name": name, "ownerJid": EVO.get("owner", "919876500001") + "@s.whatsapp.net", "profileName": "Iron Gym Desk", "connectionStatus": EVO["state"].get(name)}]); return
        self._send(404, {"status": 404, "response": {"message": ["not found"]}})

    def do_POST(self):
        if not self._ok(): return
        parts = urlparse(self.path).path.strip("/").split("/")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if parts == ["instance", "create"]:
            name = body["instanceName"]
            if name in EVO["hooks"]:
                self._send(403, {"status": 403, "response": {"message": [f'This name "{name}" is already in use.']}}); return
            EVO["hooks"][name] = body["webhook"]; EVO["state"][name] = "connecting"
            self._send(201, {"instance": {"instanceName": name, "status": "created"}, "hash": "x", "qrcode": {"base64": "data:image/png;base64,iVBORw0KGgo=", "pairingCode": None, "count": 1}}); return
        if parts[:2] == ["webhook", "set"]:
            EVO["hooks"][parts[2]] = body["webhook"]; self._send(201, {"ok": True}); return
        if parts[:2] == ["message", "sendText"]:
            name = parts[2]
            if EVO["state"].get(name) != "open":
                self._send(400, {"status": 400, "response": {"message": ["Connection Closed"]}}); return
            if EVO["fail_next"] == "notwa":
                EVO["fail_next"] = None
                self._send(400, {"status": 400, "response": {"message": [{"jid": body["number"] + "@s.whatsapp.net", "exists": False, "number": body["number"]}]}}); return
            mid = "3EB0" + uuid.uuid4().hex[:16].upper()
            EVO["sent"].append({"instance": name, "id": mid, "number": body["number"], "text": body["text"], "at": time.time()})
            self._send(201, {"key": {"remoteJid": body["number"] + "@s.whatsapp.net", "fromMe": True, "id": mid}, "status": "PENDING", "messageType": "conversation"}); return
        self._send(404, {"status": 404, "response": {"message": ["not found"]}})

    def do_DELETE(self):
        if not self._ok(): return
        parts = urlparse(self.path).path.strip("/").split("/")
        name = parts[2]
        if parts[1] == "logout": EVO["state"][name] = "close"
        if parts[1] == "delete": EVO["hooks"].pop(name, None); EVO["state"].pop(name, None)
        self._send(200, {"status": "SUCCESS"})


threading.Thread(target=HTTPServer(("127.0.0.1", 9098), Evo).serve_forever, daemon=True).start()


def H(t, idem=False):
    h = {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}
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
def login(ph):
    r = c.post(f"{B}/auth/otp/send", json={"phone": ph, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
def hook(name, event, data, token=None):
    h = EVO["hooks"].get(name, {}).get("headers", {})
    return c.post(EVO["hooks"].get(name, {}).get("url", f"{B}/webhooks/evolution"), json={"event": event, "instance": name, "data": data},
                  headers={"x-dumbbel-token": token if token is not None else h.get("x-dumbbel-token", "")})

from sqlalchemy import text
from app.db import engine
from app.services import whatsapp as wa
from app.config import settings
if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
with engine.begin() as cn:
    cn.execute(text("UPDATE wa_messages SET status='cancelled' WHERE status IN ('queued','sending')"))

T = login(rnd())
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": "Web Gym " + rnd()[-4:], "city": "Hyderabad"}))["id"]
c.patch(f"{B}/gyms/{G}", headers=H(T), json={"phone": rnd()})
with engine.begin() as cn: cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
P = c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 150000}).json()
c.post(f"{B}/gyms/{G}/upi-accounts", headers=H(T), json={"vpa": "webgym@okhdfcbank", "payee_name": "Web Gym"})
NAME = "dmbl-" + G

print("\n== link by QR")
L = chk("start link", c.post(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T), json={"risk_ack": True, "consent_confirmed": True}))
expect("QR shown, not connected yet", (bool(L["qr_base64"]), L["connected"]), (True, False))
expect("webhook registered with a secret header", bool(EVO["hooks"][NAME]["headers"].get("x-dumbbel-token")), True)
p = chk("pairing-code option", c.get(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T), params={"pairing_phone": "9876500001"}))
expect("8-char pairing code", p["pairing_code"], "ABCD1234")
EVO["state"][NAME] = "open"
s = chk("phone scanned → poll", c.get(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T)))
expect("connected with the phone's number", (s["connected"], s["phone"]), (True, "+919876500001"))
d = c.get(f"{B}/gyms/{G}/whatsapp", headers=H(T)).json()
expect("provider web, templates ready without Meta review", (d["provider"], {t["status"] for t in d["templates"]}), ("web", {"ready"}))
L2 = chk("re-link is idempotent (instance already exists)", c.post(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T), json={"risk_ack": True}))

print("\n== webhook security")
chk("wrong token rejected", hook(NAME, "connection.update", {"state": "close", "statusReason": 401}, token="nope"), 403)
chk("unknown instance rejected", c.post(f"{B}/webhooks/evolution", json={"event": "connection.update", "instance": "someone-else", "data": {}}), 403)

print("\n== members + sending")
today = date.today()
def add(name, days_left):
    start = today - timedelta(days=29 - days_left)
    return c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": name, "phone": rnd(), "membership": {"plan_id": P["id"], "start_date": start.isoformat()}}).json()["member"]
ms = [add(n, dl) for n, dl in [("Anu One", 1), ("Bharat Two", 2), ("Chetan Three", 3)]]
pv = chk("preview", c.post(f"{B}/gyms/{G}/whatsapp/preview", headers=H(T), json={"event": "expiry_reminder", "member_id": ms[0]["id"]}))
expect("preview has the link inline (no button)", ("Renew membership: https://" in pv["text"], pv["button_text"]), (True, None))
s = chk("send to all expiring", c.post(f"{B}/gyms/{G}/whatsapp/send", headers=H(T), json={"event": "expiry_reminder", "view": "expiring"}), 202)
expect("3 queued", s["queued"], 3)
deadline = time.time() + 60
while time.time() < deadline and len([x for x in EVO["sent"] if x["instance"] == NAME]) < 3:
    wa.process_queue(); time.sleep(0.5)
sent = [x for x in EVO["sent"] if x["instance"] == NAME]
expect("all 3 sent through the linked number", len(sent), 3)
gaps = [round(b["at"] - a["at"], 1) for a, b in zip(sent, sent[1:])]
print("   gaps between messages (s):", gaps)
expect("messages are spaced out, not burst", all(g >= 0.9 for g in gaps), True)
m0 = sent[0]
expect("personal text with link", ("Renew membership: https://" in m0["text"], "{{" in m0["text"]), (True, False))
tok = m0["text"].split("/renew/")[1].split()[0]
chk("renewal link from the text works", c.get(f"{B}/renew/{tok}"))

print("\n== receipts & replies")
chk("delivered", hook(NAME, "messages.update", {"keyId": m0["id"], "status": "DELIVERY_ACK", "fromMe": True}))
chk("read", hook(NAME, "messages.update", {"keyId": m0["id"], "status": "READ", "fromMe": True}))
chk("late server ack", hook(NAME, "messages.update", {"keyId": m0["id"], "status": "SERVER_ACK", "fromMe": True}))
h = c.get(f"{B}/gyms/{G}/whatsapp/messages", headers=H(T)).json()
st = {m["phone"].lstrip("+"): m["status"] for m in h["items"]}
expect("read, no downgrade", st[m0["number"]], "read")
chk("member replies STOP", hook(NAME, "messages.upsert", {"key": {"remoteJid": sent[1]["number"] + "@s.whatsapp.net", "fromMe": False, "id": "X1"}, "message": {"conversation": "Stop"}}))
r = c.get(f"{B}/gyms/{G}/whatsapp/reminders", headers=H(T)).json()
expect("member opted out", sorted(i["skip_reason"] or "-" for i in r["items"]).count("opted_out"), 1)
chk("group messages ignored", hook(NAME, "messages.upsert", {"key": {"remoteJid": "1203@g.us", "fromMe": False}, "message": {"conversation": "STOP"}}))

print("\n== not on WhatsApp / unlinked")
m4 = add("Dinesh Four", 4)
EVO["fail_next"] = "notwa"
s = chk("send to one member", c.post(f"{B}/gyms/{G}/whatsapp/send", headers=H(T), json={"event": "expiry_reminder", "member_ids": [m4["id"]]}), 202)
time.sleep(2.5); wa.process_queue()
f = c.get(f"{B}/gyms/{G}/whatsapp/messages", headers=H(T), params={"status": "failed"}).json()["items"]
expect("failed with a clear reason", (len(f), f[0]["error"] if f else None), (1, "This number isn’t on WhatsApp."))
chk("phone unlinks the device", hook(NAME, "connection.update", {"state": "close", "statusReason": 401}))
d = c.get(f"{B}/gyms/{G}/whatsapp", headers=H(T)).json()
expect("gym sees it needs relinking", (d["status"], "unlinked" in (d["last_error"] or "")), ("error", True))
chk("sending blocked while unlinked", c.post(f"{B}/gyms/{G}/whatsapp/send", headers=H(T), json={"event": "expiry_reminder", "view": "expiring"}), 409)
chk("phone reconnects", hook(NAME, "connection.update", {"state": "open", "wuid": "919876500001@s.whatsapp.net"}))
expect("connected again", c.get(f"{B}/gyms/{G}/whatsapp", headers=H(T)).json()["status"], "connected")

print("\n== quiet hours & daily cap (worker rules)")
from app.db import session_scope
from app.models import Gym, GymWhatsApp
with session_scope() as db:
    cfg, gym = db.get(GymWhatsApp, G), db.get(Gym, G)
    old = (settings.wa_web_quiet_start, settings.wa_web_quiet_end, settings.wa_web_daily_limit)
    settings.wa_web_quiet_start, settings.wa_web_quiet_end = 0, 23  # pretend it's night
    from app.utils import tz, utcnow
    now_h = utcnow().astimezone(tz(gym.timezone)).hour
    hold_auto = wa._web_hold(db, cfg, gym, "auto")
    hold_manual = wa._web_hold(db, cfg, gym, "manual")
    expect("automatic messages wait for morning, manual ones don't", (hold_auto is not None or now_h == 23, hold_manual), (True, None))
    settings.wa_web_quiet_start, settings.wa_web_quiet_end = 23, 0
    settings.wa_web_daily_limit = 1
    expect("daily cap reached → held until tomorrow", wa._web_hold(db, cfg, gym, "manual") is not None, True)
    settings.wa_web_quiet_start, settings.wa_web_quiet_end, settings.wa_web_daily_limit = old

print("\n== disconnect")
d = chk("disconnect", c.request("DELETE", f"{B}/gyms/{G}/whatsapp", headers=H(T)))
expect("device unlinked & instance removed", (d["connected"], NAME in EVO["hooks"]), (False, False))
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
