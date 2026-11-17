"""WhatsApp for gyms, on Meta's official Cloud API (one Meta app for the platform, each gym connects its own number).

Design rules:
  * Business-initiated messages are always *approved templates* — free text only works inside the 24h service
    window, which reminders never are, so we don't pretend otherwise.
  * Nothing is sent in a web request. Messages are rows in `wa_messages` (the outbox); a worker sends them with
    rate limiting and retries. `dedupe_key` (unique per gym) makes repeated cron runs / double clicks harmless.
  * "sent" only means Meta accepted it. delivered / read / failed come from signed webhooks.
  * Tokens are stored encrypted and never leave the backend; nothing sensitive is logged.
"""
import base64
import hashlib
import hmac
import logging
import random
import threading
import re
import secrets
import time
from datetime import date, timedelta

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import session_scope
from app.models import (Gym, GymMember, GymUpiAccount, GymWhatsApp, Membership, Payment, Plan, RenewalToken, User,
                        WaMessage, WaTemplate)
from app.utils import local_midnight_utc, rupees, today_in, utcnow, uuid7

log = logging.getLogger("dumbbel.whatsapp")

# ---------------------------------------------------------------- events & default templates
VARS = {"member_name": "Meghana", "gym_name": "Iron Republic Fitness", "membership_plan": "Monthly",
        "expiry_date": "30 Nov 2026", "gym_phone": "+91 98765 43210", "membership_amount": "₹1,500",
        "amount": "₹1,500", "announcement": "The gym stays open till 11 pm this week", "days_left": "3",
        "app_link": "https://dumbbel.fit/member"}
BUTTON_EVENTS = {"expiry_reminder", "expired", "payment_due"}

EVENTS: dict[str, dict] = {
    "expiry_reminder": {
        "label": "Membership expiring soon", "category": "UTILITY", "button": "Renew membership", "default_on": True,
        "body": "Hi {{member_name}}, your {{membership_plan}} membership at {{gym_name}} ends on {{expiry_date}}. "
                "Renew with the link below to keep your access. Questions? Call us on {{gym_phone}}."},
    "expired": {
        "label": "Membership expired", "category": "UTILITY", "button": "Renew membership", "default_on": True,
        "body": "Hi {{member_name}}, your {{membership_plan}} membership at {{gym_name}} ended on {{expiry_date}}. "
                "You can renew any time with the link below. Call {{gym_phone}} if you need help."},
    "gym_live": {
        "label": "Your gym is on DUMBBEL.fit (once per member)", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Hi {{member_name}}, {{gym_name}} is now on DUMBBEL.fit. Open {{app_link}} and sign in with this "
                "mobile number to check in at the gym every day, see your attendance and streak, and keep track of "
                "your membership. Questions? Call us on {{gym_phone}}. Reply STOP to stop these messages."},
    "welcome": {
        "label": "New member welcome", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Welcome to {{gym_name}}, {{member_name}}. Your {{membership_plan}} membership is active until "
                "{{expiry_date}}. Reach us on {{gym_phone}} for anything you need."},
    "renewal_confirmed": {
        "label": "Membership renewed / new plan added", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Thanks {{member_name}}, your {{membership_plan}} membership at {{gym_name}} is confirmed and valid "
                "until {{expiry_date}}. See you at the gym."},
    "payment_received": {
        "label": "Payment received", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Hi {{member_name}}, we received your payment of {{amount}} at {{gym_name}}. Thank you. "
                "Call {{gym_phone}} if anything looks wrong."},
    "payment_due": {
        "label": "Payment pending", "category": "UTILITY", "button": "Pay now", "default_on": False,
        "body": "Hi {{member_name}}, {{membership_amount}} is pending for your {{membership_plan}} membership at "
                "{{gym_name}}. Please pay at the front desk or with the link below. Thank you."},
    "payment_failed": {
        "label": "Payment not verified", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Hi {{member_name}}, we could not verify your recent payment of {{amount}} at {{gym_name}}. "
                "Please call {{gym_phone}} so we can sort it out."},
    "membership_cancelled": {
        "label": "Membership cancelled", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Hi {{member_name}}, your {{membership_plan}} membership at {{gym_name}} has been cancelled. "
                "If you think this is a mistake, please call us on {{gym_phone}}."},
    "membership_frozen": {
        "label": "Membership on hold (frozen)", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Hi {{member_name}}, your {{membership_plan}} membership at {{gym_name}} is on hold as requested. "
                "The days you are away are added back, so you don't lose any. Call {{gym_phone}} with any questions."},
    "membership_resumed": {
        "label": "Membership resumed", "category": "UTILITY", "button": None, "default_on": True,
        "body": "Welcome back {{member_name}}, your {{membership_plan}} membership at {{gym_name}} is active again "
                "and valid until {{expiry_date}}. See you at the gym."},
    "away_nudge": {
        "label": "Haven’t visited in 14 days", "category": "MARKETING", "button": None, "default_on": False,
        "body": "Hi {{member_name}}, we have not seen you at {{gym_name}} for a while. Your {{membership_plan}} "
                "membership is still active, come back whenever you are ready. Reply STOP to stop these updates."},
    "announcement": {
        "label": "Gym announcement", "category": "MARKETING", "button": None, "default_on": False, "manual_only": True,
        "body": "Hi {{member_name}}, here is an update for all members of {{gym_name}}: {{announcement}}. "
                "Please call us on {{gym_phone}} if you have any questions. Reply STOP to stop these updates."},
}
AUTO_EVENTS = [k for k, v in EVENTS.items() if not v.get("manual_only")]
# Answers to something that just happened at the desk (a payment, a new plan, a cancellation). They go out at
# once, before any bulk reminder, and also during quiet hours: the member is standing at the gym.
INSTANT_EVENTS = {"welcome", "renewal_confirmed", "payment_received", "payment_failed", "membership_cancelled",
                  "membership_frozen", "membership_resumed"}
DEFAULT_SETTINGS = {"send_hour": 10, "expiry_days_before": [7, 3, 1, 0], "expired_days_after": [1],
                    "due_every_days": 7, "automations": {k: bool(v["default_on"]) for k, v in EVENTS.items()
                                                         if not v.get("manual_only")}}
VAR_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
STATUS_ORDER = {"queued": 0, "sending": 1, "sent": 2, "delivered": 3, "read": 4}
TRANSIENT_CODES = {1, 2, 4, 80007, 130429, 131000, 131016, 131048, 131056, 133004, 133016}
TOKEN_CODES = {190, 0, 3, 10, 200}


# ---------------------------------------------------------------- secrets
def _fernet() -> Fernet:
    key = settings.whatsapp_enc_key
    if not key:  # stable fallback so tokens survive restarts even before the dedicated key is set
        key = base64.urlsafe_b64encode(hashlib.sha256(("wa-token:" + settings.jwt_secret).encode()).digest()).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- Meta Graph API
class MetaError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int = 0, transient: bool = False,
                 subcode: int | None = None):
        super().__init__(message)
        self.message, self.code, self.status, self.transient, self.subcode = message, code, status, transient, subcode

    @property
    def token_problem(self) -> bool:
        return self.code == 190 or (self.status == 401)


