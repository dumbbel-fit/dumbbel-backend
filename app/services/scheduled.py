"""Cron jobs (spec §18), all IST. Run in-process via APScheduler (set RUN_SCHEDULER=false on extra workers),
or manually:  python -m app.services.scheduled <job_name>"""
import logging
import sys
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from app.db import session_scope
from app.models import (Checkin, Gym, GymMember, GymStaff, IdempotencyKey, Import, Invite, Membership,
                        MembershipFreeze, OtpCode, Payment, Session as DbSession, User)
from app.services.cache import cache
from app.services.core import gym_settings
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.rules import load_states, streaks, visit_days
from app.utils import local_midnight_utc, rupees, today_in, utcnow

log = logging.getLogger("dumbbel.cron")


def sync_membership_states():
    """Keep the stored `state` column in step with dates/freezes; cap open-ended freezes at max_freeze_days."""
    with session_scope() as db:
        for g in db.scalars(select(Gym)):
            today = today_in(g.timezone)
            max_days = int(gym_settings(g)["max_freeze_days"])
            ms_list = db.scalars(select(Membership).where(Membership.gym_id == g.id, Membership.state != "cancelled",
                                                          Membership.end_date >= today - timedelta(days=1))).all()
            for m in ms_list:
                frs = db.scalars(select(MembershipFreeze).where(MembershipFreeze.membership_id == m.id)).all()
                for f in frs:  # auto-close open-ended freezes at the limit
                    if f.to_date is None and f.ended_at is None:
                        other = sum(x.days for x in frs if x.id != f.id)
                        allowed = max(0, max_days - other)
                        if (today - f.from_date).days >= allowed:
                            f.days, f.to_date, f.ended_at = allowed, f.from_date + timedelta(days=allowed - 1), utcnow()
                            m.end_date = m.end_date + timedelta(days=allowed)
                frozen = any(f.from_date <= today and (f.to_date is None or today <= f.to_date) for f in frs)
                m.state = "frozen" if frozen else ("scheduled" if m.start_date > today else "active")


def nightly_stats():
    sync_membership_states()
    with session_scope() as db:
        for g in db.scalars(select(Gym).where(Gym.status == "active")):
            today = today_in(g.timezone)
            for gm in db.scalars(select(GymMember).where(GymMember.gym_id == g.id, GymMember.last_checkin_at.is_not(None))):
                _, best = streaks(visit_days(db, gm.id), today)
                gm.best_streak = max(gm.best_streak or 0, best)
            cache.delete(f"gymavg:{g.id}")
    log.info("nightly.stats done")


def membership_notifications():
    """membership_ending at 7 and 1 days before end; membership_ended the day after end. 10:00 IST."""
    sync_membership_states()
    with session_scope() as db:
        for g in db.scalars(select(Gym).where(Gym.status == "active")):
            today = today_in(g.timezone)
            targets = {today + timedelta(days=7): "ending7", today + timedelta(days=1): "ending1",
                       today - timedelta(days=1): "ended"}
            rows = db.execute(select(Membership, GymMember).join(GymMember, GymMember.id == Membership.member_id).where(
                Membership.gym_id == g.id, Membership.state != "cancelled", Membership.end_date.in_(list(targets)),
                GymMember.status == "active", GymMember.claim_disputed_at.is_(None))).all()
            for m, gm in rows:
                # skip if they already renewed (a later membership exists)
                if db.scalar(select(Membership.id).where(Membership.member_id == gm.id, Membership.state != "cancelled",
                                                         Membership.start_date > m.end_date).limit(1)):
                    continue
                kind = targets[m.end_date]
                if kind == "ended":
                    title, body = "Membership ended", f"Your {m.plan_name} at {g.name} ended yesterday. Renew to keep going!"
                    t = "membership_ended"
                else:
                    n = 7 if kind == "ending7" else 1
                    title = "Membership ending soon"
                    body = f"Your {m.plan_name} at {g.name} ends in {n} day{'s' if n > 1 else ''}."
                    t = "membership_ending"
                enqueue("push.send", send_push, gm.user_id, "member", title, body, {"type": t, "member_id": gm.id})


