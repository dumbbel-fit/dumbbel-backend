"""SMS via 2Factor.in (https://documenter.getpostman.com/view/301893/TWDamFGh).

OTP:   GET https://2factor.in/API/V1/{api_key}/SMS/{phone}/{otp}/{template_name}
       -> template "Dumbble" (sender DMBL): "XXXX is your OTP template for Dumbble login"
       We generate + hash the code ourselves (spec §3 otp_codes) and pass it as {otp}.
Other: Transactional DLT SMS  POST https://2factor.in/API/R1/  module=TRANS_SMS
       (staff_invite, member_invite, gym status) once those templates are DLT-approved.
Without an API key (dev), messages are logged instead of sent.
"""
import logging

import httpx

from app.config import settings
from app.errors import ApiError
from app.utils import mask_phone

log = logging.getLogger("dumbbel.sms")
BASE = "https://2factor.in/API"


def _digits(e164: str) -> str:
    """2Factor expects a 10-digit number for India (docs: 'Length Mismatch (Expected: 10)')."""
    if e164.startswith("+91") and len(e164) == 13:
        return e164[3:]
    return e164.lstrip("+")


def send_otp(phone_e164: str, code: str) -> str | None:
    """Returns 2Factor session id. Raises UPSTREAM_SMS on failure."""
    if not settings.twofactor_api_key:
        log.error("TWOFACTOR_API_KEY is not set in .env — cannot send OTP SMS")
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
    except Exception as e:  # network / JSON
        log.error("2factor OTP send failed for %s: %s", mask_phone(phone_e164), e)
        raise ApiError(502, "UPSTREAM_SMS")
    if r.status_code != 200 or data.get("Status") != "Success":
        log.error("2factor OTP error for %s: %s", mask_phone(phone_e164), data.get("Details"))
        raise ApiError(502, "UPSTREAM_SMS", None, {"provider": data.get("Details")} if not settings.is_prod else None)
    log.info("2factor OTP accepted for %s template=%s session=%s", mask_phone(phone_e164),
             settings.twofactor_otp_template, data.get("Details"))
    return data.get("Details")


TEMPLATES = {
    "staff_invite": "{owner} added you to {gym} on DUMBBEL.fit Admin. Download: {link} and log in with this number.",
    "member_invite": "{gym} is now on DUMBBEL.fit. Get your membership QR: {link}. Log in with this number.",
    "gym_approved": "Good news! {gym} is approved on DUMBBEL.fit. {reason}Help: {support}",
    "gym_rejected": "{gym} couldn’t be approved on DUMBBEL.fit. Reason: {reason}. Help: {support}",
    "gym_suspended": "{gym} has been suspended on DUMBBEL.fit. Reason: {reason}. Help: {support}",
    "gym_reinstated": "{gym} is active again on DUMBBEL.fit. Help: {support}",
}


def _dlt_template(kind: str) -> str | None:
    if kind == "staff_invite":
        return settings.twofactor_tpl_staff_invite
    if kind == "member_invite":
        return settings.twofactor_tpl_member_invite
    if kind.startswith("gym_"):
        return settings.twofactor_tpl_gym_status
    return None


def send_template(phone_e164: str, kind: str, vars: dict) -> str | None:
    """Send a transactional template SMS. Returns provider message id; raises on failure (job retries)."""
    text = TEMPLATES[kind].format(**{k: (v if v is not None else "") for k, v in vars.items()})
    tpl = _dlt_template(kind)
    if not settings.twofactor_api_key or not tpl:
        log.warning("[sms:%s] to %s (not sent - template/API key not configured): %s", kind, mask_phone(phone_e164), text)
        return "logged"
    data = {"module": "TRANS_SMS", "apikey": settings.twofactor_api_key, "to": _digits(phone_e164),
            "from": settings.twofactor_sender_id, "templatename": tpl}
    for i, v in enumerate(vars.values(), start=1):
        data[f"var{i}"] = str(v or "")
    r = httpx.post(f"{BASE}/R1/", data=data, timeout=10)
    body = r.json()
    if body.get("Status") != "Success":
        raise RuntimeError(f"2factor: {body.get('Details')}")
    return body.get("Details")