def graph(method: str, path: str, token: str | None, *, json: dict | None = None, params: dict | None = None,
          timeout: float = 20) -> dict:
    """Call the Graph API. Raises MetaError with a user-safe message; never logs the token."""
    url = f"{settings.meta_graph_base.rstrip('/')}/{settings.meta_graph_version}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.request(method, url, json=json, params=params, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise MetaError(f"Couldn’t reach WhatsApp ({type(e).__name__}).", transient=True)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error") or {}
        code = err.get("code")
        msg = (err.get("error_user_msg") or (err.get("error_data") or {}).get("details") or err.get("message")
               or f"WhatsApp returned HTTP {r.status_code}")
        transient = r.status_code >= 500 or r.status_code == 429 or code in TRANSIENT_CODES
        raise MetaError(str(msg)[:400], code=code, status=r.status_code, transient=transient,
                        subcode=err.get("error_subcode"))
    return data


# ---------------------------------------------------------------- Evolution API (QR-linked WhatsApp, Baileys)
def web_available() -> bool:
    return bool(settings.evolution_api_url and settings.evolution_api_key)


def evo(method: str, path: str, *, json: dict | None = None, params: dict | None = None, timeout: float = 25) -> dict:
    """Call our Evolution API server. Raises MetaError (same handling as the Cloud API). Never logs the key."""
    if not web_available():
        raise MetaError("WhatsApp QR linking isn’t set up on this server.")
    url = settings.evolution_api_url.rstrip("/") + "/" + path.lstrip("/")
    try:
        r = httpx.request(method, url, json=json, params=params, timeout=timeout,
                          headers={"apikey": settings.evolution_api_key})
    except httpx.HTTPError as e:
        raise MetaError(f"The WhatsApp link service isn’t reachable ({type(e).__name__}).", transient=True)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        resp = data.get("response") if isinstance(data, dict) else None
        msg = resp.get("message") if isinstance(resp, dict) else None
        if isinstance(msg, list) and msg:
            m0 = msg[0]
            if isinstance(m0, dict) and m0.get("exists") is False:
                raise MetaError("This number isn’t on WhatsApp.", code=404404, status=r.status_code)
            msg = m0 if isinstance(m0, str) else str(m0)
        msg = msg or (data.get("error") if isinstance(data, dict) else None) or f"WhatsApp link service returned HTTP {r.status_code}"
        raise MetaError(str(msg)[:400], status=r.status_code, transient=r.status_code >= 500 or r.status_code == 429)
    return data if isinstance(data, (dict, list)) else {}


def instance_name(gym_id: str) -> str:
    return "dmbl-" + gym_id


def web_hook_secret(gym_id: str) -> str:
    return hmac.new(("evo-hook:" + settings.jwt_secret).encode(), gym_id.encode(), hashlib.sha256).hexdigest()


WEB_EVENTS = ["QRCODE_UPDATED", "CONNECTION_UPDATE", "MESSAGES_UPDATE", "MESSAGES_UPSERT"]


def web_webhook_url() -> str:
    base = (settings.evolution_webhook_base or _public_base()).rstrip("/")
    return f"{base}{settings.api_prefix}/webhooks/evolution"


def _public_base() -> str:
    from app.services.storage import _base
    return _base()


# The number a gym is linking with a pairing code, kept for the whole attempt so a session that times out and
# restarts asks for a code again instead of silently falling back to QR. One server (max-instances 1).
_PAIRING: dict[str, str] = {}
LINK_WAIT_SEC = 8   # how long the first response waits for the QR / code to be ready


def pairing_for(gym_id: str) -> str | None:
    return _PAIRING.get(gym_id)


def _wait_for_code(gym_id: str, want_code: bool, first: dict) -> dict:
    """Evolution makes the QR (and the pairing code) a few seconds after the session starts. Ask again for a
    few seconds so the first answer already has it. While connecting, /instance/connect only reads the current
    QR: it doesn't restart anything or request a new code."""
    out, end = first, time.time() + LINK_WAIT_SEC
    while out.get("state") != "open" and not (out.get("pairing_code") if want_code else out.get("qr_base64")):
        if time.time() >= end:
            break
        time.sleep(1.5)
        out = web_connect(gym_id, None)
    return out


def _wait_gone(gym_id: str, limit: float = 12.0) -> bool:
    """Evolution deletes an instance in the background after answering. Wait until it is really gone."""
    end = time.time() + limit
    while time.time() < end:
        try:
            web_state(gym_id)
        except MetaError:
            return True
        time.sleep(0.8)
    return False


def web_create(gym_id: str, pairing_number: str | None = None) -> dict:
    """Start linking and return {state, qr_base64, pairing_code}.

    Reuses this gym's session when it can. Only a session still waiting for a scan while a pairing code is wanted
    is replaced: Evolution ignores a new number on a waiting session, so the code would never come. The old one
    is deleted and we wait until it's really gone before creating the new one (deleting is asynchronous)."""
    name = instance_name(gym_id)
    try:
        state = web_state(gym_id)
    except MetaError:
        state = None   # no session yet
    if state == "open":
        _PAIRING.pop(gym_id, None)
        return {"state": "open", "qr_base64": None, "pairing_code": None}
    if pairing_number:
        _PAIRING[gym_id] = pairing_number
    else:
        _PAIRING.pop(gym_id, None)
    if state == "connecting" and pairing_number:
        web_remove(gym_id)
        _wait_gone(gym_id)
        state = None
    body = {"instanceName": name, "integration": "WHATSAPP-BAILEYS", "qrcode": True, "groupsIgnore": True,
            "rejectCall": False, "alwaysOnline": False, "readMessages": False, "readStatus": False,
            "syncFullHistory": False,
            "webhook": {"enabled": True, "url": web_webhook_url(), "byEvents": False, "base64": False,
                        "headers": {"x-dumbbel-token": web_hook_secret(gym_id)}, "events": WEB_EVENTS}}
    if pairing_number:
        body["number"] = pairing_number
    first = None
    if state is None:
        for attempt in range(4):
            try:
                d = evo("POST", "/instance/create", json=body)
                q = d.get("qrcode") or {}
                first = {"state": "connecting", "qr_base64": q.get("base64"), "pairing_code": q.get("pairingCode")}
                break
            except MetaError as e:
                msg = e.message.lower()
                if "already in use" not in msg and "already exists" not in msg:
                    raise
                try:            # still being deleted, or it exists after all: use it if it's there
                    state = web_state(gym_id)
                    break
                except MetaError:
                    time.sleep(1.5)
        else:
            raise MetaError("WhatsApp linking is busy. Please try again in a few seconds.", status=409)
    if first is None:
        # existing session: make sure the webhook points here (URL / secret may have changed), then (re)connect
        evo("POST", f"/webhook/set/{name}", json={"webhook": body["webhook"]})
        first = web_connect(gym_id, pairing_number)
    return _wait_for_code(gym_id, bool(pairing_number), first)


def web_connect(gym_id: str, pairing_number: str | None = None) -> dict:
    name = instance_name(gym_id)
    d = evo("GET", f"/instance/connect/{name}", params={"number": pairing_number} if pairing_number else None)
    if isinstance(d, dict) and (d.get("instance") or {}).get("state") == "open":
        return {"state": "open", "qr_base64": None, "pairing_code": None}
    return {"state": "connecting", "qr_base64": d.get("base64") if isinstance(d, dict) else None,
            "pairing_code": d.get("pairingCode") if isinstance(d, dict) else None}


def web_state(gym_id: str) -> str:
    d = evo("GET", f"/instance/connectionState/{instance_name(gym_id)}")
    return ((d.get("instance") or {}).get("state") or "close") if isinstance(d, dict) else "close"