def owner_digest():
    """21:00 IST: check-ins, collections, renewals due tomorrow."""
    with session_scope() as db:
        for g in db.scalars(select(Gym).where(Gym.status == "active")):
            today = today_in(g.timezone)
            checkins = db.scalar(select(func.count(func.distinct(Checkin.member_id))).where(
                Checkin.gym_id == g.id, Checkin.local_date == today, Checkin.result == "success")) or 0
            collected = db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
                Payment.gym_id == g.id, Payment.status == "recorded", Payment.source != "import",
                Payment.paid_at >= local_midnight_utc(today, g.timezone))) or 0
            renewals = db.scalar(select(func.count()).select_from(Membership).where(
                Membership.gym_id == g.id, Membership.state != "cancelled",
                Membership.end_date == today + timedelta(days=1))) or 0
            enqueue("push.send", send_push, g.owner_user_id, "admin", f"Today at {g.name}",
                    f"{checkins} check-ins · {rupees(int(collected))} collected · {renewals} renewal{'s' if renewals != 1 else ''} due tomorrow",
                    {"type": "daily_digest", "gym_id": g.id})


def cleanup():
    now = utcnow()
    with session_scope() as db:
        db.execute(delete(OtpCode).where(OtpCode.expires_at < now - timedelta(days=1)))
        db.execute(delete(DbSession).where(DbSession.expires_at < now - timedelta(days=7)))
        db.execute(delete(DbSession).where(DbSession.revoked_at < now - timedelta(days=7)))
        db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < now - timedelta(hours=24)))
        db.execute(update(Import).where(Import.status.in_(("uploaded", "parsing", "previewed")),
                                        Import.created_at < now - timedelta(days=7)).values(status="cancelled"))


def account_deletion():
    """Scrub users whose deletion request is older than 30 days (PII removed, phone tombstoned)."""
    cutoff = utcnow() - timedelta(days=30)
    with session_scope() as db:
        for u in db.scalars(select(User).where(User.deletion_requested_at < cutoff, User.deleted_at.is_(None))):
            if db.scalar(select(GymStaff.id).where(GymStaff.user_id == u.id, GymStaff.status != "deactivated").limit(1)):
                continue
            u.phone = f"deleted:{u.id}"
            u.name, u.email, u.dob, u.gender, u.photo_url = "Deleted user", None, None, None, None
            u.deleted_at = utcnow()
            db.execute(update(GymMember).where(GymMember.user_id == u.id)
                       .values(display_name="Deleted member", emergency_contact=None, notes=None))
            db.execute(update(Invite).where(
                Invite.member_id.in_(select(GymMember.id).where(GymMember.user_id == u.id))).values(phone="deleted"))
            db.execute(update(DbSession).where(DbSession.user_id == u.id, DbSession.revoked_at.is_(None))
                       .values(revoked_at=utcnow()))


JOBS = {"nightly_stats": nightly_stats, "membership_notifications": membership_notifications,
        "owner_digest": owner_digest, "cleanup": cleanup, "account_deletion": account_deletion,
        "sync_membership_states": sync_membership_states}


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    s = BackgroundScheduler(timezone="Asia/Kolkata")
    s.add_job(nightly_stats, "cron", hour=1, minute=0, id="nightly.stats")
    s.add_job(membership_notifications, "cron", hour=10, minute=0, id="daily.membership_notifications")
    s.add_job(owner_digest, "cron", hour=21, minute=0, id="daily.owner_digest")
    s.add_job(cleanup, "cron", minute=15, id="cleanup")
    s.add_job(account_deletion, "cron", hour=3, minute=0, id="account.deletion")
    s.add_job(sync_membership_states, "cron", hour=0, minute=5, id="membership.state_sync")
    from app.services import whatsapp as wa
    # WhatsApp: outbox worker every 20 s; reminders checked hourly (each gym sends at its own hour; dedupe makes repeats safe)
    s.add_job(wa.process_queue, "interval", seconds=5, id="whatsapp.queue", max_instances=1, coalesce=True)
    s.add_job(wa.run_expiry_automation, "cron", minute=5, id="whatsapp.reminders", max_instances=1, coalesce=True)
    s.start()
    return s


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    JOBS[sys.argv[1]]()
    print("done:", sys.argv[1])
