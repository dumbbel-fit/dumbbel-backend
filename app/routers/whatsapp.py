"""WhatsApp for gyms (Meta Cloud API) + personal renewal links.

Gym (dashboard):  /gyms/{gymId}/whatsapp …  connect · settings · templates · reminders · send · history · stats
Public:           /webhooks/whatsapp        Meta delivery receipts, replies (STOP), template reviews — signature checked
                  /renew/{token} …          the member's personal renewal page (plans → UPI → UTR → gym verifies)
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import false, func, or_, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import get_db
from app.deps import GymCtx, perm_ctx
from app.errors import ApiError, validation
from app.models import (Gym, GymMember, GymWhatsApp, Membership, Plan, RenewalToken, UpiPaymentRequest, User, WaMessage,
                        WaTemplate)
from app.responses import DocRoute, ERRORS
from app.services import whatsapp as wa
from app.services.audit import audit
from app.services.cache import rate_limit
from app.services.core import create_membership, default_start, price_preview, settle_credit
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.rules import credit_balance
from app.services.storage import public_url
from app.utils import clamp_limit, decode_cursor, encode_cursor, iso, local_midnight_utc, rupees, today_in, utcnow, uuid7

log = logging.getLogger("dumbbel.whatsapp")
router = APIRouter(tags=["WhatsApp"], route_class=DocRoute, responses=ERRORS)
public = APIRouter(tags=["WhatsApp (public)"], route_class=DocRoute, responses=ERRORS)

MANAGE, VIEW, SEND = "settings.manage", "members.view", "members.edit"


# ---------------------------------------------------------------- serializers
def ser_config(db: Session, gym: Gym, cfg: GymWhatsApp | None) -> dict:
    s = wa.wa_settings(cfg)
    return {"status": cfg.status if cfg else "disconnected", "connected": wa.is_connected(cfg),
            "provider": cfg.provider if cfg else None,
            "web": {"available": wa.web_available(), "daily_limit": settings.wa_web_daily_limit,
                    "quiet_hours": [settings.wa_web_quiet_start, settings.wa_web_quiet_end]},
            "phone": cfg.display_phone if cfg else None, "business_name": cfg.verified_name if cfg else None,
            "quality": cfg.quality if cfg else None, "waba_id": cfg.waba_id if cfg else None,
            "phone_number_id": cfg.phone_number_id if cfg else None, "last_error": cfg.last_error if cfg else None,
            "connected_at": iso(cfg.connected_at) if cfg else None, "checked_at": iso(cfg.checked_at) if cfg else None,
            # linked number dropped for a moment and is reconnecting by itself: still connected, messages wait
            "reconnecting": bool(cfg and cfg.status == "connected" and cfg.link_lost_at),
            "link_lost_at": iso(cfg.link_lost_at) if cfg else None,
            "connected_via": cfg.connected_via if cfg else None,
            "consent_confirmed": bool(cfg and cfg.consent_confirmed_at), "settings": s,
            "platform": {"embedded_signup": bool(settings.meta_app_id and settings.meta_es_config_id and settings.meta_app_secret),
                         "app_id": settings.meta_app_id, "config_id": settings.meta_es_config_id,
                         "graph_version": settings.meta_graph_version,
                         "webhook_ready": bool(settings.meta_app_secret and settings.meta_webhook_verify_token),
                         "webhook_url": f"{settings.public_base_url.rstrip('/')}{settings.api_prefix}/webhooks/whatsapp"},
            "upi_ready": wa.has_upi(db, gym.id), "renew_url_base": settings.renew_url_base}


def ser_tpl_for(cfg: GymWhatsApp | None, t: WaTemplate) -> dict:
    d = ser_tpl(t)
    if cfg and cfg.provider == "web":
        d["status"], d["reject_reason"] = "ready", None  # linked numbers send plain text: no WhatsApp review
    return d


def ser_tpl(t: WaTemplate) -> dict:
    ev = wa.EVENTS[t.key]
    return {"key": t.key, "label": ev["label"], "category": t.category, "body": t.body, "default_body": ev["body"],
            "button_text": t.button_text, "status": t.status, "reject_reason": t.reject_reason,
            "meta_name": t.meta_name, "language": t.language, "variables": wa.template_vars(t.body),
            "manual_only": bool(ev.get("manual_only")), "submitted_at": iso(t.submitted_at),
            "preview": wa.render(t.body, wa.VARS, f"{settings.renew_url_base}/…" if t.button_text else None)}


def ser_msg(m: WaMessage, name: str | None = None, plan: str | None = None) -> dict:
    return {"id": m.id, "member": {"id": m.member_id, "name": name}, "phone": "+" + m.phone, "event": m.event,
            "event_label": wa.EVENTS.get(m.event, {}).get("label", m.event), "category": m.category, "body": m.body,
            "status": m.status, "error": m.error, "error_code": m.error_code, "attempts": m.attempts,
            "trigger": m.trigger, "membership_id": m.membership_id, "plan_name": plan, "batch_id": m.batch_id,
            "created_at": iso(m.created_at), "sent_at": iso(m.sent_at), "delivered_at": iso(m.delivered_at),
            "read_at": iso(m.read_at), "failed_at": iso(m.failed_at), "next_attempt_at": iso(m.next_attempt_at)}


def _cfg_or_409(db: Session, gym_id: str) -> GymWhatsApp:
    cfg = wa.get_config(db, gym_id)
    if not wa.is_connected(cfg):
        raise ApiError(409, "WHATSAPP_NOT_CONNECTED", "Connect WhatsApp first.")
    return cfg


def _meta_fail(e: wa.MetaError, code: str = "WHATSAPP_ERROR"):
    raise ApiError(502 if e.transient else 422, code, e.message, {"meta_code": e.code})


# ---------------------------------------------------------------- connection
@router.get("/gyms/{gymId}/whatsapp", summary="WhatsApp connection, settings and today’s numbers (members.view)")
def get_whatsapp(ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    cfg = wa.get_config(db, ctx.gym.id)
    tpls = wa.ensure_templates(db, ctx.gym.id)
    db.commit()
    out = ser_config(db, ctx.gym, cfg)
    if not ctx.has(MANAGE):
        out["waba_id"] = out["phone_number_id"] = None
    out["templates"] = [ser_tpl_for(cfg, t) for t in tpls]
    out["stats"] = _stats(db, ctx.gym)
    out["can_manage"], out["can_send"] = ctx.has(MANAGE), ctx.has(SEND)
    return out


class ConnectIn(BaseModel):
    waba_id: str = Field(..., pattern=r"^\d{5,25}$", description="WhatsApp Business Account ID")
    phone_number_id: str = Field(..., pattern=r"^\d{5,25}$")
    access_token: str = Field(..., min_length=20, max_length=1000, description="System-user (permanent) token")
    consent_confirmed: bool = Field(False, description="Owner confirms members agreed to get WhatsApp updates")


def _connect(db: Session, ctx: GymCtx, waba_id: str, phone_number_id: str, token: str, via: str, consent: bool):
    other = db.scalar(select(GymWhatsApp).where(GymWhatsApp.phone_number_id == phone_number_id,
                                                GymWhatsApp.gym_id != ctx.gym.id))
    if other:
        raise ApiError(409, "NUMBER_IN_USE", "This WhatsApp number is already connected to another gym.")
    try:
        info = wa.verify_number(token, phone_number_id)
        wa.subscribe_app(token, waba_id)
    except wa.MetaError as e:
        _meta_fail(e, "WHATSAPP_CONNECT_FAILED")
    cfg = wa.get_config(db, ctx.gym.id)
    if not cfg:
        cfg = GymWhatsApp(gym_id=ctx.gym.id)
        db.add(cfg)
    now = utcnow()
    changed_account = cfg.waba_id and cfg.waba_id != waba_id
    if cfg.provider == "web" and cfg.instance_name:
        wa.web_remove(ctx.gym.id)
    cfg.provider, cfg.instance_name = "cloud", None
    cfg.waba_id, cfg.phone_number_id, cfg.token_enc = waba_id, phone_number_id, wa.encrypt(token)
    cfg.display_phone, cfg.verified_name = info.get("display_phone_number"), info.get("verified_name")
    cfg.quality = info.get("quality_rating")
    cfg.status, cfg.last_error, cfg.connected_via = "connected", None, via
    cfg.connected_by, cfg.connected_at, cfg.checked_at = ctx.user.id, now, now
    if consent:
        cfg.consent_confirmed_at = cfg.consent_confirmed_at or now
    tpls = wa.ensure_templates(db, ctx.gym.id)
    if changed_account:  # templates live in the WhatsApp account, so a new account needs them submitted again
        for t in tpls:
            t.status, t.meta_name, t.meta_id, t.reject_reason, t.version = "draft", None, None, None, t.version + 1
    audit(db, ctx.gym.id, ctx.user.id, "whatsapp.connect", "gym", ctx.gym.id,
          f"Connected WhatsApp {cfg.display_phone or phone_number_id}")
    db.commit()
    try:  # pick up templates this account already has approved
        wa.sync_templates(db, cfg)
        db.commit()
    except wa.MetaError:
        db.rollback()
    wa.onboard_later(ctx.gym.id)
    return get_whatsapp(ctx, db)


@router.post("/gyms/{gymId}/whatsapp/connect", summary="Connect a WhatsApp Business number (settings.manage)",
             description="Checks the number with Meta, subscribes this app to the account’s webhooks and stores the "
                         "token encrypted. The token is never returned by any endpoint.")
def connect(body: ConnectIn, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    rate_limit(f"waconnect:{ctx.gym.id}", 10, 3600)
    return _connect(db, ctx, body.waba_id, body.phone_number_id, body.access_token.strip(), "manual", body.consent_confirmed)


class EmbeddedIn(BaseModel):
    code: str = Field(..., min_length=10, max_length=2000)
    waba_id: str = Field(..., pattern=r"^\d{5,25}$")
    phone_number_id: str = Field(..., pattern=r"^\d{5,25}$")
    consent_confirmed: bool = False


@router.post("/gyms/{gymId}/whatsapp/connect/embedded", summary="Finish Meta Embedded Signup (settings.manage)")
def connect_embedded(body: EmbeddedIn, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    rate_limit(f"waconnect:{ctx.gym.id}", 10, 3600)
    try:
        token = wa.exchange_code(body.code)
        try:  # register the number for Cloud API (no-op if it already is)
            wa.graph("POST", f"{body.phone_number_id}/register", token,
                     json={"messaging_product": "whatsapp", "pin": f"{int(hashlib.sha256(ctx.gym.id.encode()).hexdigest(), 16) % 1000000:06d}"})
        except wa.MetaError as e:
            log.info("whatsapp register skipped: code=%s", e.code)
    except wa.MetaError as e:
        _meta_fail(e, "WHATSAPP_CONNECT_FAILED")
    return _connect(db, ctx, body.waba_id, body.phone_number_id, token, "embedded", body.consent_confirmed)


@router.post("/gyms/{gymId}/whatsapp/test", summary="Re-check the connection with Meta (settings.manage)")
def test_connection(ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    cfg = wa.get_config(db, ctx.gym.id)
    if cfg and cfg.provider == "web" and cfg.instance_name:
        rate_limit(f"watest:{ctx.gym.id}", 30, 3600)
        try:
            state = wa.web_state(ctx.gym.id)
            if state != "open":   # ask it to reconnect with the saved session and give it a few seconds
                try:
                    wa.web_connect(ctx.gym.id, None)
                except wa.MetaError:
                    pass
                for _ in range(6):
                    time.sleep(1.5)
                    state = wa.web_state(ctx.gym.id)
                    if state == "open":
                        break
            now = utcnow()
            if state == "open":
                prof = wa.web_profile(ctx.gym.id)
                if cfg.status != "connected":
                    wa.onboard_later(ctx.gym.id)   # queue again what waited while it was down
                cfg.status, cfg.last_error, cfg.link_lost_at = "connected", None, None
                cfg.display_phone = prof.get("phone") or cfg.display_phone
                cfg.verified_name = prof.get("name") or cfg.verified_name
            else:
                cfg.link_lost_at = cfg.link_lost_at or now
                if cfg.status != "connected" or now - cfg.link_lost_at >= wa.LINK_GRACE:
                    cfg.status, cfg.last_error = "error", wa.UNLINKED_TEXT
        except wa.MetaError as e:
            cfg.last_error = e.message
        cfg.checked_at = utcnow()
        db.commit()
        return get_whatsapp(ctx, db)
    if not cfg or not cfg.token_enc:
        raise ApiError(409, "WHATSAPP_NOT_CONNECTED", "Connect WhatsApp first.")
    rate_limit(f"watest:{ctx.gym.id}", 20, 3600)
    try:
        info = wa.verify_number(wa.decrypt(cfg.token_enc), cfg.phone_number_id)
        cfg.status, cfg.last_error = "connected", None
        cfg.display_phone, cfg.verified_name = info.get("display_phone_number"), info.get("verified_name")
        cfg.quality = info.get("quality_rating")
        try:
            wa.sync_templates(db, cfg)
        except wa.MetaError:
            pass
    except wa.MetaError as e:
        if not e.transient:
            cfg.status = "error"
        cfg.last_error = e.message
    cfg.checked_at = utcnow()
    db.commit()
    return get_whatsapp(ctx, db)


@router.delete("/gyms/{gymId}/whatsapp", summary="Disconnect WhatsApp and erase the stored token (settings.manage)")
def disconnect(ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    cfg = wa.get_config(db, ctx.gym.id)
    if cfg:
        if cfg.provider == "web" and cfg.instance_name:
            wa.web_remove(ctx.gym.id)  # unlinks the device from the phone too
            cfg.instance_name = None
        cfg.token_enc, cfg.status, cfg.last_error = None, "disconnected", None
        db.execute(update(WaMessage).where(WaMessage.gym_id == ctx.gym.id, WaMessage.status.in_(("queued", "sending")))
                   .values(status="cancelled", error="WhatsApp was disconnected", error_code="disconnected"))
        audit(db, ctx.gym.id, ctx.user.id, "whatsapp.disconnect", "gym", ctx.gym.id, "Disconnected WhatsApp")
        db.commit()
    return get_whatsapp(ctx, db)


class SettingsIn(BaseModel):
    send_hour: int | None = Field(None, ge=6, le=21, description="Local hour reminders go out (6–21)")
    expiry_days_before: list[int] | None = Field(None, max_length=6)
    expired_days_after: list[int] | None = Field(None, max_length=4)
    due_every_days: int | None = Field(None, ge=3, le=30)
    automations: dict[str, bool] | None = None
    consent_confirmed: bool | None = None


@router.patch("/gyms/{gymId}/whatsapp/settings", summary="Automation settings (settings.manage)")
def patch_settings(body: SettingsIn, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    cfg = wa.get_config(db, ctx.gym.id)
    if not cfg:
        cfg = GymWhatsApp(gym_id=ctx.gym.id)
        db.add(cfg)
    s = wa.wa_settings(cfg)
    data = body.model_dump(exclude_unset=True)
    if data.get("expiry_days_before") is not None:
        d = sorted({int(x) for x in data["expiry_days_before"]}, reverse=True)
        if any(x < 0 or x > 30 for x in d):
            raise validation({"expiry_days_before": "Use days between 0 and 30."})
        s["expiry_days_before"] = d
    if data.get("expired_days_after") is not None:
        d = sorted({int(x) for x in data["expired_days_after"]})
        if any(x < 1 or x > 30 for x in d):
            raise validation({"expired_days_after": "Use days between 1 and 30."})
        s["expired_days_after"] = d
    for k in ("send_hour", "due_every_days"):
        if data.get(k) is not None:
            s[k] = data[k]
    if data.get("automations"):
        bad = [k for k in data["automations"] if k not in wa.AUTO_EVENTS]
        if bad:
            raise validation({"automations": f"Unknown automation: {bad[0]}"})
        s["automations"] = {**s["automations"], **data["automations"]}
    if data.get("consent_confirmed") is not None:
        cfg.consent_confirmed_at = (cfg.consent_confirmed_at or utcnow()) if data["consent_confirmed"] else None
    cfg.settings = s
    audit(db, ctx.gym.id, ctx.user.id, "whatsapp.settings", "gym", ctx.gym.id, "Updated WhatsApp automation settings")
    db.commit()
    return get_whatsapp(ctx, db)


# ---------------------------------------------------------------- link a normal WhatsApp with a QR (Evolution API)
class WebLinkIn(BaseModel):
    pairing_phone: str | None = Field(None, description="Link with an 8-character code instead of a QR (10-digit number)")
    risk_ack: bool = Field(..., description="Owner accepts that WhatsApp may restrict numbers that send automated messages")
    consent_confirmed: bool = False


def _web_status(db: Session, ctx: GymCtx, cfg: GymWhatsApp, link: dict | None = None) -> dict:
    out = {"state": "open" if cfg.status == "connected" else (link or {}).get("state", "connecting"),
           "qr_base64": (link or {}).get("qr_base64"), "pairing_code": (link or {}).get("pairing_code"),
           "connected": wa.is_connected(cfg), "phone": cfg.display_phone, "name": cfg.verified_name}
    return out


@router.post("/gyms/{gymId}/whatsapp/web/link", summary="Start linking the gym’s WhatsApp by QR code (settings.manage)",
             description="Creates this gym’s linked-device session and returns a QR (base64 PNG) to scan in WhatsApp → "
                         "Linked devices, or an 8-character pairing code when `pairing_phone` is given. Poll "
                         "GET /whatsapp/web/link until `connected` is true.")
def web_link(body: WebLinkIn, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    if not wa.web_available():
        raise ApiError(409, "WEB_LINK_UNAVAILABLE", "QR linking isn’t set up on this server yet.")
    if not body.risk_ack:
        raise validation({"risk_ack": "Please confirm you understand how linking works."})
    rate_limit(f"walink:{ctx.gym.id}", 20, 3600)
    pairing = None
    if body.pairing_phone:
        digits = "".join(ch for ch in body.pairing_phone if ch.isdigit())[-10:]
        if len(digits) != 10:
            raise validation({"pairing_phone": "Enter the 10-digit WhatsApp number."})
        pairing = "91" + digits
    cfg = wa.get_config(db, ctx.gym.id)
    if not cfg:
        cfg = GymWhatsApp(gym_id=ctx.gym.id)
        db.add(cfg)
    if cfg.provider != "web" and cfg.token_enc:  # switching from the Meta API: drop that token
        cfg.token_enc, cfg.waba_id, cfg.phone_number_id = None, None, None
    try:  # a linked number is only ever reconnected here, never thrown away
        link = wa.web_create(ctx.gym.id, pairing, keep_session=cfg.status == "connected")
    except wa.MetaError as e:
        _meta_fail(e, "WHATSAPP_LINK_FAILED")
    cfg.provider, cfg.instance_name = "web", wa.instance_name(ctx.gym.id)
    if cfg.status != "connected":
        cfg.status, cfg.last_error = "disconnected", None
    if body.consent_confirmed:
        cfg.consent_confirmed_at = cfg.consent_confirmed_at or utcnow()
    cfg.connected_via, cfg.connected_by = "qr", ctx.user.id
    wa.ensure_templates(db, ctx.gym.id)
    audit(db, ctx.gym.id, ctx.user.id, "whatsapp.link", "gym", ctx.gym.id, "Started linking WhatsApp by QR code")
    db.commit()
    return _web_status(db, ctx, cfg, link)


@router.get("/gyms/{gymId}/whatsapp/web/link", summary="Linking progress: fresh QR / pairing code, or connected")
def web_link_status(pairing_phone: str | None = None, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    cfg = wa.get_config(db, ctx.gym.id)
    if not cfg or cfg.provider != "web" or not cfg.instance_name:
        raise ApiError(409, "NOT_LINKING", "Start linking first.")
    try:
        state = wa.web_state(ctx.gym.id)
    except wa.MetaError as e:
        _meta_fail(e, "WHATSAPP_LINK_FAILED")
    if state == "open":
        wa._PAIRING.pop(ctx.gym.id, None)
        if cfg.status != "connected":
            prof = {}
            try:
                prof = wa.web_profile(ctx.gym.id)
            except wa.MetaError:
                pass
            phone = prof.get("phone")
            other = phone and db.scalar(select(GymWhatsApp).where(GymWhatsApp.display_phone == phone,
                                                                  GymWhatsApp.gym_id != ctx.gym.id,
                                                                  GymWhatsApp.status == "connected"))
            if other:
                wa.web_remove(ctx.gym.id)
                cfg.instance_name, cfg.status = None, "disconnected"
                db.commit()
                raise ApiError(409, "NUMBER_IN_USE", "This WhatsApp number is already linked to another gym.")
            cfg.status, cfg.last_error, cfg.connected_at, cfg.checked_at = "connected", None, utcnow(), utcnow()
            cfg.display_phone, cfg.verified_name = phone, prof.get("name")
            audit(db, ctx.gym.id, ctx.user.id, "whatsapp.connect", "gym", ctx.gym.id,
                  f"Linked WhatsApp {phone or ''} by QR code")
            db.commit()
            wa.onboard_later(ctx.gym.id)
        return _web_status(db, ctx, cfg, {"state": "open"})
    pairing = wa.pairing_for(ctx.gym.id)   # remembered from "Start linking", so a restarted session keeps code mode
    if pairing_phone and not pairing:
        digits = "".join(ch for ch in pairing_phone if ch.isdigit())[-10:]
        pairing = "91" + digits if len(digits) == 10 else None
    try:
        link = wa.web_connect(ctx.gym.id, pairing)
    except wa.MetaError as e:
        _meta_fail(e, "WHATSAPP_LINK_FAILED")
    return _web_status(db, ctx, cfg, link)


@public.post("/webhooks/evolution", include_in_schema=False)
async def evolution_webhook(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", "Invalid JSON.")
    inst = str(payload.get("instance") or "")
    if not inst.startswith("dmbl-"):
        raise ApiError(403, "FORBIDDEN", "Unknown instance.")
    gym_id = inst[5:]
    tok = request.headers.get("x-dumbbel-token") or ""
    if not hmac.compare_digest(tok, wa.web_hook_secret(gym_id)):
        raise ApiError(403, "BAD_SIGNATURE", "Token check failed.")
    try:
        res = await run_in_threadpool(wa.handle_evolution, payload, gym_id)
        if res.get("statuses") or res.get("inbound") or res.get("state"):
            log.info("evolution webhook: %s", res)
    except Exception as e:
        log.error("evolution webhook failed: %s", type(e).__name__)
        raise ApiError(500, "WEBHOOK_FAILED", "Temporary error.")
    return {"ok": True}


# ---------------------------------------------------------------- templates
class TemplateIn(BaseModel):
    body: str = Field(..., min_length=20, max_length=1000)


def _tpl(db: Session, gym_id: str, key: str) -> WaTemplate:
    if key not in wa.EVENTS:
        raise ApiError(404, "NOT_FOUND", "That template doesn’t exist.")
    wa.ensure_templates(db, gym_id)
    return db.scalar(select(WaTemplate).where(WaTemplate.gym_id == gym_id, WaTemplate.key == key))


@router.patch("/gyms/{gymId}/whatsapp/templates/{key}", summary="Edit a template’s wording (settings.manage)",
              description="Changing the text makes it a new draft: it must be approved by WhatsApp again before it "
                          "can be sent. Until then the automation for it is paused.")
def edit_template(key: str, body: TemplateIn, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    t = _tpl(db, ctx.gym.id, key)
    text = body.body.strip()
    err = wa.validate_template_body(text)
    if err:
        raise validation({"body": err})
    if text != t.body:
        t.body, t.status, t.reject_reason = text, "draft", None
        t.version, t.meta_name, t.meta_id = t.version + 1, None, None
        if (cfg := wa.get_config(db, ctx.gym.id)) and cfg.provider == "web":
            db.commit()
            return ser_tpl_for(cfg, t)
        audit(db, ctx.gym.id, ctx.user.id, "whatsapp.template", "gym", ctx.gym.id, f"Edited WhatsApp template “{wa.EVENTS[key]['label']}”")
    db.commit()
    return ser_tpl(t)


@router.post("/gyms/{gymId}/whatsapp/templates/{key}/submit", summary="Send a template to WhatsApp for approval")
def submit_template(key: str, ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    cfg = _cfg_or_409(db, ctx.gym.id)
    if cfg.provider == "web":
        return ser_tpl_for(cfg, _tpl(db, ctx.gym.id, key))
    t = _tpl(db, ctx.gym.id, key)
    if t.status in ("pending", "approved"):
        return ser_tpl(t)
    rate_limit(f"watpl:{ctx.gym.id}", 30, 3600)
    try:
        wa.submit_template(db, cfg, t)
    except wa.MetaError as e:
        if e.code == 2388024 or "already exists" in e.message.lower():  # name taken by an older review: bump and retry once
            t.version += 1
            try:
                wa.submit_template(db, cfg, t)
            except wa.MetaError as e2:
                db.commit()
                _meta_fail(e2, "TEMPLATE_REJECTED")
        else:
            _meta_fail(e, "TEMPLATE_REJECTED")
    db.commit()
    return ser_tpl(t)


@router.post("/gyms/{gymId}/whatsapp/templates/submit-all", summary="Submit every draft template for approval")
def submit_all(ctx: GymCtx = Depends(perm_ctx(MANAGE)), db: Session = Depends(get_db)):
    cfg = _cfg_or_409(db, ctx.gym.id)
    if cfg.provider == "web":
        return {"items": [ser_tpl_for(cfg, t) for t in wa.ensure_templates(db, ctx.gym.id)], "errors": {}}
    rate_limit(f"watpl:{ctx.gym.id}", 30, 3600)
    errors = {}
    for t in wa.ensure_templates(db, ctx.gym.id):
        if t.status in ("draft", "rejected"):
            try:
                wa.submit_template(db, cfg, t)
            except wa.MetaError as e:
                errors[t.key] = e.message
    db.commit()
    return {"items": [ser_tpl(t) for t in wa.ensure_templates(db, ctx.gym.id)], "errors": errors}


@router.post("/gyms/{gymId}/whatsapp/templates/sync", summary="Refresh approval status from WhatsApp")
def sync_templates(ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    cfg = _cfg_or_409(db, ctx.gym.id)
    if cfg.provider == "web":
        return {"items": [ser_tpl_for(cfg, t) for t in wa.ensure_templates(db, ctx.gym.id)]}
    rate_limit(f"watplsync:{ctx.gym.id}", 60, 3600)
    try:
        wa.sync_templates(db, cfg)
    except wa.MetaError as e:
        _meta_fail(e)
    db.commit()
    return {"items": [ser_tpl(t) for t in wa.ensure_templates(db, ctx.gym.id)]}


# ---------------------------------------------------------------- reminders: who, preview, send
VIEWS = {"expiring": (0, 7), "expired": (-30, -1), "today": (0, 0), "month": (0, 30)}


def _eligible(db: Session, gym: Gym, view: str, event: str):
    lo, hi = VIEWS[view]
    rows = wa.expiry_targets(db, gym, window=(lo, hi))
    last = {}
    ids = [gm.id for _, gm, _, _ in rows]
    if ids:
        for mid, ev, st, at in db.execute(select(WaMessage.member_id, WaMessage.event, WaMessage.status, WaMessage.created_at)
                                          .where(WaMessage.gym_id == gym.id, WaMessage.member_id.in_(ids),
                                                 WaMessage.event.in_(("expiry_reminder", "expired")),
                                                 WaMessage.created_at > utcnow() - timedelta(days=30))
                                          .order_by(WaMessage.created_at)):
            last[mid] = {"status": st, "at": iso(at), "event": ev}
    today = today_in(gym.timezone)
    start = local_midnight_utc(today, gym.timezone)
    sent_today = set(db.scalars(select(WaMessage.member_id).where(
        WaMessage.gym_id == gym.id, WaMessage.member_id.in_(ids or [""]), WaMessage.created_at >= start,
        WaMessage.event == event, WaMessage.status.notin_(("failed", "cancelled")))))
    out = []
    for ms, gm, u, left in rows:
        reason = None
        if gm.wa_opted_out_at is not None:
            reason = "opted_out"
        elif not wa.wa_number(u.phone):
            reason = "no_phone"
        elif gm.id in sent_today:
            reason = "duplicate"
        out.append({"member_id": gm.id, "name": gm.display_name, "member_code": gm.member_code, "phone": u.phone,
                    "photo_url": public_url(u.photo_url), "membership_id": ms.id, "plan_name": ms.plan_name,
                    "end_date": ms.end_date.isoformat(), "days_left": left, "can_send": reason is None,
                    "skip_reason": reason, "skip_label": wa.SKIP.get(reason) if reason else None,
                    "last_message": last.get(gm.id), "on_app": u.first_login_at is not None})
    return out


@router.get("/gyms/{gymId}/whatsapp/reminders", summary="Members whose membership is ending / has ended (members.view)",
            description="`view`: expiring (next 7 days) · today · month (next 30 days) · expired (last 30 days). "
                        "Each row says whether a reminder can be sent now and, if not, why.")
def reminders(view: Literal["expiring", "today", "month", "expired"] = "expiring",
              ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    event = "expired" if view == "expired" else "expiry_reminder"
    cfg = wa.get_config(db, ctx.gym.id)
    tpl = db.scalar(select(WaTemplate).where(WaTemplate.gym_id == ctx.gym.id, WaTemplate.key == event))
    items = _eligible(db, ctx.gym, view, event)
    blocked = None if wa.is_connected(cfg) and wa.template_ready(cfg, tpl) else (
        "not_connected" if not wa.is_connected(cfg) else "template_not_approved")
    return {"view": view, "event": event, "items": items, "sendable": sum(1 for i in items if i["can_send"]),
            "blocked": blocked, "blocked_label": wa.SKIP.get(blocked) if blocked else None,
            "template_status": ("ready" if cfg and cfg.provider == "web" else tpl.status) if tpl else "draft",
            "provider": cfg.provider if cfg else None}


class PreviewIn(BaseModel):
    event: str
    member_id: str | None = None
    announcement: str | None = Field(None, max_length=300)


@router.post("/gyms/{gymId}/whatsapp/preview", summary="Preview a message for a member (members.view)")
def preview(body: PreviewIn, ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    t = _tpl(db, ctx.gym.id, body.event)
    v = dict(wa.VARS)
    name = None
    if body.member_id:
        gm = db.get(GymMember, body.member_id)
        if not gm or gm.gym_id != ctx.gym.id:
            raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
        v = wa.build_vars(db, ctx.gym, gm, wa.latest_membership(db, gm.id),
                          {"announcement": body.announcement} if body.announcement else None)
        name = gm.display_name
    elif body.announcement:
        v["announcement"] = body.announcement
    v["gym_name"], v["gym_phone"] = ctx.gym.name, wa.fmt_phone(ctx.gym.phone)
    cfg = wa.get_config(db, ctx.gym.id)
    text = wa.render(t.body, v)
    if cfg and cfg.provider == "web" and t.button_text:  # linked numbers send the link inside the text
        text += f"\n\n{t.button_text}: {settings.renew_url_base}/…"
    return {"event": body.event, "member_name": name, "status": t.status, "provider": cfg.provider if cfg else None,
            "button_text": None if (cfg and cfg.provider == "web") else t.button_text, "text": text,
            "link": f"{settings.renew_url_base}/…" if t.button_text else None}


class SendIn(BaseModel):
    event: Literal["expiry_reminder", "expired", "payment_due", "announcement", "welcome"]
    member_ids: list[str] | None = Field(None, max_length=2000)
    view: Literal["expiring", "today", "month", "expired"] | None = Field(None, description="Send to everyone eligible in this reminders view")
    audience: Literal["active"] | None = Field(None, description="Announcements: every active member")
    announcement: str | None = Field(None, min_length=5, max_length=300)


@router.post("/gyms/{gymId}/whatsapp/send", status_code=202, summary="Queue WhatsApp messages (members.edit)",
             description="Returns straight away with a `batch_id`; messages are sent in the background. Poll "
                         "GET /whatsapp/batches/{id} for progress. A member is skipped (with the reason) if they "
                         "opted out, have no valid number, or already got this message today.")
def send(body: SendIn, ctx: GymCtx = Depends(perm_ctx(SEND)), db: Session = Depends(get_db)):
    gym = ctx.gym
    cfg = _cfg_or_409(db, gym.id)
    if gym.status != "active":
        raise ApiError(423, "GYM_PENDING")
    rate_limit(f"wasend:{gym.id}", 30, 3600)
    tpl = _tpl(db, gym.id, body.event)
    if not wa.template_ready(cfg, tpl):
        raise ApiError(409, "TEMPLATE_NOT_APPROVED", wa.SKIP["template_not_approved"] + ".", {"status": tpl.status})
    if body.event == "announcement":
        if not body.announcement:
            raise validation({"announcement": "Write the announcement."})
        if cfg.consent_confirmed_at is None:
            raise ApiError(409, "CONSENT_REQUIRED", wa.SKIP["no_consent"] + ".")
    targets: list[tuple[GymMember, Membership | None, User | None]] = []
    if body.view:
        for ms, gm, u, _ in wa.expiry_targets(db, gym, window=VIEWS[body.view]):
            targets.append((gm, ms, u))
    elif body.audience == "active":
        if body.event != "announcement":
            raise validation({"audience": "Only announcements can go to every active member."})
        for ms, gm, u, _ in wa.expiry_targets(db, gym, window=(0, 3650)):
            targets.append((gm, ms, u))
    elif body.member_ids:
        members = db.scalars(select(GymMember).where(GymMember.gym_id == gym.id, GymMember.id.in_(body.member_ids))).all()
        if len(members) != len(set(body.member_ids)):
            raise ApiError(404, "NOT_FOUND", "One or more members weren’t found in this gym.")
        for gm in members:
            targets.append((gm, wa.latest_membership(db, gm.id), None))
    else:
        raise validation({"member_ids": "Choose who to send to."})
    if not targets:
        raise ApiError(409, "NOBODY_ELIGIBLE", "Nobody is eligible for this message right now.")
    batch = uuid7()
    day = today_in(gym.timezone).isoformat()
    extra = {"announcement": body.announcement} if body.announcement else None
    stamp = hashlib.sha1((body.announcement or "").encode()).hexdigest()[:10] if body.event == "announcement" else day
    queued, skipped = 0, []
    for gm, ms, u in targets:
        m, why = wa.queue_message(db, gym, gm, body.event, ms=ms, batch_id=batch, extra=extra, actor_id=ctx.user.id,
                                  trigger="manual", cfg=cfg, tpl=tpl, user=u,
                                  dedupe=f"manual:{body.event}:{gm.id}:{stamp}")
        if m:
            queued += 1
        else:
            skipped.append({"member_id": gm.id, "name": gm.display_name, "reason": why, "label": wa.SKIP.get(why, why)})
    audit(db, gym.id, ctx.user.id, "whatsapp.send", "gym", gym.id,
          f"Queued {queued} WhatsApp {wa.EVENTS[body.event]['label'].lower()} message{'s' if queued != 1 else ''}"
          + (f" ({len(skipped)} skipped)" if skipped else ""))
    db.commit()
    if queued:
        enqueue("wa.queue", wa.process_queue)
    return {"batch_id": batch if queued else None, "queued": queued, "skipped": skipped, "total": len(targets)}


@router.get("/gyms/{gymId}/whatsapp/batches/{batchId}", summary="Progress of a bulk send")
def batch(batchId: str, ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    rows = dict(db.execute(select(WaMessage.status, func.count()).where(
        WaMessage.gym_id == ctx.gym.id, WaMessage.batch_id == batchId).group_by(WaMessage.status)).all())
    total = sum(rows.values())
    if not total:
        raise ApiError(404, "NOT_FOUND", "That batch wasn’t found.")
    pending = rows.get("queued", 0) + rows.get("sending", 0)
    failed = [ser_msg(m) for m in db.scalars(select(WaMessage).where(
        WaMessage.gym_id == ctx.gym.id, WaMessage.batch_id == batchId, WaMessage.status == "failed").limit(50))]
    names = {g.id: g.display_name for g in db.scalars(select(GymMember).where(
        GymMember.id.in_([f["member"]["id"] for f in failed] or [""])))}
    for f in failed:
        f["member"]["name"] = names.get(f["member"]["id"])
    return {"batch_id": batchId, "total": total, "pending": pending, "done": pending == 0,
            "accepted": rows.get("sent", 0) + rows.get("delivered", 0) + rows.get("read", 0),
            "delivered": rows.get("delivered", 0) + rows.get("read", 0), "read": rows.get("read", 0),
            "failed": rows.get("failed", 0), "cancelled": rows.get("cancelled", 0), "failures": failed}


# ---------------------------------------------------------------- history & stats
@router.get("/gyms/{gymId}/whatsapp/messages", summary="Message history (members.view)",
            description="`status`: pending (queued/sending) · sent · delivered · read · failed. Delivered/read come "
                        "from WhatsApp receipts, so a message stays at `sent` until the phone actually gets it.")
def messages(status: Literal["pending", "sent", "delivered", "read", "failed", "cancelled"] | None = None,
             event: str | None = None, member_id: str | None = None, q: str | None = Query(None, max_length=60),
             limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
             ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    conds = [WaMessage.gym_id == ctx.gym.id]
    if status == "pending":
        conds.append(WaMessage.status.in_(("queued", "sending")))
    elif status:
        conds.append(WaMessage.status == status)
    if event:
        conds.append(WaMessage.event == event)
    if member_id:
        conds.append(WaMessage.member_id == member_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        mids = select(GymMember.id).where(GymMember.gym_id == ctx.gym.id, GymMember.display_name.ilike(like))
        conds.append(or_(WaMessage.member_id.in_(mids), WaMessage.phone.like(f"%{digits}%") if len(digits) >= 4 else false()))
    stmt = select(WaMessage).where(*conds)
    c = decode_cursor(cursor)
    if c:
        from datetime import datetime
        t = datetime.fromisoformat(c["t"])
        stmt = stmt.where(or_(WaMessage.created_at < t, (WaMessage.created_at == t) & (WaMessage.id < c["id"])))
    lim = clamp_limit(limit)
    rows = db.scalars(stmt.order_by(WaMessage.created_at.desc(), WaMessage.id.desc()).limit(lim + 1)).all()
    nxt = None
    if len(rows) > lim:
        rows = rows[:lim]
        nxt = encode_cursor({"t": rows[-1].created_at.isoformat(), "id": rows[-1].id})
    names = {g.id: g.display_name for g in db.scalars(select(GymMember).where(
        GymMember.id.in_([m.member_id for m in rows if m.member_id] or [""])))}
    plans = {m.id: m.plan_name for m in db.scalars(select(Membership).where(
        Membership.id.in_([m.membership_id for m in rows if m.membership_id] or [""])))}
    counts = dict(db.execute(select(WaMessage.status, func.count()).where(WaMessage.gym_id == ctx.gym.id)
                             .group_by(WaMessage.status)).all())
    return {"items": [ser_msg(m, names.get(m.member_id), plans.get(m.membership_id)) for m in rows], "next_cursor": nxt,
            "counts": {"all": sum(counts.values()), "pending": counts.get("queued", 0) + counts.get("sending", 0),
                       "sent": counts.get("sent", 0), "delivered": counts.get("delivered", 0),
                       "read": counts.get("read", 0), "failed": counts.get("failed", 0)},
            "events": [{"key": k, "label": v["label"]} for k, v in wa.EVENTS.items()]}


@router.post("/gyms/{gymId}/whatsapp/messages/{messageId}/retry", summary="Try a failed message again (members.edit)")
def retry(messageId: str, ctx: GymCtx = Depends(perm_ctx(SEND)), db: Session = Depends(get_db)):
    _cfg_or_409(db, ctx.gym.id)
    m = db.get(WaMessage, messageId)
    if not m or m.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That message wasn’t found.")
    if m.status != "failed":
        raise ApiError(409, "NOT_FAILED", "Only failed messages can be retried.")
    rate_limit(f"waretry:{ctx.gym.id}", 100, 3600)
    m.status, m.attempts, m.next_attempt_at, m.error, m.error_code, m.failed_at, m.wamid = "queued", 0, utcnow(), None, None, None, None
    db.commit()
    enqueue("wa.queue", wa.process_queue)
    return ser_msg(m)


def _stats(db: Session, gym: Gym) -> dict:
    today = today_in(gym.timezone)
    t0 = local_midnight_utc(today, gym.timezone)
    m0 = local_midnight_utc(today.replace(day=1), gym.timezone)

    def counts(since):
        return dict(db.execute(select(WaMessage.status, func.count()).where(
            WaMessage.gym_id == gym.id, WaMessage.created_at >= since).group_by(WaMessage.status)).all())
    d, m = counts(t0), counts(m0)

    def pack(c):
        acc = c.get("sent", 0) + c.get("delivered", 0) + c.get("read", 0)
        return {"sent": acc, "delivered": c.get("delivered", 0) + c.get("read", 0), "read": c.get("read", 0),
                "failed": c.get("failed", 0), "pending": c.get("queued", 0) + c.get("sending", 0)}
    reminders_month = db.scalar(select(func.count()).select_from(WaMessage).where(
        WaMessage.gym_id == gym.id, WaMessage.created_at >= m0, WaMessage.event.in_(("expiry_reminder", "expired")),
        WaMessage.status.in_(("sent", "delivered", "read")))) or 0
    renewed = db.execute(select(func.count(), func.coalesce(func.sum(Membership.price_paise), 0)).select_from(RenewalToken)
                         .join(Membership, Membership.id == RenewalToken.result_membership_id)
                         .where(RenewalToken.gym_id == gym.id, RenewalToken.used_at >= m0)).one()
    opened = db.scalar(select(func.count()).select_from(RenewalToken).where(
        RenewalToken.gym_id == gym.id, RenewalToken.opened_at >= m0)) or 0
    opted = db.scalar(select(func.count()).select_from(GymMember).where(
        GymMember.gym_id == gym.id, GymMember.wa_opted_out_at.is_not(None))) or 0
    return {"today": pack(d), "month": pack(m), "reminders_month": reminders_month, "links_opened_month": opened,
            "renewals_month": int(renewed[0] or 0), "renewal_revenue_paise": int(renewed[1] or 0), "opted_out": opted}


@router.get("/gyms/{gymId}/whatsapp/stats", summary="WhatsApp numbers for the dashboard (members.view)")
def stats(ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    cfg = wa.get_config(db, ctx.gym.id)
    return {"connected": wa.is_connected(cfg), "status": cfg.status if cfg else "disconnected", **_stats(db, ctx.gym)}


class OptIn(BaseModel):
    opted_out: bool


@router.get("/gyms/{gymId}/members/{memberId}/whatsapp",
            summary="One member's WhatsApp: did the welcome go out (and why not), and every message sent to them",
            description="`welcome.state`: sent · sending · not_sent · not_needed, with `code` and a plain `reason`. "
                        "`not_sent_log`: automatic messages that were skipped for this member, with the reason "
                        "(recorded from 20 Nov 2026).")
def member_whatsapp(memberId: str, ctx: GymCtx = Depends(perm_ctx(VIEW)), db: Session = Depends(get_db)):
    from app.models import AuditLog
    gym = ctx.gym
    m = db.get(GymMember, memberId)
    if not m or m.gym_id != gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    u = db.get(User, m.user_id)
    cfg = wa.get_config(db, gym.id)
    rows = db.scalars(select(WaMessage).where(WaMessage.gym_id == gym.id, WaMessage.member_id == m.id)
                      .order_by(WaMessage.created_at.desc()).limit(30)).all()
    plans = {x.id: x.plan_name for x in db.scalars(select(Membership).where(
        Membership.id.in_([r.membership_id for r in rows if r.membership_id] or [""])))}
    skipped = db.scalars(select(AuditLog).where(AuditLog.gym_id == gym.id, AuditLog.action == "whatsapp.not_sent",
                                                AuditLog.entity_id == m.id).order_by(AuditLog.id.desc()).limit(20)).all()
    welcome = wa.welcome_status(db, gym, m, u, cfg)
    welcome["can_send_now"] = (welcome["state"] == "not_sent" and wa.is_connected(cfg) and gym.status == "active"
                               and welcome["code"] not in ("no_plan", "opted_out", "no_phone", "archived"))
    return {"connection": {"status": cfg.status if cfg else "not_set_up", "connected": wa.is_connected(cfg),
                           "provider": cfg.provider if cfg else None,
                           "reconnecting": bool(cfg and cfg.status == "connected" and getattr(cfg, "link_lost_at", None))},
            "welcome": welcome,
            "messages": [ser_msg(r, m.display_name, plans.get(r.membership_id)) for r in rows],
            "not_sent_log": [{"at": iso(a.at), "event": (a.after or {}).get("event"), "reason": (a.after or {}).get("reason"),
                              "summary": a.summary} for a in skipped]}


@router.post("/gyms/{gymId}/members/{memberId}/whatsapp-opt", summary="Switch WhatsApp messages off/on for a member (members.edit)")
def member_opt(memberId: str, body: OptIn, ctx: GymCtx = Depends(perm_ctx(SEND)), db: Session = Depends(get_db)):
    gm = db.get(GymMember, memberId)
    if not gm or gm.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    if body.opted_out:
        gm.wa_opted_out_at, gm.wa_opt_source = gm.wa_opted_out_at or utcnow(), "staff"
        db.execute(update(WaMessage).where(WaMessage.gym_id == ctx.gym.id, WaMessage.member_id == gm.id,
                                           WaMessage.status == "queued")
                   .values(status="cancelled", error="Member opted out", error_code="opted_out"))
    else:
        gm.wa_opted_out_at, gm.wa_opt_source = None, None
    audit(db, ctx.gym.id, ctx.user.id, "whatsapp.opt", "member", gm.id,
          f"WhatsApp messages {'stopped' if body.opted_out else 'resumed'} for {gm.display_name}")
    db.commit()
    return {"member_id": gm.id, "opted_out": gm.wa_opted_out_at is not None, "opted_out_at": iso(gm.wa_opted_out_at)}


# ---------------------------------------------------------------- Meta webhook
@public.get("/webhooks/whatsapp", include_in_schema=False)
def webhook_verify(request: Request):
    p = request.query_params
    expected = settings.meta_webhook_verify_token
    if p.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(p.get("hub.verify_token") or "", expected):
        return Response(p.get("hub.challenge") or "", media_type="text/plain")
    raise ApiError(403, "FORBIDDEN", "Verification failed.")


@public.post("/webhooks/whatsapp", include_in_schema=False)
async def webhook(request: Request):
    raw = await request.body()
    secret = settings.meta_app_secret
    if not secret:
        raise ApiError(503, "WEBHOOK_NOT_CONFIGURED", "META_APP_SECRET isn’t set.")
    sig = request.headers.get("x-hub-signature-256") or ""
    good = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        raise ApiError(403, "BAD_SIGNATURE", "Signature check failed.")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", "Invalid JSON.")
    if payload.get("object") != "whatsapp_business_account":
        return {"ok": True}
    try:
        res = await run_in_threadpool(wa.handle_webhook, payload)
        log.info("whatsapp webhook: %s", res)
    except Exception as e:  # ask Meta to retry
        log.error("whatsapp webhook failed: %s", type(e).__name__)
        raise ApiError(500, "WEBHOOK_FAILED", "Temporary error.")
    return {"ok": True}


# ---------------------------------------------------------------- personal renewal link
def _token(db: Session, raw: str, request: Request) -> tuple[RenewalToken, GymMember, Gym]:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    rate_limit(f"renewip:{ip}", 60, 600)
    if not raw or len(raw) < 20 or len(raw) > 80:
        raise ApiError(404, "LINK_INVALID", "This renewal link isn’t valid.")
    t = db.scalar(select(RenewalToken).where(RenewalToken.token_hash == wa.hash_token(raw)))
    if not t or t.revoked_at is not None:
        raise ApiError(404, "LINK_INVALID", "This renewal link isn’t valid. Ask your gym for a new one.")
    if t.expires_at < utcnow():
        raise ApiError(410, "LINK_EXPIRED", "This renewal link has expired. Ask your gym to send a new one.")
    gm, g = db.get(GymMember, t.member_id), db.get(Gym, t.gym_id)
    if not gm or not g or gm.gym_id != g.id or gm.status != "active" or g.status != "active":
        raise ApiError(404, "LINK_INVALID", "This renewal link isn’t valid any more.")
    return t, gm, g


def _open_request(db: Session, t: RenewalToken) -> UpiPaymentRequest | None:
    return db.scalar(select(UpiPaymentRequest).where(UpiPaymentRequest.renewal_token_id == t.id)
                     .order_by(UpiPaymentRequest.created_at.desc()).limit(1))


def _renew_view(db: Session, t: RenewalToken, gm: GymMember, g: Gym) -> dict:
    from app.routers.upi import ser_req
    u = db.get(User, gm.user_id)
    cur = wa.latest_membership(db, gm.id)
    credit = max(0, credit_balance(db, gm.id))
    start = default_start(db, g, gm.id)
    plans = []
    for p in db.scalars(select(Plan).where(Plan.gym_id == g.id, Plan.active.is_(True)).order_by(Plan.sort_order, Plan.duration_days)):
        pr = price_preview(db, p, gm)
        plans.append({"id": p.id, "name": p.name, "duration_days": p.duration_days, "description": p.description,
                      "facilities": p.facilities or [], "list_price_paise": p.price_paise,
                      "price_paise": pr["total_paise"], "to_pay_paise": max(0, pr["total_paise"] - credit),
                      "price": pr, "start_date": start.isoformat(),
                      "end_date": (start + timedelta(days=p.duration_days - 1)).isoformat()})
    r = _open_request(db, t)
    digits = "".join(ch for ch in (u.phone or "") if ch.isdigit())
    done = None
    if t.used_at and t.result_membership_id:
        ms = db.get(Membership, t.result_membership_id)
        done = {"plan_name": ms.plan_name, "start_date": ms.start_date.isoformat(), "end_date": ms.end_date.isoformat()} if ms else None
    return {"gym": {"name": g.name, "area": g.area, "city": g.city, "phone": g.phone, "logo_url": public_url(g.logo_url)},
            "member": {"first_name": (gm.display_name or "").split(" ")[0], "member_code": gm.member_code,
                       "phone_hint": ("•••• ••" + digits[-4:]) if len(digits) >= 4 else None},
            "current": {"plan_name": cur.plan_name, "end_date": cur.end_date.isoformat(),
                        "days_left": (cur.end_date - today_in(g.timezone)).days} if cur else None,
            "plans": plans, "credit_paise": credit, "upi_enabled": wa.has_upi(db, g.id),
            "request": ser_req(r, None) if r and r.status in ("pending", "claimed", "expired", "rejected", "verified") else None,
            "renewed": done, "expires_at": iso(t.expires_at)}


@public.get("/renew/{token}", summary="Personal renewal page: gym, current membership and plans with this member’s prices")
def renew_page(token: str, request: Request, db: Session = Depends(get_db)):
    t, gm, g = _token(db, token, request)
    if t.opened_at is None:
        t.opened_at = utcnow()
    _sync_result(db, t)
    db.commit()
    return _renew_view(db, t, gm, g)


def _sync_result(db: Session, t: RenewalToken):
    """If the gym verified a payment started from this link, mark the link as used (attribution)."""
    if t.used_at:
        return
    r = db.scalar(select(UpiPaymentRequest).where(UpiPaymentRequest.renewal_token_id == t.id,
                                                  UpiPaymentRequest.status == "verified",
                                                  UpiPaymentRequest.result_membership_id.is_not(None)).limit(1))
    if r:
        t.used_at, t.result_membership_id = r.decided_at or utcnow(), r.result_membership_id


class RenewStartIn(BaseModel):
    plan_id: str


@public.post("/renew/{token}/start", status_code=201, summary="Choose a plan → get the UPI payment (or renew on credit)",
             description="Nothing is renewed on the member’s say-so: the membership starts only when the gym verifies "
                         "the payment (or straight away if the member’s credit already covers it).")
def renew_start(token: str, body: RenewStartIn, request: Request, db: Session = Depends(get_db)):
    from app.routers.upi import _active_plan, default_account, new_request, ser_req
    t, gm, g = _token(db, token, request)
    rate_limit(f"renewstart:{t.id}", 12, 3600)
    _sync_result(db, t)
    if t.used_at:
        raise ApiError(409, "ALREADY_RENEWED", "This link was already used to renew.")
    plan = _active_plan(db, g.id, body.plan_id)
    total = price_preview(db, plan, gm)["total_paise"]
    credit = max(0, credit_balance(db, gm.id))
    if total - credit <= 0:
        ms = create_membership(db, g, gm.user_id, gm, plan=plan, start=default_start(db, g, gm.id))
        db.flush()
        settle_credit(db, g, gm, gm.user_id, ms.id)
        t.used_at, t.result_membership_id = utcnow(), ms.id
        audit(db, g.id, gm.user_id, "membership.renew", "membership", ms.id,
              f"{gm.display_name} renewed on {plan.name} from their renewal link using credit")
        db.commit()
        wa.fire_later(g.id, "renewal_confirmed", gm.id, ms.id, dedupe=f"renewed:{ms.id}")
        return {"mode": "credit", **_renew_view(db, t, gm, g)}
    prev = _open_request(db, t)
    if prev and prev.status == "claimed":
        raise ApiError(409, "PAYMENT_IN_REVIEW", "You’ve already submitted a payment. The gym is checking it.")
    if prev and prev.status == "pending" and prev.plan_id != plan.id:
        prev.status, prev.decided_at = "cancelled", utcnow()
    acct = default_account(db, g.id)
    r = new_request(db, g, gm, None, total - credit, acct, "member", gm.user_id, None, 1440, plan)
    r.renewal_token_id = t.id
    audit(db, g.id, gm.user_id, "upi.request", "upi_request", r.id,
          f"{gm.display_name} started a renewal to {plan.name} ({rupees(r.amount_paise)}) from their renewal link")
    db.commit()
    return {"mode": "upi", "request": ser_req(r, None, plan_name=plan.name)}


class RenewClaimIn(BaseModel):
    utr: str


@public.post("/renew/{token}/claim", summary="I’ve paid — submit the UTR for the gym to verify")
def renew_claim(token: str, body: RenewClaimIn, request: Request, db: Session = Depends(get_db)):
    from app.routers.upi import _utr_taken, clean_utr, payment_staff, ser_req
    t, gm, g = _token(db, token, request)
    rate_limit(f"renewclaim:{t.id}", 8, 3600)
    r = _open_request(db, t)
    if not r or r.status not in ("pending", "expired", "claimed"):
        raise ApiError(409, "NOT_ACTIVE", "There’s no open payment for this link. Choose a plan first.")
    utr = clean_utr(body.utr)
    _utr_taken(db, g.id, utr, exclude_req=r.id)
    r.status, r.utr, r.claimed_at = "claimed", utr, utcnow()
    audit(db, g.id, gm.user_id, "upi.claim", "upi_request", r.id,
          f"{gm.display_name} says they paid {rupees(r.amount_paise)} by UPI (UTR {utr}) from their renewal link")
    db.commit()
    for uid in payment_staff(db, g.id):
        enqueue("push.send", send_push, uid, "admin", "UPI payment to verify",
                f"{gm.display_name} paid {rupees(r.amount_paise)} (UTR {utr}). Check your bank and verify.",
                {"type": "upi_claimed", "gym_id": g.id, "request_id": r.id})
    return {"request": ser_req(r, None)}


@public.post("/renew/{token}/stop", summary="Stop WhatsApp messages from this gym")
def renew_stop(token: str, request: Request, db: Session = Depends(get_db)):
    t, gm, g = _token(db, token, request)
    gm.wa_opted_out_at, gm.wa_opt_source = gm.wa_opted_out_at or utcnow(), "member"
    db.execute(update(WaMessage).where(WaMessage.gym_id == g.id, WaMessage.member_id == gm.id, WaMessage.status == "queued")
               .values(status="cancelled", error="Member opted out", error_code="opted_out"))
    db.commit()
    return {"opted_out": True}