def web_profile(gym_id: str) -> dict:
    d = evo("GET", "/instance/fetchInstances", params={"instanceName": instance_name(gym_id)})
    row = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else {})
    row = row.get("instance", row) if isinstance(row, dict) else {}
    jid = row.get("ownerJid") or row.get("owner") or ""
    digits = re.sub(r"\D", "", jid.split("@")[0]) if jid else ""
    return {"phone": ("+" + digits) if digits else None, "name": row.get("profileName")}


def web_remove(gym_id: str):
    name = instance_name(gym_id)
    for path in (f"/instance/logout/{name}", f"/instance/delete/{name}"):
        try:
            evo("DELETE", path)
        except MetaError:
            pass


# ---------------------------------------------------------------- config
def get_config(db: Session, gym_id: str) -> GymWhatsApp | None:
    return db.get(GymWhatsApp, gym_id)


def wa_settings(cfg: GymWhatsApp | None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    saved = (cfg.settings if cfg else None) or {}
    s.update({k: v for k, v in saved.items() if k != "automations"})
    s["automations"] = {**DEFAULT_SETTINGS["automations"], **(saved.get("automations") or {})}
    return s


def is_connected(cfg: GymWhatsApp | None) -> bool:
    if not cfg or cfg.status != "connected":
        return False
    if cfg.provider == "web":
        return bool(cfg.instance_name) and web_available()
    return bool(cfg.token_enc and cfg.phone_number_id)


def template_ready(cfg: GymWhatsApp | None, tpl: WaTemplate | None) -> bool:
    """Linked (web) numbers send plain text, so no WhatsApp approval is needed; the Cloud API needs it."""
    if not tpl:
        return False
    return True if (cfg and cfg.provider == "web") else tpl.status == "approved"


def verify_number(token: str, phone_number_id: str) -> dict:
    return graph("GET", phone_number_id, token,
                 params={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"})


def subscribe_app(token: str, waba_id: str):
    """Make Meta deliver this WABA's webhooks (delivery receipts, replies, template reviews) to our app."""
    graph("POST", f"{waba_id}/subscribed_apps", token)


def exchange_code(code: str) -> str:
    if not (settings.meta_app_id and settings.meta_app_secret):
        raise MetaError("Embedded signup isn’t configured on this server.")
    d = graph("GET", "oauth/access_token", None, params={"client_id": settings.meta_app_id,
                                                         "client_secret": settings.meta_app_secret, "code": code})
    return d["access_token"]


# Default texts we have since improved: a gym still on the old default (never edited) gets the new one.
OLD_DEFAULTS = {"renewal_confirmed": "Thanks {{member_name}}, your {{membership_plan}} membership at {{gym_name}} is renewed "
                                     "and valid until {{expiry_date}}. See you at the gym."}


def ensure_templates(db: Session, gym_id: str) -> list[WaTemplate]:
    have = {t.key: t for t in db.scalars(select(WaTemplate).where(WaTemplate.gym_id == gym_id))}
    for key, old in OLD_DEFAULTS.items():
        t = have.get(key)
        if t is not None and t.body == old and t.status in ("draft", "ready", "approved") and \
                not db.scalar(select(GymWhatsApp.provider).where(GymWhatsApp.gym_id == gym_id, GymWhatsApp.provider == "cloud")):
            t.body = EVENTS[key]["body"]   # linked (web) numbers send plain text: no WhatsApp re-approval needed
    for key, ev in EVENTS.items():
        if key not in have:
            t = WaTemplate(gym_id=gym_id, key=key, category=ev["category"], body=ev["body"], button_text=ev["button"])
            try:   # several messages can fire at once for the same gym: the other one may have just made it
                with db.begin_nested():
                    db.add(t)
                    db.flush()
            except IntegrityError:
                t = db.scalar(select(WaTemplate).where(WaTemplate.gym_id == gym_id, WaTemplate.key == key))
            if t is not None:
                have[key] = t
    db.flush()
    return [have[k] for k in EVENTS if k in have]


# ---------------------------------------------------------------- templates
def template_vars(body: str) -> list[str]:
    out = []
    for v in VAR_RE.findall(body):
        if v not in out:
            out.append(v)
    return out


def validate_template_body(body: str) -> str | None:
    """Meta's rules, checked before we submit so owners get a clear message instead of a rejection."""
    b = (body or "").strip()
    if len(b) < 20:
        return "The message is too short."
    if len(b) > 1000:
        return "Keep the message under 1,000 characters."
    unknown = [v for v in template_vars(b) if v not in VARS]
    if unknown:
        return f"Unknown variable: {{{{{unknown[0]}}}}}. Use only the listed variables."
    if re.search(r"\{\{(?!\s*[a-z_]+\s*\}\})", b) or b.count("{{") != b.count("}}"):
        return "A variable isn’t closed properly. Write them like {{member_name}}."
    if VAR_RE.match(b) or re.search(r"\{\{\s*[a-z_]+\s*\}\}\s*$", b):
        return "The message can’t start or end with a variable. Add a word before/after it."
    if re.search(r"\}\}\s*\{\{", b):
        return "Two variables can’t sit next to each other. Put some text between them."
    if "\n\n\n" in b or "\t" in b or "    " in b:
        return "Remove tabs, long gaps and more than one blank line."
    words = len(re.sub(VAR_RE, "", b).split())
    if words < 3 * len(template_vars(b)):
        return "Too many variables for such a short message. Add more text."
    return None


def meta_name(key: str, version: int) -> str:
    return f"dmbl_{key}_v{version}"


def to_meta_components(t: WaTemplate) -> list[dict]:
    names = template_vars(t.body)
    text = t.body
    for i, n in enumerate(names, 1):
        text = re.sub(r"\{\{\s*" + n + r"\s*\}\}", "{{" + str(i) + "}}", text)
    body: dict = {"type": "BODY", "text": text}
    if names:
        body["example"] = {"body_text": [[VARS[n] for n in names]]}
    comps = [body]
    if t.button_text:
        base = settings.renew_url_base.rstrip("/")
        comps.append({"type": "BUTTONS", "buttons": [{"type": "URL", "text": t.button_text, "url": base + "/{{1}}",
                                                      "example": [base + "/Zx81kExampleToken"]}]})
    return comps


def submit_template(db: Session, cfg: GymWhatsApp, t: WaTemplate) -> WaTemplate:
    err = validate_template_body(t.body)
    if err:
        raise MetaError(err)
    token = decrypt(cfg.token_enc)
    if t.status != "draft" or t.meta_name is None:
        pass
    name = meta_name(t.key, t.version)
    d = graph("POST", f"{cfg.waba_id}/message_templates", token,
              json={"name": name, "language": t.language, "category": t.category, "components": to_meta_components(t)})
    t.meta_name, t.meta_id = name, str(d.get("id") or "") or None
    t.status = _tpl_status(d.get("status") or "PENDING")
    t.reject_reason, t.submitted_at = None, utcnow()
    return t


def _tpl_status(meta: str) -> str:
    m = (meta or "").upper()
    return {"APPROVED": "approved", "PENDING": "pending", "IN_APPEAL": "pending", "REJECTED": "rejected",
            "PAUSED": "paused", "DISABLED": "disabled", "PENDING_DELETION": "disabled"}.get(m, "pending")


def sync_templates(db: Session, cfg: GymWhatsApp) -> int:
    token = decrypt(cfg.token_enc)
    d = graph("GET", f"{cfg.waba_id}/message_templates", token,
              params={"fields": "name,status,category,rejected_reason,id,language", "limit": 200})
    remote = {x["name"]: x for x in d.get("data", [])}
    n = 0
    for t in db.scalars(select(WaTemplate).where(WaTemplate.gym_id == cfg.gym_id, WaTemplate.meta_name.is_not(None))):
        r = remote.get(t.meta_name)
        if not r:
            continue
        st = _tpl_status(r.get("status"))
        if st != t.status or (r.get("category") and r["category"] != t.category):
            n += 1
        t.status, t.meta_id = st, str(r.get("id") or t.meta_id or "") or None
        if r.get("category"):
            t.category = r["category"]  # Meta may re-categorise (e.g. utility → marketing)
        rr = r.get("rejected_reason")
        t.reject_reason = None if not rr or rr == "NONE" else str(rr).replace("_", " ").capitalize()
    return n


# ---------------------------------------------------------------- variables & rendering
def _clean_param(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:300] or "-"


def fmt_date(d: date | None) -> str:
    return d.strftime("%-d %b %Y") if d else "-"


def fmt_phone(p: str | None) -> str:
    digits = re.sub(r"\D", "", p or "")
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:7]} {digits[7:]}"
    return p or "-"


def build_vars(db: Session, gym: Gym, member: GymMember, ms: Membership | None, extra: dict | None = None) -> dict:
    first = (member.display_name or "there").strip().split(" ")[0][:40]
    amount = None
    if ms is not None:
        paid = db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
            Payment.membership_id == ms.id, Payment.status == "recorded")) or 0
        due = max(0, (ms.price_paise or 0) - int(paid))
        amount = rupees(due if due > 0 else (ms.price_paise or 0))
    v = {"member_name": first, "gym_name": gym.name, "membership_plan": ms.plan_name if ms else "membership",
         "expiry_date": fmt_date(ms.end_date) if ms else "-", "gym_phone": fmt_phone(gym.phone),
         "membership_amount": amount or "-", "amount": amount or "-", "app_link": settings.member_app_url}
    if ms is not None:
        v["days_left"] = str(max(0, (ms.end_date - today_in(gym.timezone)).days))
    v.update({k: x for k, x in (extra or {}).items() if x is not None})
    return {k: _clean_param(x) for k, x in v.items()}


