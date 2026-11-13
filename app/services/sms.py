"""SMS via 2Factor.in (https://documenter.getpostman.com/view/301893/TWDamFGh).

OTP:   v1 -> GET https://2factor.in/API/V1/{key}/SMS/{10-digit phone}/{otp}/{template}  (falls back to a voice call
             when the DLT template isn't mapped)   |  r1 -> transactional SMS only (never a call)
Other: Transactional DLT SMS  POST https://2factor.in/API/R1/  module=TRANS_SMS  (optional gym-status SMS).
       These need a DLT-approved template per message; without one, operators silently drop the SMS,
       so we refuse to send (SmsNotConfigured) instead of pretending it worked.
Every send is logged with the provider response and the request id.
"""
import logging

import httpx

from app.config import settings
from app.context import current_request_id
from app.errors import ApiError
from app.utils import mask_phone

log = logging.getLogger("dumbbel.sms")
BASE = "https://2factor.in/API"


class SmsNotConfigured(Exception):
    pass


class SmsFailed(Exception):
    pass


def _digits(e164: str) -> str:
    """2Factor expects a 10-digit number for India (docs: 'Length Mismatch (Expected: 10)')."""
    if e164.startswith("+91") and len(e164) == 13:
        return e164[3:]
    return e164.lstrip("+")


def _log(kind: str, phone: str, ok: bool, provider: str | None, extra: str = ""):
    (log.info if ok else log.error)("sms kind=%s to=%s ok=%s provider=%r rid=%s %s", kind, mask_phone(phone), ok,
                                    provider, current_request_id(), extra)


def send_otp(phone_e164: str, code: str) -> str | None:
    """Returns 2Factor session id. Raises UPSTREAM_SMS on failure."""
    if not settings.twofactor_api_key:
        _log("otp", phone_e164, False, "TWOFACTOR_API_KEY missing")
        raise ApiError(503, "UPSTREAM_SMS", "SMS isn’t configured on the server yet (TWOFACTOR_API_KEY missing).")
    try:
        if settings.twofactor_otp_route.lower() == "r1":
            r = httpx.post(f"{BASE}/R1/", timeout=10, data={
                "module": "TRANS_SMS", "apikey": settings.twofactor_api_key, "to": _digits(phone_e164),
                "from": settings.twofactor_sender_id, "templatename": settings.twofactor_otp_template, "var1": code})
        else:
            url = f"{BASE}/V1/{settings.twofactor_api_key}/SMS/{_digits(phone_e164)}/{code}/{settings.twofactor_otp_template}"
            r = httpx.get(url, timeout=10)
        data = r.json()
    except Exception as e:
        _log("otp", phone_e164, False, f"{type(e).__name__}: {e}")
        raise ApiError(502, "UPSTREAM_SMS")
    ok = r.status_code == 200 and data.get("Status") == "Success"
    _log("otp", phone_e164, ok, data.get("Details"), f"route={settings.twofactor_otp_route}")
    if not ok:
        raise ApiError(502, "UPSTREAM_SMS", None, {"provider": data.get("Details")} if not settings.is_prod else None)
    return data.get("Details")


TEMPLATES = {
    "gym_approved": "Good news! {gym} is approved on DUMBBEL.fit. {reason}Help: {support}",
    "gym_rejected": "{gym} couldn’t be approved on DUMBBEL.fit. Reason: {reason}. Help: {support}",
    "gym_suspended": "{gym} has been suspended on DUMBBEL.fit. Reason: {reason}. Help: {support}",
    "gym_reinstated": "{gym} is active again on DUMBBEL.fit. Help: {support}",
}


def render(kind: str, vars: dict) -> str:
    return TEMPLATES[kind].format(**{k: (v if v is not None else "") for k, v in vars.items()})


def _dlt_template(kind: str) -> str | None:
    if kind.startswith("gym_"):
        return settings.twofactor_tpl_gym_status
    return None


def is_configured(kind: str) -> bool:
    return bool(settings.twofactor_api_key and _dlt_template(kind))


def send_template(phone_e164: str, kind: str, vars: dict) -> str:
    """Send a DLT transactional SMS. Returns provider message id.
    Raises SmsNotConfigured (no key/template) or SmsFailed (provider said no)."""
    tpl = _dlt_template(kind)
    if not settings.twofactor_api_key or not tpl:
        env_name = "TWOFACTOR_TPL_GYM_STATUS" if kind.startswith("gym_") else f"TWOFACTOR_TPL_{kind.upper()}"
        missing = "TWOFACTOR_API_KEY" if not settings.twofactor_api_key else env_name
        _log(kind, phone_e164, False, f"not configured ({missing})")
        raise SmsNotConfigured(f"SMS template for {kind} isn’t set up (DLT). Missing {missing}.")
    data = {"module": "TRANS_SMS", "apikey": settings.twofactor_api_key, "to": _digits(phone_e164),
            "from": settings.twofactor_sender_id, "templatename": tpl}
    for i, v in enumerate(vars.values(), start=1):
        data[f"var{i}"] = str(v or "")
    try:
        r = httpx.post(f"{BASE}/R1/", data=data, timeout=10)
        body = r.json()
    except Exception as e:
        _log(kind, phone_e164, False, f"{type(e).__name__}: {e}")
        raise SmsFailed(f"SMS provider unreachable: {type(e).__name__}")
    ok = body.get("Status") == "Success"
    _log(kind, phone_e164, ok, body.get("Details"), f"template={tpl}")
    if not ok:
        raise SmsFailed(str(body.get("Details") or "SMS provider rejected the message"))
    return str(body.get("Details"))
