"""After the link server was down, messages that backed off (next try up to 2 h away) go out as soon as the link
works again: on the watcher's next check and on a fresh link event. Quiet-hour holds and old messages stay put.
Run against a TEST database:  DATABASE_URL=...dumbbel_test python tests/wa_wake_after_outage_test.py"""
import os
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import engine, session_scope  # noqa: E402
from app.models import Gym, GymMember, GymWhatsApp, User, WaMessage  # noqa: E402
from app.services import whatsapp as wa  # noqa: E402
from app.utils import utcnow, uuid7  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
fails = []


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


def setup():
    with session_scope() as db:
        u = User(phone="+91" + str(uuid.uuid4().int)[:10].replace("0", "9").rjust(10, "9")[:10], name="Owner")
        db.add(u); db.flush()
        g = Gym(name="Wake Test", code_prefix=uuid.uuid4().hex[:3].upper(), owner_user_id=u.id, status="active", phone="+919000000001")
        db.add(g); db.flush()
        m = GymMember(gym_id=g.id, user_id=u.id, member_code=f"WK-{uuid.uuid4().hex[:6]}", display_name="Vishnu",
                      status="active", joined_on=utcnow().date())
        db.add(m)
        db.add(GymWhatsApp(gym_id=g.id, provider="web", instance_name=wa.instance_name(g.id), status="connected"))
        db.flush()
        now = utcnow()
        ids = {}
        for key, err, nxt, age in [("backed_off", "", now + timedelta(hours=2), 1), ("quiet_hold", None, now + timedelta(hours=10), 1),
                                   ("old", "", now + timedelta(hours=2), 30)]:
            mid = uuid7()
            db.add(WaMessage(id=mid, gym_id=g.id, member_id=m.id, phone="917674802158", event="payment_received",
                             category="UTILITY", template_key="payment_received", body="x", status="queued",
                             error_code=err, error="The WhatsApp link service isn’t reachable" if err is not None else None,
                             attempts=4, next_attempt_at=nxt, created_at=now - timedelta(hours=age), trigger="auto"))
            ids[key] = mid
        return g.id, ids


def due(ids):
    with session_scope() as db:
        now = utcnow() + timedelta(seconds=5)
        return {k: db.get(WaMessage, v).next_attempt_at.replace(tzinfo=None) <= now.replace(tzinfo=None) for k, v in ids.items()}


print("== watcher sees the link open again (gym never looked disconnected)")
G, ids = setup()
wa.web_state = lambda gid: "open"
wa.web_available = lambda: True
wa._watch_one(G)
expect("only the backed-off message is due now", due(ids), {"backed_off": True, "quiet_hold": False, "old": False})

print("\n== a fresh link event does the same")
G, ids = setup()
wa.web_profile = lambda gid: {}
import app.services.jobs as jobs  # noqa: E402
jobs.enqueue = lambda *a, **k: None
wa.handle_evolution({"event": "connection.update", "instance": wa.instance_name(G), "data": {"state": "open"}}, G)
expect("backed-off message is due now", due(ids)["backed_off"], True)

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