def render(body: str, v: dict, link: str | None = None) -> str:
    text = VAR_RE.sub(lambda m: str(v.get(m.group(1), "")), body)
    return text + (f"\n\n{link}" if link else "")


def latest_membership(db: Session, member_id: str) -> Membership | None:
    return db.scalar(select(Membership).where(Membership.member_id == member_id, Membership.state != "cancelled")
                     .order_by(Membership.end_date.desc()).limit(1))


def wa_number(phone: str | None) -> str | None:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 10:
        d = "91" + d
    return d if 11 <= len(d) <= 15 and not (phone or "").startswith("deleted") else None


# ---------------------------------------------------------------- renewal links
def new_renewal_token(db: Session, gym_id: str, member_id: str, message_id: str | None = None,
                      source: str = "whatsapp") -> str:
    raw = secrets.token_urlsafe(24)
    if message_id:  # a retry replaces the link from the earlier attempt
        db.execute(update(RenewalToken).where(RenewalToken.message_id == message_id, RenewalToken.used_at.is_(None))
                   .values(revoked_at=utcnow()))
    db.add(RenewalToken(token_hash=hash_token(raw), gym_id=gym_id, member_id=member_id, message_id=message_id,
                        source=source, expires_at=utcnow() + timedelta(days=settings.renew_token_days)))
    return raw


# ---------------------------------------------------------------- queueing
SKIP = {"not_connected": "WhatsApp isn’t connected", "automation_off": "This automation is switched off",
        "template_not_approved": "The message template isn’t approved by WhatsApp yet",
        "no_phone": "No valid mobile number", "opted_out": "Member opted out of WhatsApp messages",
        "duplicate": "Already sent", "no_consent": "Confirm member consent in WhatsApp settings first",
        "archived": "Member is deactivated"}


def queue_message(db: Session, gym: Gym, member: GymMember, event: str, *, ms: Membership | None = None,
                  dedupe: str | None = None, batch_id: str | None = None, extra: dict | None = None,
                  actor_id: str | None = None, trigger: str = "auto", cfg: GymWhatsApp | None = None,
                  tpl: WaTemplate | None = None, user: User | None = None,
                  offline: bool = False) -> tuple[WaMessage | None, str | None]:
    """Add one message to the outbox. Returns (message, None) or (None, skip_reason). Caller commits.
    offline=True: WhatsApp is down right now; the message is kept as waiting (see WAITING) instead of skipped."""
    cfg = cfg or get_config(db, gym.id)
    if not offline and not is_connected(cfg):
        return None, "not_connected"
    if trigger == "auto" and not wa_settings(cfg)["automations"].get(event, False):
        return None, "automation_off"
    tpl = tpl or db.scalar(select(WaTemplate).where(WaTemplate.gym_id == gym.id, WaTemplate.key == event))
    if tpl is None and event in EVENTS:   # a message type added after this gym linked WhatsApp
        tpl = next((t for t in ensure_templates(db, gym.id) if t.key == event), None)
        if tpl is None:   # another message created it a moment ago; our snapshot can't see it yet
            raise _TemplateRace()
    if not template_ready(cfg, tpl):
        return None, "template_not_approved"
    if member.status != "active":
        return None, "archived"
    if member.wa_opted_out_at is not None:
        return None, "opted_out"
    if tpl.category == "MARKETING" and cfg.consent_confirmed_at is None:
        return None, "no_consent"
    user = user or db.get(User, member.user_id)
    to = wa_number(user.phone if user else None)
    if not to:
        return None, "no_phone"
    if dedupe and db.scalar(select(WaMessage.id).where(WaMessage.gym_id == gym.id, WaMessage.dedupe_key == dedupe)):
        return None, "duplicate"
    v = build_vars(db, gym, member, ms, extra)
    link = f"{settings.renew_url_base.rstrip('/')}/…" if tpl.button_text else None
    m = WaMessage(id=uuid7(), gym_id=gym.id, member_id=member.id, membership_id=ms.id if ms else None, phone=to,
                  event=event, category=tpl.category, template_key=tpl.key, vars=v, body=render(tpl.body, v, link),
                  status="queued", next_attempt_at=utcnow(), dedupe_key=dedupe, batch_id=batch_id, trigger=trigger,
                  created_by=actor_id)
    if offline:
        m.status, m.error_code, m.error, m.next_attempt_at = "cancelled", "disconnected", WAITING_TEXT, None
    try:
        with db.begin_nested():
            db.add(m)
            db.flush()
    except IntegrityError:  # another worker queued the same reminder a moment ago
        return None, "duplicate"
    return m, None


class _TemplateRace(Exception):
    pass


