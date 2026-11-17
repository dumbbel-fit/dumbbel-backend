"""WhatsApp down for a while: welcome / payment messages are kept, and go out when WhatsApp is back.

Runs the service directly against the TEST database with the WhatsApp gateway replaced by a fake, so nothing is sent.
    DATABASE_URL=...dumbbel_test python tests/wa_offline_test.py"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EVOLUTION_API_URL", "http://fake")
os.environ.setdefault("EVOLUTION_API_KEY", "x")
from sqlalchemy import func, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine, session_scope  # noqa: E402
from app.models import Gym, GymMember, GymWhatsApp, Membership, User, WaMessage, WaTemplate  # noqa: E402
from app.services import whatsapp as wa  # noqa: E402
from app.utils import today_in, utcnow, uuid7  # noqa: E402

assert "test" in engine.url.database, "refusing to run outside a test database"
fails = []


def expect(name, got, want):
    ok = got == want
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


sent = []
wa.evo = lambda method, path, **kw: (sent.append((kw.get("json") or {}).get("number")) or {"key": {"id": "fake-" + uuid7()}})
settings.wa_web_quiet_start, settings.wa_web_quiet_end = 24, 0
settings.wa_web_min_gap_sec = settings.wa_web_max_gap_sec = 0
from app.services import jobs  # noqa: E402
woken = []
_enqueue = jobs.enqueue
jobs.enqueue = lambda name, fn, *a, **k: woken.append(name) if name == "wa.send_now" else _enqueue(name, fn, *a, **k)


def drain(n=8):
    for _ in range(n):
        wa.process_queue()

tag = uuid7()[-8:]
digits = tag.translate(str.maketrans("abcdef", "123456"))
with session_scope() as db:
    owner = User(phone=f"+9195{digits[:8]}", name="Owner")
    db.add(owner); db.flush()
    for i in range(46656):
        cp = "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[(hash(tag) + i) // 36 ** k % 36] for k in range(3))
        if not db.scalar(select(Gym.id).where(Gym.code_prefix == cp)):
            break
    gym = Gym(name=f"Offline Test {tag}", code_prefix=cp, owner_user_id=owner.id, status="active")
    db.add(gym); db.flush()
    GID, today = gym.id, today_in(gym.timezone)

    def member(n, status="active"):
        u = User(phone=f"+9194{n}{digits[:7]}", name=f"M{n}")
        db.add(u); db.flush()
        gm = GymMember(gym_id=GID, user_id=u.id, member_code=f"{tag}{n}", display_name=f"Member {n}", joined_on=today, status=status)
        db.add(gm); db.flush()
        db.add(Membership(gym_id=GID, member_id=gm.id, plan_name="Monthly", price_paise=100000, start_date=today, end_date=today + timedelta(days=30)))
        return gm.id
    A, B, OLD = member(1), member(2), member(3)
    db.add(GymWhatsApp(gym_id=GID, provider="web", instance_name=wa.instance_name(GID), status="connected", connected_at=utcnow()))
    wa.ensure_templates(db, GID)


def rows(event=None):
    with session_scope() as db:
        q = select(WaMessage).where(WaMessage.gym_id == GID)
        if event:
            q = q.where(WaMessage.event == event)
        return {(m.event, m.member_id): (m.status, m.error_code) for m in db.scalars(q)}


def set_status(st):
    with session_scope() as db:
        db.get(GymWhatsApp, GID).status = st


print("== connected: sent normally")
wa.fire(GID, "welcome", A, dedupe=f"welcome:{A}")
expect("welcome queued at once", rows("welcome")[("welcome", A)], ("queued", None))
expect("the sender is woken at once, not on the next tick", woken.count("wa.send_now"), 1)

print("== link dropped: messages are kept, not lost")
set_status("error")
wa.fire(GID, "welcome", B, dedupe=f"welcome:{B}")
wa.fire(GID, "payment_received", B, extra={"amount": "₹1,000"}, dedupe=f"paid:{B}")
expect("welcome while down is kept as waiting", rows("welcome")[("welcome", B)], ("cancelled", "disconnected"))
expect("payment receipt while down is kept as waiting", rows("payment_received")[("payment_received", B)], ("cancelled", "disconnected"))
expect("nothing is sent while down", wa.process_queue(), 0)
expect("the queued one fails as not connected", rows("welcome")[("welcome", A)][1], "not_connected")
wa.fire(GID, "welcome", OLD, dedupe=f"welcome:{OLD}")
with session_scope() as db:
    old = db.scalar(select(WaMessage).where(WaMessage.gym_id == GID, WaMessage.member_id == OLD))
    old.created_at = utcnow() - timedelta(hours=30)
    db.add(WaMessage(id=uuid7(), gym_id=GID, member_id=OLD, phone="+919400000000", event="gym_live", category="UTILITY",
                     template_key="gym_live", body="x", status="cancelled", error_code="disconnected",
                     error="WhatsApp was disconnected", dedupe_key=f"live:{OLD}", trigger="auto",
                     created_at=utcnow() - timedelta(days=5)))
with session_scope() as db:   # B is archived while WhatsApp is down
    db.get(GymMember, B).status = "archived"

print("== WhatsApp back: waiting messages go out")
set_status("connected")
wa.onboard_gym(GID)
r = rows()
expect("failed welcome queued again", r[("welcome", A)], ("queued", None))
expect("waiting payment receipt queued again", r[("payment_received", B)], ("queued", None))
expect("welcome older than 24 h is not sent late", r[("welcome", OLD)], ("cancelled", "disconnected"))
expect("go-live message cancelled 5 days ago is queued again", r[("gym_live", OLD)], ("queued", None))
drain()
r = rows()
expect("A's welcome is sent", r[("welcome", A)][0], "sent")
expect("member archived meanwhile is not messaged", (r[("welcome", B)][0], r[("payment_received", B)][0]), ("cancelled", "cancelled"))
expect("go-live is sent", r[("gym_live", OLD)][0], "sent")
expect("revive is a no-op the second time", (wa.onboard_gym(GID), len(rows())), (0, len(r)))

print("== instant messages: during quiet hours, ahead of reminders, template made on demand")
settings.wa_web_quiet_start, settings.wa_web_quiet_end = 0, 0         # it is "night" all day
with session_scope() as db:
    db.query(WaTemplate).filter(WaTemplate.gym_id == GID, WaTemplate.key == "membership_cancelled").delete()
    c = db.get(GymWhatsApp, GID); c.next_send_at = None
wa.fire(GID, "expiry_reminder", A, dedupe=f"exp:{A}")
wa.fire(GID, "membership_cancelled", A, dedupe=f"cancelled:{A}")
expect("cancelled message queued although its template was missing", rows("membership_cancelled").get(("membership_cancelled", A), (None,))[0] in ("queued", "sent"), True)
drain()
r = rows()
expect("cancellation goes out at night", r[("membership_cancelled", A)][0], "sent")
expect("reminder waits for the morning", r[("expiry_reminder", A)][0], "queued")
for ev in ("welcome", "payment_received", "payment_failed", "membership_cancelled", "membership_frozen", "membership_resumed"):
    expect(f"{ev} is on by default", wa.DEFAULT_SETTINGS["automations"].get(ev), True)
settings.wa_web_quiet_start, settings.wa_web_quiet_end = 24, 0

print("== reminder sent by hand today is not sent again automatically")
with session_scope() as db:
    def ended(n):
        u = User(phone=f"+9193{n}{digits[:7]}", name=f"E{n}")
        db.add(u); db.flush()
        gm = GymMember(gym_id=GID, user_id=u.id, member_code=f"{tag}E{n}", display_name=f"Ended {n}", joined_on=today)
        db.add(gm); db.flush()
        db.add(Membership(gym_id=GID, member_id=gm.id, plan_name="Monthly", price_paise=100000,
                          start_date=today - timedelta(days=31), end_date=today - timedelta(days=1)))
        return gm.id
    HAND, NOHAND = ended(1), ended(2)
    db.add(WaMessage(id=uuid7(), gym_id=GID, member_id=HAND, phone="+919300000001", event="expired", category="UTILITY",
                     template_key="expired", body="x", status="sent", dedupe_key=f"manual:expired:{HAND}", trigger="manual"))
wa.run_expiry_automation(force=True)
with session_scope() as db:
    auto_for = lambda mid: db.scalar(select(func.count()).select_from(WaMessage).where(
        WaMessage.gym_id == GID, WaMessage.member_id == mid, WaMessage.event == "expired", WaMessage.trigger == "auto"))
    expect("member reminded by hand this morning: no automatic copy", auto_for(HAND), 0)
    expect("member not reminded yet: gets the automatic one", auto_for(NOHAND), 1)

print("== never linked: nothing is created")
with session_scope() as db:
    db.delete(db.get(GymWhatsApp, GID))
n = len(rows())
wa.fire(GID, "welcome", A, dedupe=f"welcome2:{A}")
expect("a gym without WhatsApp gets no rows", len(rows()), n)

print("\nFAILS:", fails or "none")
sys.exit(1 if fails else 0)
