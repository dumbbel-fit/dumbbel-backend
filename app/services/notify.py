"""Invites (sent synchronously so the API can report exactly what happened) and gym-status SMS."""
import logging
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Gym, Invite, User
from app.services import sms
from app.services.jobs import enqueue
from app.utils import utcnow

log = logging.getLogger("dumbbel.notify")


def invite_vars(db: Session, inv: Invite) -> dict:
    gym = db.get(Gym, inv.gym_id)
    if inv.kind == "staff":
        owner = db.get(User, gym.owner_user_id)
        return {"owner": owner.name or "Your gym owner", "gym": gym.name, "link": settings.admin_app_link}
    return {"gym": gym.name, "link": settings.member_app_link}


def whatsapp_share(inv: Invite, text: str) -> str:
    return f"https://wa.me/{inv.phone.lstrip('+')}?text={quote(text)}"


def send_invite_now(db: Session, inv: Invite) -> dict:
    """Try to send one invite SMS. Updates the invite row (caller commits).
    Returns {status: sent|failed, reason?, provider_message_id?, whatsapp_url}."""
    kind = "staff_invite" if inv.kind == "staff" else "member_invite"
    vars = invite_vars(db, inv)
    text = sms.render(kind, vars)
    out = {"whatsapp_url": whatsapp_share(inv, text), "message": text}
    try:
        mid = sms.send_template(inv.phone, kind, vars)
        inv.status = "sent" if inv.status != "joined" else "joined"
        inv.provider_message_id, inv.sent_at, inv.error = mid, utcnow(), None
        out.update(status="sent", provider_message_id=mid)
    except sms.SmsNotConfigured as e:
        inv.status, inv.error = "failed", str(e)[:500]
        out.update(status="failed", reason="SMS_NOT_CONFIGURED", detail=str(e))
    except sms.SmsFailed as e:
        inv.status, inv.error = "failed", str(e)[:500]
        out.update(status="failed", reason="SMS_PROVIDER_ERROR", detail=str(e))
    return out


def queue_gym_status_sms(gym_id: str, kind: str, reason: str | None):
    from app.db import session_scope

    def run():
        with session_scope() as db:
            g = db.get(Gym, gym_id)
            owner = db.get(User, g.owner_user_id)
            phone = owner.phone
            if kind == "gym_approved":
                why = f"{reason}. " if reason else ""
            else:
                why = reason or "—"
            vars = {"gym": g.name, "reason": why, "support": settings.support_phone}
        try:
            sms.send_template(phone, kind, vars)
        except sms.SmsNotConfigured:
            pass  # already logged; nothing to retry
    enqueue("sms.send", run, retries=2)