def fire(gym_id: str, event: str, member_id: str, membership_id: str | None = None, extra: dict | None = None,
         dedupe: str | None = None, _try: int = 0):
    """Automation trigger used after a business event (runs in its own session; never raises).
    Retried in a fresh session if it lost a race to create a template (dedupe keys stop doubles)."""
    try:
        # Templates first, in their own short transaction with the gym's WhatsApp row locked, so messages fired
        # at the same moment (welcome + receipt + go-live) never race or deadlock creating the same rows.
        with session_scope() as db:
            if db.scalar(select(GymWhatsApp.gym_id).where(GymWhatsApp.gym_id == gym_id).with_for_update()):
                if not db.scalar(select(WaTemplate.id).where(WaTemplate.gym_id == gym_id, WaTemplate.key == event)):
                    ensure_templates(db, gym_id)
        with session_scope() as db:
            cfg = get_config(db, gym_id)
            online = is_connected(cfg)
            if not online and not (cfg and cfg.connected_at):   # never linked: nothing to send through
                return
            gym, member = db.get(Gym, gym_id), db.get(GymMember, member_id)
            if not gym or not member or member.gym_id != gym_id:
                return
            ms = db.get(Membership, membership_id) if membership_id else latest_membership(db, member_id)
            m, _ = queue_message(db, gym, member, event, ms=ms, dedupe=dedupe, extra=extra, cfg=cfg, offline=not online)
        if m is not None and online:
            from app.services.jobs import enqueue
            enqueue("wa.send_now", process_queue)   # don't wait for the next sender tick
    except Exception as e:  # an automation must never break the action that triggered it
        if _try < 3 and isinstance(e, (_TemplateRace, IntegrityError, OperationalError)):
            time.sleep(0.4 * (_try + 1))
            return fire(gym_id, event, member_id, membership_id, extra, dedupe, _try + 1)
        log.warning("whatsapp trigger %s failed: %s", event, type(e).__name__)


def fire_later(gym_id: str, event: str, member_id: str, membership_id: str | None = None, extra: dict | None = None,
               dedupe: str | None = None):
    from app.services.jobs import enqueue

    def run():
        time.sleep(1.5)  # let the request that triggered this commit first
        fire(gym_id, event, member_id, membership_id, extra, dedupe)
    enqueue("wa.trigger", run)


# ---------------------------------------------------------------- who is eligible
def expiry_targets(db: Session, gym: Gym, days: list[int] | None = None, window: tuple[int, int] | None = None
                   ) -> list[tuple[Membership, GymMember, User, int]]:
    """Latest membership per active member that ends on today+N (N<0 = already ended). Members who already hold a
    later membership are left out. Returns (membership, member, user, days_left)."""
    today = today_in(gym.timezone)
    q = select(Membership, GymMember, User).join(GymMember, GymMember.id == Membership.member_id).join(
        User, User.id == GymMember.user_id).where(
        Membership.gym_id == gym.id, Membership.state != "cancelled", GymMember.status == "active",
        GymMember.claim_disputed_at.is_(None))
    if days is not None:
        q = q.where(Membership.end_date.in_([today + timedelta(days=d) for d in days] or [today - timedelta(days=9999)]))
    elif window is not None:
        q = q.where(Membership.end_date >= today + timedelta(days=window[0]),
                    Membership.end_date <= today + timedelta(days=window[1]))
    rows = db.execute(q.order_by(Membership.end_date)).all()
    if not rows:
        return []
    later = {}
    for mid, end in db.execute(select(Membership.member_id, func.max(Membership.end_date)).where(
            Membership.member_id.in_({gm.id for _, gm, _ in rows}), Membership.state != "cancelled")
            .group_by(Membership.member_id)):
        later[mid] = end
    out, seen = [], set()
    for ms, gm, u in rows:
        if later.get(gm.id) and later[gm.id] > ms.end_date:  # renewed already
            continue
        if gm.id in seen:
            continue
        seen.add(gm.id)
        out.append((ms, gm, u, (ms.end_date - today).days))
    return out


def run_expiry_automation(force: bool = False) -> int:
    """Hourly: for each connected gym at its send hour, queue expiring / expired / pending-payment reminders.
    Safe to run any number of times — dedupe keys stop repeats."""
    queued = 0
    with session_scope() as db:
        cfgs = db.scalars(select(GymWhatsApp).where(GymWhatsApp.status == "connected")).all()
        for cfg in cfgs:
            gym = db.get(Gym, cfg.gym_id)
            if not gym or gym.status != "active":
                continue
            s = wa_settings(cfg)
            from app.utils import tz
            hour = utcnow().astimezone(tz(gym.timezone)).hour
            try:  # every hour: members added or imported since the last run get their one-time message
                ensure_templates(db, gym.id)
                queued += revive_waiting(db, gym.id)
                queued += queue_onboarding(db, gym, cfg)
                db.commit()
            except Exception as e:
                db.rollback()
                log.warning("whatsapp onboarding failed: %s", type(e).__name__)
            if not force and hour != int(s.get("send_hour", 10)):
                continue
            tpls = {t.key: t for t in db.scalars(select(WaTemplate).where(WaTemplate.gym_id == gym.id))}
            auto = s["automations"]
            if auto.get("expiry_reminder"):
                for ms, gm, u, left in expiry_targets(db, gym, days=[int(d) for d in s["expiry_days_before"]]):
                    m, _ = queue_message(db, gym, gm, "expiry_reminder", ms=ms, dedupe=f"expiry:{ms.id}:{left}",
                                         cfg=cfg, tpl=tpls.get("expiry_reminder"), user=u)
                    queued += bool(m)
            if auto.get("expired"):
                for ms, gm, u, left in expiry_targets(db, gym, days=[-int(d) for d in s["expired_days_after"]]):
                    m, _ = queue_message(db, gym, gm, "expired", ms=ms, dedupe=f"expired:{ms.id}:{-left}",
                                         cfg=cfg, tpl=tpls.get("expired"), user=u)
                    queued += bool(m)
            if auto.get("payment_due"):
                from app.services.rules import load_states
                every = max(1, int(s.get("due_every_days", 7)))
                bucket = today_in(gym.timezone).toordinal() // every
                states = load_states(db, gym)
                due_ids = [mid for mid, st in states.items() if st.due_paise > 0]
                for gm in db.scalars(select(GymMember).where(GymMember.id.in_(due_ids or [""]))):
                    ms = latest_membership(db, gm.id)
                    m, _ = queue_message(db, gym, gm, "payment_due", ms=ms, dedupe=f"due:{gm.id}:{bucket}", cfg=cfg,
                                         tpl=tpls.get("payment_due"),
                                         extra={"membership_amount": rupees(states[gm.id].due_paise)})
                    queued += bool(m)
            if auto.get("away_nudge"):
                cut = utcnow() - timedelta(days=14)
                month = today_in(gym.timezone).strftime("%Y%m")
                live = {gm.id for _, gm, _, _ in expiry_targets(db, gym, window=(0, 3650))}
                for gm in db.scalars(select(GymMember).where(
                        GymMember.gym_id == gym.id, GymMember.status == "active", GymMember.id.in_(live or [""]),
                        or_(GymMember.last_checkin_at < cut, GymMember.last_checkin_at.is_(None)),
                        GymMember.created_at < cut)):
                    m, _ = queue_message(db, gym, gm, "away_nudge", ms=latest_membership(db, gm.id),
                                         dedupe=f"away:{gm.id}:{month}", cfg=cfg, tpl=tpls.get("away_nudge"))
                    queued += bool(m)
            db.commit()
    if queued:
        log.info("whatsapp automation queued %s message(s)", queued)
    return queued


