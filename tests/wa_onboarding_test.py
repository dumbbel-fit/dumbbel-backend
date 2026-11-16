"""The one-time "your gym is on DUMBBEL.fit" WhatsApp message: who gets it, exactly once, paced, behind reminders.

Runs the service directly against the TEST database with the WhatsApp gateway replaced by a fake, so nothing is sent.
    DATABASE_URL=...dumbbel_test EVOLUTION_API_URL=http://fake EVOLUTION_API_KEY=x python tests/wa_onboarding_test.py"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EVOLUTION_API_URL", "http://fake")
os.environ.setdefault("EVOLUTION_API_KEY", "x")
from sqlalchemy import select, text  # noqa: E402

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


sent_texts = []
wa.evo = lambda method, path, **kw: (sent_texts.append((kw.get("json") or {}).get("text", "")) or {"key": {"id": "fake-" + uuid7()}})
settings.wa_web_quiet_start, settings.wa_web_quiet_end = 24, 0   # no quiet hours in this test
settings.wa_web_min_gap_sec, settings.wa_web_max_gap_sec = 20, 20
from app.services import jobs  # noqa: E402
_enqueue = jobs.enqueue   # this test drives the sender itself: don't let "send now" wake-ups run in the background
jobs.enqueue = lambda name, fn, *a, **k: None if name == "wa.send_now" else _enqueue(name, fn, *a, **k)

tag = uuid7()[-8:]
with session_scope() as db:
    owner = User(phone=f"+9197{tag[:8].translate(str.maketrans('abcdef', '123456'))}", name="Owner")
    db.add(owner); db.flush()
    gym = Gym(name=f"Live Test {tag}", code_prefix=None, owner_user_id=owner.id, status="active", phone="+919000000001")
    # code_prefix is unique and 3 chars: derive one that is free
    for i in range(46656):
        cp = "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[(hash(tag) + i) // 36 ** k % 36] for k in range(3))
        if not db.scalar(select(Gym.id).where(Gym.code_prefix == cp)):
            gym.code_prefix = cp
            break
    db.add(gym); db.flush()
    GID = gym.id
    today = today_in(gym.timezone)

    def member(name, end_in, status="active", opted_out=False, phone_ok=True, i=[0]):
        i[0] += 1
        u = User(phone=(f"+9198{i[0]:02d}{tag[:6].translate(str.maketrans('abcdef', '123456'))}" if phone_ok else f"deleted-{tag}-{i[0]}"), name=name)
        db.add(u); db.flush()
        gm = GymMember(gym_id=GID, user_id=u.id, member_code=f"{tag}{i[0]}", display_name=name, joined_on=today - timedelta(days=200),
                       status=status, wa_opted_out_at=utcnow() if opted_out else None)
        db.add(gm); db.flush()
        if end_in is not None:
            db.add(Membership(gym_id=GID, member_id=gm.id, plan_name="Monthly", price_paise=150000,
                              start_date=today + timedelta(days=end_in - 30), end_date=today + timedelta(days=end_in), state="active"))
        return gm.id

    M = {"current": member("Asha Rao", 20), "ends_today": member("Bala K", 0), "lapsed_10": member("Chitra", -10),
         "lapsed_60": member("Dinesh", -60), "archived": member("Esha", 20, status="archived"),
         "opted_out": member("Farid", 20, opted_out=True), "no_phone": member("Gita", 20, phone_ok=False),
         "no_membership": member("Hari", None)}
    db.add(GymWhatsApp(gym_id=GID, provider="web", instance_name=wa.instance_name(GID), status="connected"))
    wa.ensure_templates(db, GID)


def live_rows():
    with session_scope() as db:
        return {m.member_id: m for m in db.scalars(select(WaMessage).where(WaMessage.gym_id == GID, WaMessage.event == "gym_live"))}


print("== who gets it")
expect("first run queues the 3 eligible members", wa.onboard_gym(GID), 3)
rows = live_rows()
expect("current, ending today and lapsed 10 days ago are queued", set(rows), {M["current"], M["ends_today"], M["lapsed_10"]})
expect("lapsed 60 days, archived, opted out, no phone, no membership are left out",
       [k for k in ("lapsed_60", "archived", "opted_out", "no_phone", "no_membership") if M[k] in rows], [])
body = rows[M["current"]].body
expect("message has the name, gym, app link and STOP line",
       all(x in body for x in ("Hi Asha", f"Live Test {tag}", settings.member_app_url, "Reply STOP")), True)
expect("no unfilled variable is left in the text", "{{" in body, False)
times = sorted(r.next_attempt_at for r in rows.values())
expect("messages are spread 20 s apart, not sent in one burst", [round((b - a).total_seconds()) for a, b in zip(times, times[1:])], [20, 20])

print("== exactly once")
expect("second run queues nothing", wa.onboard_gym(GID), 0)
expect("hourly automation queues nothing new", (wa.run_expiry_automation(), len(live_rows()))[1], 3)
with session_scope() as db:
    gm = db.get(GymMember, M["no_membership"])
    db.add(Membership(gym_id=GID, member_id=gm.id, plan_name="Monthly", price_paise=150000, start_date=today, end_date=today + timedelta(days=30), state="active"))
expect("a member who joins later gets it on the next run", wa.onboard_gym(GID), 1)
expect("still one message per member", len(live_rows()), 4)

print("== added by hand: sent straight away, once")
with session_scope() as db:
    u = User(phone=f"+9196{tag[:8].translate(str.maketrans('abcdef', '123456'))}", name="Indu New")
    db.add(u); db.flush()
    gm = GymMember(gym_id=GID, user_id=u.id, member_code=f"{tag}N", display_name="Indu New", joined_on=today)
    db.add(gm); db.flush()
    NEW = gm.id
wa.fire(GID, "gym_live", NEW, None, dedupe=f"live:{NEW}")
expect("a member added without a plan is queued at once", NEW in live_rows(), True)
expect("their message has no unfilled variable", "{{" in live_rows()[NEW].body, False)
wa.fire(GID, "gym_live", NEW, None, dedupe=f"live:{NEW}")
with session_scope() as db:
    db.add(Membership(gym_id=GID, member_id=NEW, plan_name="Monthly", price_paise=150000, start_date=today, end_date=today + timedelta(days=30), state="active"))
expect("firing again, a later plan and the hourly run add nothing", (wa.onboard_gym(GID), wa.run_expiry_automation(), len(live_rows()))[2], 5)
with session_scope() as db:
    db.execute(text("DELETE FROM memberships WHERE member_id=:m"), {"m": NEW})
    db.get(GymMember, NEW).status = "archived"

print("== switch and connection")
with session_scope() as db:
    db.execute(text("DELETE FROM wa_messages WHERE gym_id=:g"), {"g": GID})
    cfg = db.get(GymWhatsApp, GID)
    cfg.settings = {"automations": {"gym_live": False}}
expect("switched off: nothing queued", wa.onboard_gym(GID), 0)
with session_scope() as db:
    cfg = db.get(GymWhatsApp, GID)
    cfg.settings, cfg.status = None, "disconnected"
expect("not linked: nothing queued", wa.onboard_gym(GID), 0)
with session_scope() as db:
    db.get(GymWhatsApp, GID).status = "connected"
expect("linked again: queued", wa.onboard_gym(GID), 4)

print("== sending order and pace")
with session_scope() as db:
    db.execute(text("UPDATE wa_messages SET next_attempt_at = :t WHERE gym_id=:g"), {"t": utcnow() - timedelta(minutes=5), "g": GID})
    gym, gm = db.get(Gym, GID), db.get(GymMember, M["ends_today"])
    ms = wa.latest_membership(db, gm.id)
    r, why = wa.queue_message(db, gym, gm, "expiry_reminder", ms=ms, dedupe=f"expiry:{ms.id}:0")
    expect("a renewal reminder can be queued for the same member", (bool(r), why), (True, None))
    RID = r.id
others = lambda: text("UPDATE wa_messages SET next_attempt_at = :t WHERE gym_id <> :g AND status='queued'")
with session_scope() as db:   # keep other test gyms' queued rows out of this pass
    db.execute(others(), {"t": utcnow() + timedelta(days=2), "g": GID})
expect("one pass sends one message from a linked number", wa.process_queue(), 1)
with session_scope() as db:
    expect("the reminder went first, ahead of the older go-live messages", db.get(WaMessage, RID).status, "sent")
    expect("go-live messages are still waiting", db.scalar(text("SELECT COUNT(*) FROM wa_messages WHERE gym_id=:g AND event='gym_live' AND status='queued'"), {"g": GID}), 4)
expect("the next pass sends nothing until the gap has passed", wa.process_queue(), 0)
with session_scope() as db:
    db.get(GymWhatsApp, GID).next_send_at = utcnow() - timedelta(seconds=1)
expect("after the gap, one go-live message goes", wa.process_queue(), 1)
expect("the sent text carries the app link", settings.member_app_url in sent_texts[-1], True)

print("== daily limit keeps room for reminders")
settings.wa_web_daily_limit = 4      # 2 sent so far today; go-live may use 75% = 3
with session_scope() as db:
    db.get(GymWhatsApp, GID).next_send_at = None
expect("third message of the day: go-live still allowed", wa.process_queue(), 1)
with session_scope() as db:
    db.get(GymWhatsApp, GID).next_send_at = None
expect("at 75% of the limit, go-live waits", wa.process_queue(), 0)
with session_scope() as db:
    held = db.scalars(select(WaMessage).where(WaMessage.gym_id == GID, WaMessage.event == "gym_live", WaMessage.status == "queued")).all()
    expect("the held message is kept and moved to a later time, not dropped",
           (len(held), sum(h.next_attempt_at > utcnow() + timedelta(minutes=30) for h in held) >= 1), (2, True))
    expect("nothing failed", db.scalar(text("SELECT COUNT(*) FROM wa_messages WHERE gym_id=:g AND status IN ('failed','cancelled')"), {"g": GID}), 0)

with session_scope() as db:   # tidy up
    for t in ("wa_messages", "wa_templates", "renewal_tokens", "memberships"):
        db.execute(text(f"DELETE FROM {t} WHERE gym_id=:g"), {"g": GID})
    db.execute(text("DELETE FROM gym_whatsapp WHERE gym_id=:g"), {"g": GID})
    db.execute(text("DELETE FROM gym_members WHERE gym_id=:g"), {"g": GID})
    db.execute(text("UPDATE gyms SET status='rejected' WHERE id=:g"), {"g": GID})
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
