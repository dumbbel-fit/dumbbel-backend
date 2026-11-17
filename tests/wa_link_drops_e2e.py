"""WhatsApp linked numbers must not look "unlinked" because of a short drop, and the app must never throw away a
working session. Against a MOCK Evolution server (no phone needed):

  * a drop (any close reason except a logout) keeps the gym connected and marks it reconnecting
  * a message that hits a closed socket waits and is sent once the link is back (nothing cancelled)
  * the link watcher asks Evolution to reconnect, and marks the gym back online when it is
  * only a link still down after the grace period (or a session that is gone) is shown as unlinked
  * a gym shown as unlinked comes back by itself when the session reconnects
  * "Link again" on a connected gym never logs out / deletes its session
  * if Evolution can't be reached, nothing is changed

Start the API exactly like tests/whatsapp_web_e2e.py, then run this with the same EVOLUTION_* / WA_WEB_* env."""
import json
import os
import random
import sys
import threading
import time
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails: list[str] = []
EVO = {"state": {}, "sent": [], "hooks": {}, "deletes": [], "connects": 0, "reconnect_on_connect": False,
       "key": os.environ.get("EVOLUTION_API_KEY", "testkey")}
PHONE = "91987650" + str(random.randint(1000, 9999))


class Evo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.headers.get("apikey") != EVO["key"]:
            return self._send(401, {"status": 401})
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:2] == ["instance", "connectionState"]:
            if parts[2] not in EVO["state"]:
                return self._send(404, {"status": 404, "response": {"message": ["The instance does not exist"]}})
            return self._send(200, {"instance": {"instanceName": parts[2], "state": EVO["state"][parts[2]]}})
        if parts[:2] == ["instance", "connect"]:
            EVO["connects"] += 1
            if EVO["reconnect_on_connect"]:
                EVO["state"][parts[2]] = "open"   # the saved session reconnects without a scan
            return self._send(200, {"pairingCode": None, "code": "2@x", "base64": "data:image/png;base64,iVBORw0KGgo=", "count": 1})
        if parts[:2] == ["instance", "fetchInstances"]:
            return self._send(200, [{"ownerJid": PHONE + "@s.whatsapp.net", "profileName": "Drop Gym Desk"}])
        self._send(404, {"status": 404, "response": {"message": ["not found"]}})

    def do_POST(self):
        if self.headers.get("apikey") != EVO["key"]:
            return self._send(401, {"status": 401})
        parts = urlparse(self.path).path.strip("/").split("/")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if parts == ["instance", "create"]:
            name = body["instanceName"]
            if name in EVO["state"]:
                return self._send(403, {"status": 403, "response": {"message": [f'This name "{name}" is already in use.']}})
            EVO["hooks"][name] = body["webhook"]
            EVO["state"][name] = "connecting"
            return self._send(201, {"instance": {"instanceName": name}, "qrcode": {"base64": "data:image/png;base64,iVBORw0KGgo=", "pairingCode": None}})
        if parts[:2] == ["webhook", "set"]:
            EVO["hooks"][parts[2]] = body["webhook"]
            return self._send(201, {"ok": True})
        if parts[:2] == ["message", "sendText"]:
            if EVO["state"].get(parts[2]) != "open":
                return self._send(400, {"status": 400, "response": {"message": ["Connection Closed"]}})
            mid = "3EB0" + uuid.uuid4().hex[:16].upper()
            EVO["sent"].append({"instance": parts[2], "id": mid, "number": body["number"], "text": body["text"]})
            return self._send(201, {"key": {"id": mid, "fromMe": True}, "status": "PENDING"})
        self._send(404, {"status": 404})

    def do_DELETE(self):
        if self.headers.get("apikey") != EVO["key"]:
            return self._send(401, {"status": 401})
        parts = urlparse(self.path).path.strip("/").split("/")
        EVO["deletes"].append(parts[1])
        if parts[1] == "logout":
            EVO["state"][parts[2]] = "close"
        if parts[1] == "delete":
            EVO["state"].pop(parts[2], None)
            EVO["hooks"].pop(parts[2], None)
        self._send(200, {"status": "SUCCESS"})


threading.Thread(target=HTTPServer(("127.0.0.1", 9098), Evo).serve_forever, daemon=True).start()


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


rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))


def login(ph):
    r = c.post(f"{B}/auth/otp/send", json={"phone": ph, "app": "web"}).json()
    return c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]


def hook(name, event, data):
    h = EVO["hooks"][name]
    return c.post(h["url"], json={"event": event, "instance": name, "data": data},
                  headers={"x-dumbbel-token": h["headers"]["x-dumbbel-token"]})


from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.services import whatsapp as wa  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
with engine.begin() as cn:
    cn.execute(text("UPDATE wa_messages SET status='cancelled' WHERE status IN ('queued','sending')"))
settings.wa_web_quiet_start, settings.wa_web_quiet_end = 24, 0   # no quiet hours: this test may run at night

T = login(rnd())
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": "Drop Gym " + rnd()[-4:], "city": "Hyderabad"}))["id"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active' WHERE id=:g"), {"g": G})
NAME = "dmbl-" + G


def status():
    return c.get(f"{B}/gyms/{G}/whatsapp", headers=H(T)).json()


def db_msgs():
    with engine.connect() as cn:
        return [dict(r._mapping) for r in cn.execute(text(
            "SELECT id, status, error_code, event FROM wa_messages WHERE gym_id=:g ORDER BY created_at"), {"g": G})]