# ---------------------------------------------------------------- "your gym is on DUMBBEL.fit" (once per member)
LIVE_EVENT = "gym_live"
LIVE_RECENT_DAYS = 30     # members whose membership ended longer ago than this are not messaged
LIVE_CAP_SHARE = 0.75     # of the daily limit on a linked number; the rest is kept for reminders


def queue_onboarding(db: Session, gym: Gym, cfg: GymWhatsApp, tpl: WaTemplate | None = None) -> int:
    """Queue the one-time "your gym is on DUMBBEL.fit" message for every member who has not had it: active members
    with a membership that is current or ended in the last 30 days. Safe to call any number of times: the dedupe
    key `live:<member>` allows one per member, ever. Caller commits."""
    if not is_connected(cfg) or not wa_settings(cfg)["automations"].get(LIVE_EVENT, False):
        return 0
    tpl = tpl or db.scalar(select(WaTemplate).where(WaTemplate.gym_id == gym.id, WaTemplate.key == LIVE_EVENT))
    if not template_ready(cfg, tpl):
        return 0
    done = set(db.scalars(select(WaMessage.dedupe_key).where(WaMessage.gym_id == gym.id, WaMessage.event == LIVE_EVENT)))
    targets = [t for t in expiry_targets(db, gym, window=(-LIVE_RECENT_DAYS, 36500)) if f"live:{t[1].id}" not in done]
    step = (settings.wa_web_min_gap_sec + settings.wa_web_max_gap_sec) / 2 if cfg.provider == "web" else 0
    now, n = utcnow(), 0
    for ms, gm, u, _ in targets:
        m, _why = queue_message(db, gym, gm, LIVE_EVENT, ms=ms, dedupe=f"live:{gm.id}", cfg=cfg, tpl=tpl, user=u)
        if m:
            # spread out in send order, so a reminder queued later is not stuck behind hundreds of these
            m.next_attempt_at = now + timedelta(seconds=step * n)
            n += 1
    return n


WAITING_TEXT = "WhatsApp wasn’t connected. Sent automatically if WhatsApp is linked again within 24 hours."
WAITING_HOURS = 24
WAITING_CODES = ("disconnected", "not_connected")


def revive_waiting(db: Session, gym_id: str) -> int:
    """WhatsApp is back: queue again what couldn't go while it was down. Welcome, payment and reminder messages
    only if they are under 24 hours old (a receipt days later is confusing); the one-time go-live message always,
    because otherwise that member would never get it (its dedupe key blocks a new one). Caller commits."""
    cutoff = utcnow() - timedelta(hours=WAITING_HOURS)
    r = db.execute(update(WaMessage).where(
        WaMessage.gym_id == gym_id, WaMessage.status.in_(("cancelled", "failed")), WaMessage.error_code.in_(WAITING_CODES),
        WaMessage.trigger == "auto", or_(WaMessage.event == LIVE_EVENT, WaMessage.created_at >= cutoff))
        .values(status="queued", next_attempt_at=utcnow(), error=None, error_code=None, attempts=0, failed_at=None)
        .execution_options(synchronize_session=False))
    return r.rowcount or 0


def onboard_gym(gym_id: str) -> int:
    """Own session; never raises. Used after WhatsApp is linked and after an import finishes."""
    try:
        with session_scope() as db:
            cfg, gym = get_config(db, gym_id), db.get(Gym, gym_id)
            if not cfg or not gym or gym.status != "active":
                return 0
            ensure_templates(db, gym_id)
            back = revive_waiting(db, gym_id) if is_connected(cfg) else 0
            n = queue_onboarding(db, gym, cfg)
        if back:
            log.info("whatsapp: %s waiting message(s) queued again for gym %s", back, gym_id)
        if n:
            log.info("whatsapp onboarding queued %s message(s) for gym %s", n, gym_id)
        return n
    except Exception as e:
        log.warning("whatsapp onboarding failed: %s", type(e).__name__)
        return 0


def onboard_later(gym_id: str):
    from app.services.jobs import enqueue

    def run():
        time.sleep(2)  # let the request that triggered this commit first
        onboard_gym(gym_id)
    enqueue("wa.onboard", run)


# ---------------------------------------------------------------- sending (worker)
def _send_web(db: Session, m: WaMessage, cfg: GymWhatsApp, tpl: WaTemplate):
    link = None
    if tpl.button_text and m.member_id:
        raw = new_renewal_token(db, m.gym_id, m.member_id, m.id)
        link = f"{settings.renew_url_base.rstrip('/')}/{raw}"
    text = render(tpl.body, m.vars or {})
    if link:
        text += f"\n\n{tpl.button_text}: {link}"
    if tpl.category == "MARKETING" and "STOP" not in text:
        text += "\n\nReply STOP to stop these messages."
    d = evo("POST", f"/message/sendText/{cfg.instance_name}",
            json={"number": m.phone, "text": text, "linkPreview": False, "delay": random.randint(1200, 3500)}, timeout=45)
    wamid = ((d.get("key") or {}).get("id")) if isinstance(d, dict) else None
    if not wamid:
        raise MetaError("WhatsApp didn’t confirm the message.", transient=True)
    m.wamid, m.status, m.sent_at, m.error, m.error_code = wamid, "sent", utcnow(), None, None


def _send_one(db: Session, m: WaMessage, cfg: GymWhatsApp, tpl: WaTemplate, token: str):
    names = template_vars(tpl.body)
    v = m.vars or {}
    comps = []
    if names:
        comps.append({"type": "body", "parameters": [{"type": "text", "text": _clean_param(v.get(n))} for n in names]})
    link = None
    if tpl.button_text:
        raw = new_renewal_token(db, m.gym_id, m.member_id, m.id)
        link = f"{settings.renew_url_base.rstrip('/')}/{raw}"
        comps.append({"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": raw}]})
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": m.phone, "type": "template",
               "template": {"name": tpl.meta_name, "language": {"code": tpl.language}, "components": comps}}
    d = graph("POST", f"{cfg.phone_number_id}/messages", token, json=payload)
    wamid = ((d.get("messages") or [{}])[0]).get("id")
    if not wamid:
        raise MetaError("WhatsApp didn’t return a message id.", transient=True)
    m.wamid, m.status, m.sent_at, m.error, m.error_code = wamid, "sent", utcnow(), None, None
    del link


BACKOFF = [60, 300, 1800, 7200]  # seconds before attempts 2..5


_queue_lock = threading.Lock()


def _web_hold(db: Session, cfg: GymWhatsApp, gym: Gym, trigger: str, event: str | None = None):
    """Linked numbers: quiet hours for automatic messages and a daily cap. Returns a UTC datetime to retry at, or None."""
    from app.utils import tz
    z = tz(gym.timezone)
    local = utcnow().astimezone(z)
    qs, qe = settings.wa_web_quiet_start, settings.wa_web_quiet_end
    if trigger == "auto" and event not in INSTANT_EVENTS and (local.hour >= qs or local.hour < qe):
        nxt = local.replace(hour=qe, minute=random.randint(0, 20), second=0, microsecond=0)
        if local.hour >= qs:
            nxt += timedelta(days=1)
        return nxt.astimezone(utcnow().tzinfo)
    start = local_midnight_utc(local.date(), gym.timezone)
    sent = db.scalar(select(func.count()).select_from(WaMessage).where(
        WaMessage.gym_id == cfg.gym_id, WaMessage.sent_at >= start)) or 0
    limit = settings.wa_web_daily_limit
    if event == LIVE_EVENT:
        limit = int(limit * LIVE_CAP_SHARE)
    if sent >= limit:
        nxt = (local + timedelta(days=1)).replace(hour=qe, minute=random.randint(0, 30), second=0, microsecond=0)
        return nxt.astimezone(utcnow().tzinfo)
    return None


