"""Expo push notifications (spec §18)."""
import logging

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import DeviceToken

log = logging.getLogger("dumbbel.push")
EXPO_URL = "https://exp.host/--/api/v2/push/send"


def send_push(user_id: str, app: str, title: str, body: str, data: dict | None = None):
    """Send to every device the user registered for that app. Never raises."""
    try:
        with session_scope() as db:
            tokens = db.scalars(select(DeviceToken.expo_push_token)
                                .where(DeviceToken.user_id == user_id, DeviceToken.app == app)).all()
            if not tokens:
                return
            msgs = [{"to": t, "title": title, "body": body, "data": data or {}, "sound": "default"} for t in tokens]
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if settings.expo_access_token:
                headers["Authorization"] = f"Bearer {settings.expo_access_token}"
            r = httpx.post(EXPO_URL, json=msgs, headers=headers, timeout=10)
            res = r.json().get("data", [])
            for tok, item in zip(tokens, res if isinstance(res, list) else []):
                if item.get("status") == "error" and item.get("details", {}).get("error") == "DeviceNotRegistered":
                    db.query(DeviceToken).filter(DeviceToken.expo_push_token == tok).delete()
    except Exception as e:
        log.warning("push to %s failed: %s", user_id, e)