def drain(until, secs=40):
    end = time.time() + secs
    while time.time() < end and not until():
        with engine.begin() as cn:  # skip the 2-minute wait for the test
            cn.execute(text("UPDATE wa_messages SET next_attempt_at=UTC_TIMESTAMP() WHERE gym_id=:g AND status='queued'"), {"g": G})
            cn.execute(text("UPDATE gym_whatsapp SET next_send_at=NULL WHERE gym_id=:g"), {"g": G})
        wa.process_queue()
        time.sleep(0.5)


print("\n== link")
chk("start link", c.post(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T), json={"risk_ack": True, "consent_confirmed": True}))
EVO["state"][NAME] = "open"
expect("linked", chk("poll", c.get(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T)))["connected"], True)

print("\n== a short drop is not an unlink")
EVO["state"][NAME] = "close"
chk("link drops (connection closed, 428)", hook(NAME, "connection.update", {"state": "close", "statusReason": 428}))
d = status()
expect("still connected, reconnecting", (d["status"], d["connected"], d["reconnecting"]), ("connected", True, True))
m = chk("add a member while it's down (welcome message)", c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Ravi Drop", "phone": rnd()}), 201)
mid = m["member"]["id"]
s = chk("send a message while it's down", c.post(f"{B}/gyms/{G}/whatsapp/send", headers=H(T), json={"event": "welcome", "member_ids": [mid]}), 202)
time.sleep(1)
drain(lambda: False, secs=3)
msgs = db_msgs()
expect("nothing cancelled or failed, all waiting", {x["status"] for x in msgs}, {"queued"})
expect("the one tried says it waits for the reconnect", "reconnecting" in {x["error_code"] for x in msgs}, True)
expect("gym still connected after the failed send", status()["status"], "connected")

print("\n== watcher reconnects it")
EVO["reconnect_on_connect"] = True
before = EVO["connects"]
wa.watch_links()
expect("watcher asked Evolution to reconnect", EVO["connects"] > before, True)
expect("session is open again", EVO["state"][NAME], "open")
wa.watch_links()
d = status()
expect("back to plain connected", (d["status"], d["reconnecting"]), ("connected", False))
drain(lambda: all(x["status"] in ("sent", "delivered", "read") for x in db_msgs()))
expect("the waiting messages went out", {x["status"] for x in db_msgs()}, {"sent"})
EVO["reconnect_on_connect"] = False

print("\n== reconnect via the webhook clears the flag too")
hook(NAME, "connection.update", {"state": "close", "statusReason": 408})
expect("reconnecting", status()["reconnecting"], True)
hook(NAME, "connection.update", {"state": "open", "wuid": PHONE + "@s.whatsapp.net"})
expect("open event → connected, not reconnecting", (status()["status"], status()["reconnecting"]), ("connected", False))

print("\n== Evolution unreachable: nothing changes")
EVO["state"][NAME] = "close"
hook(NAME, "connection.update", {"state": "close", "statusReason": 503})
with engine.begin() as cn:
    cn.execute(text("UPDATE gym_whatsapp SET link_lost_at = UTC_TIMESTAMP() - INTERVAL 30 MINUTE WHERE gym_id=:g"), {"g": G})
real_url = settings.evolution_api_url
settings.evolution_api_url = "http://127.0.0.1:9"   # nothing listens there
wa.watch_links()
settings.evolution_api_url = real_url
expect("not flagged while we can't check", status()["status"], "connected")

print("\n== a long outage is flagged, and recovers by itself")
with engine.begin() as cn:
    cn.execute(text("UPDATE gym_whatsapp SET link_lost_at = UTC_TIMESTAMP() - INTERVAL 5 MINUTE WHERE gym_id=:g"), {"g": G})
wa.watch_links()
expect("within the grace period: still connected", status()["status"], "connected")
with engine.begin() as cn:
    cn.execute(text("UPDATE gym_whatsapp SET link_lost_at = UTC_TIMESTAMP() - INTERVAL 20 MINUTE WHERE gym_id=:g"), {"g": G})
wa.watch_links()
d = status()
expect("down 20 min: shown as needing a scan", (d["status"], "unlinked" in (d["last_error"] or "")), ("error", True))
expect("session was not deleted by us", "delete" in EVO["deletes"] or "logout" in EVO["deletes"], False)
EVO["state"][NAME] = "open"
wa.watch_links()
d = status()
expect("session came back → connected again, no scan", (d["status"], d["last_error"], d["reconnecting"]), ("connected", None, False))

print("\n== 'Link again' on a connected gym never removes its session")
EVO["state"][NAME] = "connecting"   # e.g. reconnecting right now
L = chk("link again with a pairing code", c.post(f"{B}/gyms/{G}/whatsapp/web/link", headers=H(T),
                                                 json={"risk_ack": True, "pairing_phone": PHONE[-10:]}))
expect("no logout / delete sent to Evolution", EVO["deletes"], [])
expect("instance still exists", NAME in EVO["state"], True)

print("\n== session gone from the link server")
EVO["state"][NAME] = "open"
wa.watch_links()
EVO["state"].pop(NAME)
hook_url = EVO["hooks"][NAME]
wa.watch_links()
d = status()
expect("gone → needs a scan", (d["status"], "unlinked" in (d["last_error"] or "")), ("error", True))

print("\n== a logout on the phone is still shown straight away")
EVO["state"][NAME] = "open"
wa.watch_links()
expect("connected", status()["status"], "connected")
hook(NAME, "connection.update", {"state": "close", "statusReason": 401})
expect("logged out (401) → needs a scan", status()["status"], "error")

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
