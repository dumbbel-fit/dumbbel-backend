"""Owner notifications for gym status changes: push to the Admin app (+ SMS only if a DLT template is set up).
App invites were removed: people are linked automatically when they log in with their phone number."""
import logging

from app.config import settings
from app.db import session_scope
from app.models import Gym
from app.services import sms
from app.services.jobs import enqueue
from app.services.push import send_push

log = logging.getLogger("dumbbel.notify")

TITLES = {"gym_approved": "Your gym is approved 🎉", "gym_rejected": "Gym verification needs attention",
          "gym_suspended": "Your gym is suspended", "gym_reinstated": "Your gym is active again"}


def queue_gym_status_sms(gym_id: str, kind: str, reason: str | None):
    def run():
        with session_scope() as db:
            g = db.get(Gym, gym_id)
            owner_id, name = g.owner_user_id, g.name
            from app.models import User
            phone = db.get(User, owner_id).phone
        why = (f"{reason}. " if reason else "") if kind == "gym_approved" else (reason or "—")
        vars = {"gym": name, "reason": why, "support": settings.support_phone}
        send_push(owner_id, "admin", TITLES.get(kind, "Gym update"), sms.render(kind, vars),
                  {"type": kind, "gym_id": gym_id})
        if sms.is_configured(kind):
            try:
                sms.send_template(phone, kind, vars)
            except Exception as e:  # logged inside sms
                log.warning("gym status SMS failed: %s", e)
    enqueue("notify.gym_status", run)