def process_queue(limit: int = 60) -> int:
    """Send due messages. Rows are claimed with SKIP LOCKED so two workers never send the same message.
    Linked (web) numbers send one message at a time with a random 12–35 s gap, like a person would."""
    if not _queue_lock.acquire(blocking=False):
        return 0  # another loop in this process is already sending
    try:
        return _process_queue(limit)
    finally:
        _queue_lock.release()


def _process_queue(limit: int) -> int:
    now = utcnow()
    with session_scope() as db:
        # release rows a crashed worker left in "sending"
        db.execute(update(WaMessage).where(WaMessage.status == "sending", WaMessage.updated_at < now - timedelta(minutes=10))
                   .values(status="queued", next_attempt_at=now))
        rows = db.execute(select(WaMessage.id, WaMessage.gym_id).where(
            WaMessage.status == "queued", WaMessage.next_attempt_at <= now)
            .order_by(case((WaMessage.event.in_(INSTANT_EVENTS), 0), (WaMessage.event == LIVE_EVENT, 2), else_=1),
                      WaMessage.next_attempt_at)  # receipts first, then reminders, then the one-time message
            .limit(400).with_for_update(skip_locked=True)).all()
        if not rows:
            return 0
        providers = dict(db.execute(select(GymWhatsApp.gym_id, GymWhatsApp.provider).where(
            GymWhatsApp.gym_id.in_({g for _, g in rows}))).all())
        web_gyms = {g for _, g in rows if providers.get(g) == "web"}
        # Linked numbers: one message per number per pass, only once its random gap has passed. The gap lives in the
        # database and the gym row is locked, so several workers/instances still can't burst one number.
        web_ok = set()
        if web_gyms:
            for cfg in db.scalars(select(GymWhatsApp).where(GymWhatsApp.gym_id.in_(web_gyms))
                                  .with_for_update(skip_locked=True)):
                if cfg.next_send_at is None or cfg.next_send_at <= now:
                    cfg.next_send_at = now + timedelta(seconds=random.uniform(settings.wa_web_min_gap_sec,
                                                                               settings.wa_web_max_gap_sec))
                    web_ok.add(cfg.gym_id)
        ids, web_taken = [], set()
        for mid, gid in rows:
            if gid in web_gyms:
                if gid not in web_ok or gid in web_taken:
                    continue
                web_taken.add(gid)
            ids.append(mid)
            if len(ids) >= limit:
                break
        if not ids:
            return 0
        db.execute(update(WaMessage).where(WaMessage.id.in_(ids)).values(status="sending"))
    sent = 0
    gap = 1.0 / max(1, settings.wa_send_per_sec)
    cfg_cache: dict[str, tuple] = {}
    for mid in ids:
        started = time.monotonic()
        try:
            with session_scope() as db:
                m = db.get(WaMessage, mid)
                if not m or m.status != "sending":
                    continue
                if m.gym_id not in cfg_cache:
                    cfg = get_config(db, m.gym_id)
                    cfg_cache[m.gym_id] = (cfg.status if cfg else None, decrypt(cfg.token_enc) if cfg else None)
                cfg = get_config(db, m.gym_id)
                token = cfg_cache[m.gym_id][1]
                member = db.get(GymMember, m.member_id) if m.member_id else None
                tpl = db.scalar(select(WaTemplate).where(WaTemplate.gym_id == m.gym_id, WaTemplate.key == m.template_key))
                m.attempts += 1
                if not is_connected(cfg) or (cfg.provider != "web" and not token):
                    _fail(m, "not_connected", "WhatsApp was disconnected before this could be sent.")
                elif member is not None and member.wa_opted_out_at is not None:
                    _fail(m, "opted_out", "The member opted out before this was sent.", status="cancelled")
                elif member is not None and member.status != "active" and m.trigger == "auto":
                    _fail(m, "archived", "The member was archived before this was sent.", status="cancelled")
                elif not template_ready(cfg, tpl):
                    _fail(m, "template", "The template is no longer approved.")
                elif cfg.provider == "web" and (hold := _web_hold(db, cfg, db.get(Gym, m.gym_id), m.trigger, m.event)):
                    m.status, m.next_attempt_at, m.attempts = "queued", hold, max(0, m.attempts - 1)
                else:
                    try:
                        if cfg.provider == "web":
                            _send_web(db, m, cfg, tpl)
                        else:
                            _send_one(db, m, cfg, tpl, token)
                        sent += 1
                    except MetaError as e:
                        if cfg.provider == "web" and e.status in (400, 404) and "not on whatsapp" not in e.message.lower() \
                                and ("closed" in e.message.lower() or "not connected" in e.message.lower() or e.status == 404 and "instance" in e.message.lower()):
                            cfg.status, cfg.last_error = "error", "WhatsApp was unlinked. Scan the QR code again to resume sending."
                            m.status, m.next_attempt_at = "queued", utcnow() + timedelta(minutes=30)
                        elif e.token_problem and cfg.provider != "web":
                            cfg.status, cfg.last_error = "error", "WhatsApp rejected the access token. Reconnect to resume sending."
                            cfg_cache[m.gym_id] = ("error", None)
                            _fail(m, str(e.code or 190), cfg.last_error)
                        elif e.transient and m.attempts <= len(BACKOFF):
                            m.status, m.error, m.error_code = "queued", e.message, str(e.code or e.status or "")
                            m.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF[m.attempts - 1])
                        else:
                            _fail(m, str(e.code or e.status or ""), e.message)
        except Exception as e:
            log.error("whatsapp send crashed for %s: %s", mid, type(e).__name__)
            with session_scope() as db:
                m = db.get(WaMessage, mid)
                if m and m.status == "sending":
                    m.status, m.next_attempt_at = "queued", utcnow() + timedelta(minutes=5)
        spent = time.monotonic() - started
        if spent < gap:
            time.sleep(gap - spent)
    return sent


# ---------------------------------------------------------------- Evolution webhooks
EVO_STATUS = {"SERVER_ACK": "sent", "DELIVERY_ACK": "delivered", "READ": "read", "PLAYED": "read", "ERROR": "failed"}


