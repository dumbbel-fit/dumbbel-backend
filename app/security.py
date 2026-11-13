"""JWT access tokens, opaque refresh tokens, OTP hashing and QR tokens (spec §5, §15)."""
import base64
import hashlib
import hmac
import json
import secrets
import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import settings

_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


# ---------- OTP ----------
def gen_otp() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(settings.otp_length))


def hash_otp(code: str) -> str:
    return _ph.hash(code)


def verify_otp(code_hash: str, code: str) -> bool:
    try:
        return _ph.verify(code_hash, code)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# ---------- access / refresh ----------
def make_access_token(user_id: str, app: str, session_id: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "app": app, "sid": session_id, "iat": now,
                       "exp": now + settings.access_token_ttl_sec, "typ": "access"},
                      settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub", "sid"]})


def make_refresh_token(session_id: str) -> str:
    """Opaque '<session_id>.<random>' so a reused (rotated-out) token can be traced to its session."""
    return f"{session_id}.{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- QR (DBF1.<b64 payload>.<b64 hmac>) ----------
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _qr_keys() -> dict[str, str]:
    out = {}
    for part in settings.qr_keys.split(","):
        if ":" in part:
            kid, sec = part.split(":", 1)
            out[kid.strip()] = sec.strip()
    return out


QR_TTL = 30
QR_SKEW = 10


def make_qr_token(member_id: str, gym_id: str) -> tuple[str, int]:
    kid = settings.qr_current_kid
    secret = _qr_keys()[kid]
    now = int(time.time())
    payload = _b64e(json.dumps({"m": member_id, "g": gym_id, "iat": now, "exp": now + QR_TTL,
                                "j": secrets.token_hex(12)}, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), f"{kid}.{payload}".encode(), hashlib.sha256).digest())
    return f"{kid}.{payload}.{sig}", now + QR_TTL


def parse_qr_token(token: str) -> dict | None:
    """Returns payload if signature, version and expiry are valid, else None."""
    try:
        kid, payload, sig = token.strip().split(".")
    except ValueError:
        return None
    keys = _qr_keys()
    if kid not in keys:
        return None
    if kid != settings.qr_current_kid:
        # previous key is only honoured during the 5-minute rotation window
        until = settings.qr_previous_kid_valid_until
        if not until or time.time() > until:
            return None
    expect = _b64e(hmac.new(keys[kid].encode(), f"{kid}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        data = json.loads(_b64d(payload))
        now = time.time()
        if data["exp"] - data["iat"] > QR_TTL:
            return None
        if now > data["exp"] + QR_SKEW or now < data["iat"] - QR_SKEW:
            return None
        if not all(k in data for k in ("m", "g", "j")):
            return None
        return data
    except Exception:
        return None


# ---------- Gym check-in QR (member scans the gym) ----------
# Live screen QR: "DBG2.<b64 payload {g, iat, exp, j}>.<b64 hmac>", valid GYM_QR_TTL s, refreshed every 20 s.
# Printed poster QR: static signature over (gym id, gym.checkin_secret); rotate the secret to revoke old posters.
GYM_QR_TTL = 60


def _kid_secret(kid: str | None = None) -> tuple[str, str]:
    keys = _qr_keys()
    kid = kid or settings.qr_current_kid
    return kid, keys[kid]


def gym_static_sig(gym_id: str, checkin_secret: str) -> str:
    _, secret = _kid_secret()
    return _b64e(hmac.new(secret.encode(), f"DBGS:{gym_id}:{checkin_secret}".encode(), hashlib.sha256).digest()[:18])


def make_gym_display_token(gym_id: str) -> tuple[str, int]:
    _, secret = _kid_secret()
    now = int(time.time())
    payload = _b64e(json.dumps({"g": gym_id, "iat": now, "exp": now + GYM_QR_TTL, "j": secrets.token_hex(6)},
                               separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), f"DBG2.{payload}".encode(), hashlib.sha256).digest()[:18])
    return f"DBG2.{payload}.{sig}", now + GYM_QR_TTL


def parse_gym_display_token(token: str) -> dict | None:
    try:
        ver, payload, sig = token.strip().split(".")
    except ValueError:
        return None
    if ver != "DBG2":
        return None
    for kid, secret in _qr_keys().items():
        if kid != settings.qr_current_kid and not (settings.qr_previous_kid_valid_until
                                                   and time.time() <= settings.qr_previous_kid_valid_until):
            continue
        expect = _b64e(hmac.new(secret.encode(), f"DBG2.{payload}".encode(), hashlib.sha256).digest()[:18])
        if hmac.compare_digest(expect, sig):
            try:
                data = json.loads(_b64d(payload))
            except Exception:
                return None
            now = time.time()
            if now > data["exp"] + QR_SKEW or now < data["iat"] - QR_SKEW:
                return None
            return data
    return None
