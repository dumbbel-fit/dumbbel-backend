"""SMS jobs with retry ×3 (spec §18): invites and gym status updates."""
import logging
import time

from app.config import settings
from app.db import session_scope
from app.models import Gym, Invite, User
from app.services import sms
from app.services.jobs import enqueue
from app.utils import utcnow

log = logging.getLogger("dumbbel.notify")


def _send_invite(invite_id: str):
    with session_scope() as db:
        inv = db.get(Invite, invite_id)
        if not inv or inv.status not in ("queued", "failed"):
            return
        gym = db.get(Gym, inv.gym_id)
        owner = db.get(User, gym.owner_user_id)
        phone, kind = inv.phone, inv.kind
        vars = ({"owner": owner.name or "Your gym owner", "gym": gym.name, "link": settings.admin_app_link}
                if kind == "staff" else {"gym": gym.name, "link": settings.member_app_link})
    err = None
    for attempt in range(3):
        try:
            mid = sms.send_template(phone, "staff_invite" if kind == "staff" else "member_invite", vars)
            with session_scope() as db:
                inv = db.get(Invite, invite_id)
                if inv.status != "joined":
                    inv.status = "sent"
                inv.provider_message_id, inv.sent_at, inv.error = mid, utcnow(), None
            return
        except Exception as e:
            err = str(e)[:500]
            time.sleep(2 * (attempt + 1))
    with session_scope() as db:
        inv = db.get(Invite, invite_id)
        inv.status, inv.error = "failed", err


def queue_invite(invite_id: str):
    enqueue("sms.send", _send_invite, invite_id)


def queue_gym_status_sms(gym_id: str, kind: str, reason: str | None):
    def run():
        with session_scope() as db:
            g = db.get(Gym, gym_id)
            owner = db.get(User, g.owner_user_id)
            phone = owner.phone
            if kind == "gym_approved":
                why = f"{reason}. " if reason else ""
            else:
                why = reason or "—"
            vars = {"gym": g.name, "reason": why,
                    "support": settings.support_phone}
        sms.send_template(phone, kind, vars)
    enqueue("sms.send", run, retries=2)