def handle_evolution(payload: dict, gym_id: str) -> dict:
    out = {"event": payload.get("event")}
    event = (payload.get("event") or "").lower().replace("_", ".")
    data = payload.get("data") or {}
    with session_scope() as db:
        cfg = get_config(db, gym_id)
        if not cfg or cfg.provider != "web" or cfg.instance_name != payload.get("instance"):
            if event in ("connection.update", "qrcode.updated"):
                log.info("wa link %s: %s ignored (WhatsApp not being linked for this gym now)", gym_id[:8], event)
            return {"ignored": True}
        if event == "qrcode.updated":   # a new QR / pairing code, or "QR code limit reached" (session gave up)
            log.info("wa link %s: new code%s", gym_id[:8], (" — " + str(data.get("message"))[:120]) if data.get("message") else "")
        if event == "connection.update":
            state = data.get("state")
            # one line per linking event, so a failed link shows WhatsApp's reason in the server log
            log.info("wa link %s: state=%s reason=%s was=%s", gym_id[:8], state, data.get("statusReason"), cfg.status)
            if state == "open":
                prof = {}
                try:
                    prof = web_profile(gym_id)
                except MetaError:
                    pass
                wuid = re.sub(r"\D", "", str(data.get("wuid") or "").split("@")[0])
                cfg.display_phone = prof.get("phone") or (("+" + wuid) if wuid else cfg.display_phone)
                cfg.verified_name = prof.get("name") or data.get("profileName") or cfg.verified_name
                if cfg.status != "connected":
                    cfg.connected_at = utcnow()
                    onboard_later(gym_id)
                cfg.status, cfg.last_error, cfg.checked_at = "connected", None, utcnow()
            elif state == "close" and cfg.status == "connected":
                reason = data.get("statusReason")
                cfg.status = "error"
                cfg.last_error = ("WhatsApp was unlinked from the phone. Scan the QR code again to resume sending."
                                  if reason in (401, 403, 440) else "The WhatsApp link dropped. Reconnecting…")
            out["state"] = state
        elif event == "messages.update":
            items = data if isinstance(data, list) else [data]
            n = 0
            for it in items:
                st = EVO_STATUS.get(str(it.get("status") or "").upper())
                if st and it.get("fromMe", True) is not False:
                    n += bool(apply_status(db, cfg, {"id": it.get("keyId") or (it.get("key") or {}).get("id"), "status": st,
                                                     "errors": [{"code": "web", "title": "WhatsApp couldn’t deliver this"}] if st == "failed" else None}))
            out["statuses"] = n
        elif event == "messages.upsert":
            items = data if isinstance(data, list) else [data]
            n = 0
            for it in items:
                key = it.get("key") or {}
                if key.get("fromMe") or "@g.us" in str(key.get("remoteJid") or ""):
                    continue
                msg = it.get("message") or {}
                text = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text") or ""
                jid = key.get("remoteJidAlt") if "@lid" in str(key.get("remoteJid") or "") else key.get("remoteJid")
                frm = re.sub(r"\D", "", str(jid or "").split("@")[0])
                n += bool(apply_inbound(db, cfg, {"from": frm, "type": "text", "text": {"body": text}}))
            out["inbound"] = n
    return out


def _fail(m: WaMessage, code: str, message: str, status: str = "failed"):
    m.status, m.error_code, m.error, m.failed_at, m.next_attempt_at = status, code[:20], message[:500], utcnow(), None


# ---------------------------------------------------------------- webhooks
def apply_status(db: Session, cfg: GymWhatsApp, st: dict) -> bool:
    m = db.scalar(select(WaMessage).where(WaMessage.wamid == st.get("id"), WaMessage.gym_id == cfg.gym_id))
    if not m:
        return False
    new = st.get("status")
    ts = utcnow()
    if new == "failed":
        if m.status in ("delivered", "read"):
            return False
        err = (st.get("errors") or [{}])[0]
        m.status, m.failed_at = "failed", ts
        m.error_code = str(err.get("code") or "")[:20]
        m.error = str((err.get("error_data") or {}).get("details") or err.get("title") or err.get("message") or "Not delivered")[:500]
        return True
    if new not in STATUS_ORDER or STATUS_ORDER.get(m.status, 0) >= STATUS_ORDER[new]:
        if new == "delivered" and m.delivered_at is None and m.status == "read":
            m.delivered_at = ts
        return False  # duplicate or out-of-order event
    m.status = new
    if new == "sent":
        m.sent_at = m.sent_at or ts
    elif new == "delivered":
        m.delivered_at = ts
    elif new == "read":
        m.read_at, m.delivered_at = ts, m.delivered_at or ts
    return True


STOP_WORDS = {"stop", "unsubscribe", "stop promotions", "opt out", "optout", "cancel", "band karo", "no more"}
START_WORDS = {"start", "subscribe", "unstop", "opt in"}


def apply_inbound(db: Session, cfg: GymWhatsApp, msg: dict) -> str | None:
    """Honour STOP / START replies. Returns 'opted_out' / 'opted_in' / None."""
    text = ""
    if msg.get("type") == "text":
        text = (msg.get("text") or {}).get("body") or ""
    elif msg.get("type") == "button":
        text = (msg.get("button") or {}).get("text") or ""
    word = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if word not in STOP_WORDS and word not in START_WORDS:
        return None
    digits = re.sub(r"\D", "", msg.get("from") or "")
    if not digits:
        return None
    members = db.scalars(select(GymMember).join(User, User.id == GymMember.user_id).where(
        GymMember.gym_id == cfg.gym_id, User.phone.in_(["+" + digits, digits]))).all()
    for gm in members:
        if word in STOP_WORDS:
            gm.wa_opted_out_at, gm.wa_opt_source = gm.wa_opted_out_at or utcnow(), "member"
        else:
            gm.wa_opted_out_at, gm.wa_opt_source = None, None
    if word in STOP_WORDS and members:
        db.execute(update(WaMessage).where(WaMessage.gym_id == cfg.gym_id, WaMessage.status == "queued",
                                           WaMessage.member_id.in_([g.id for g in members]))
                   .values(status="cancelled", error="Member opted out", error_code="opted_out"))
    return ("opted_out" if word in STOP_WORDS else "opted_in") if members else None


def handle_webhook(payload: dict) -> dict:
    """Process a verified webhook body. Idempotent: repeated deliveries change nothing."""
    out = {"statuses": 0, "inbound": 0, "templates": 0}
    with session_scope() as db:
        for entry in payload.get("entry") or []:
            for ch in entry.get("changes") or []:
                val, field = ch.get("value") or {}, ch.get("field")
                if field == "messages":
                    pid = (val.get("metadata") or {}).get("phone_number_id")
                    cfg = db.scalar(select(GymWhatsApp).where(GymWhatsApp.phone_number_id == str(pid))) if pid else None
                    if not cfg:
                        continue
                    for st in val.get("statuses") or []:
                        out["statuses"] += bool(apply_status(db, cfg, st))
                    for msg in val.get("messages") or []:
                        out["inbound"] += bool(apply_inbound(db, cfg, msg))
                elif field == "message_template_status_update":
                    cfg = db.scalar(select(GymWhatsApp).where(GymWhatsApp.waba_id == str(entry.get("id"))))
                    if not cfg:
                        continue
                    t = db.scalar(select(WaTemplate).where(WaTemplate.gym_id == cfg.gym_id,
                                                           WaTemplate.meta_name == val.get("message_template_name")))
                    if t:
                        t.status = _tpl_status(val.get("event"))
                        reason = val.get("reason")
                        t.reject_reason = None if not reason or reason == "NONE" else str(reason).replace("_", " ").capitalize()[:300]
                        out["templates"] += 1
                elif field in ("account_update", "phone_number_quality_update"):
                    cfg = db.scalar(select(GymWhatsApp).where(GymWhatsApp.waba_id == str(entry.get("id"))))
                    if cfg and val.get("current_limit"):
                        cfg.quality = str(val.get("event") or cfg.quality or "")[:20]
    return out


def has_upi(db: Session, gym_id: str) -> bool:
    return bool(db.scalar(select(GymUpiAccount.id).where(GymUpiAccount.gym_id == gym_id,
                                                         GymUpiAccount.active.is_(True)).limit(1)))


_ = Plan
