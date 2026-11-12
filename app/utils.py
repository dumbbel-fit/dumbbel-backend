"""Small shared helpers: ids, time, phone, money, codes, pagination."""
import base64
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import phonenumbers

from app.errors import ApiError

DEFAULT_TZ = "Asia/Kolkata"


# ---------- ids ----------
def uuid7() -> str:
    """RFC 9562 UUID v7 (time ordered)."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


# ---------- time ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def today_in(tzname: str | None) -> date:
    return utcnow().astimezone(tz(tzname)).date()


def local_date_of(dt: datetime, tzname: str | None) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz(tzname)).date()


def local_midnight_utc(d: date, tzname: str | None) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz(tzname)).astimezone(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    dt = aware(dt)
    return dt.isoformat() if dt else None


def dstr(d: date | None) -> str | None:
    return d.isoformat() if d else None


def month_bounds(month: str | None, tzname: str | None) -> tuple[date, date]:
    """'2026-11' -> (2026-11-01, 2026-11-30). Defaults to current month in gym tz."""
    if month:
        m = re.fullmatch(r"(\d{4})-(\d{2})", month)
        if not m:
            raise ApiError(422, "VALIDATION", "Month must look like 2026-11.", {"fields": {"month": "Use YYYY-MM"}})
        y, mo = int(m.group(1)), int(m.group(2))
    else:
        t = today_in(tzname)
        y, mo = t.year, t.month
    start = date(y, mo, 1)
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


# ---------- phone ----------
def normalize_phone(value, field: str = "phone") -> str:
    """Accepts {'country':'IN','number':'98...'} or an E.164 string. Returns E.164."""
    country, number = "IN", None
    if isinstance(value, dict):
        country = (value.get("country") or "IN").upper()
        number = str(value.get("number") or "")
    elif hasattr(value, "number"):
        country = (getattr(value, "country", None) or "IN").upper()
        number = str(value.number or "")
    elif isinstance(value, str):
        number = value
    if not number:
        raise ApiError(400, "PHONE_INVALID", "That phone number doesn’t look right.", {"field": field})
    number = re.sub(r"[\s\-()]", "", number)
    try:
        p = phonenumbers.parse(number, None if number.startswith("+") else country)
    except phonenumbers.NumberParseException:
        raise ApiError(400, "PHONE_INVALID", "That phone number doesn’t look right.", {"field": field})
    if not phonenumbers.is_valid_number(p):
        raise ApiError(400, "PHONE_INVALID", "That phone number doesn’t look right.", {"field": field})
    if p.country_code == 91 and str(p.national_number)[0] not in "6789":
        raise ApiError(400, "PHONE_INVALID", "Indian mobile numbers start with 6, 7, 8 or 9.", {"field": field})
    return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)


def mask_phone(e164: str | None) -> str:
    if not e164:
        return ""
    if e164.startswith("+91") and len(e164) == 13:
        return f"+91 {e164[3:5]}•••••{e164[-3:]}"
    return e164[:3] + "•" * max(0, len(e164) - 6) + e164[-3:]


PHONE_RE = re.compile(r"\+?\d{10,13}")


def mask_phones_in_text(text: str) -> str:
    return PHONE_RE.sub(lambda m: mask_phone(m.group(0) if m.group(0).startswith("+") else "+91" + m.group(0)[-10:]), text)


# ---------- money ----------
def rupees(paise: int) -> str:
    r, p = divmod(int(paise), 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{s}" + (f".{p:02d}" if p else "")


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _three(n: int) -> str:
    h, r = divmod(n, 100)
    out = []
    if h:
        out.append(_ONES[h] + " Hundred")
    if r:
        out.append(_two(r))
    return " ".join(out)


def amount_in_words(paise: int) -> str:
    r, p = divmod(int(paise), 100)
    if r == 0 and p == 0:
        return "Zero Rupees Only"
    parts = []
    crore, r = divmod(r, 10_000_000)
    lakh, r = divmod(r, 100_000)
    thousand, r = divmod(r, 1000)
    if crore:
        parts.append(_three(crore) + " Crore")
    if lakh:
        parts.append(_two(lakh) + " Lakh")
    if thousand:
        parts.append(_two(thousand) + " Thousand")
    if r:
        parts.append(_three(r))
    words = " ".join(parts) + " Rupees" if parts else ""
    if p:
        words = (words + " and " if words else "") + _two(p) + " Paise"
    return words + " Only"


# ---------- pagination ----------
def encode_cursor(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict:
    if not cursor:
        return {}
    try:
        pad = "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception:
        raise ApiError(400, "BAD_REQUEST", "That page link has expired. Pull to refresh.", {"field": "cursor"})


def clamp_limit(limit: int | None, default: int = 50) -> int:
    if not limit:
        return default
    return max(1, min(100, int(limit)))


def paginate_list(items: list, limit: int | None, cursor: str | None) -> tuple[list, str | None]:
    """Offset cursor over an in-memory, already-sorted list."""
    lim = clamp_limit(limit)
    off = int(decode_cursor(cursor).get("o", 0))
    page = items[off: off + lim]
    nxt = encode_cursor({"o": off + lim}) if off + lim < len(items) else None
    return page, nxt


def first_name(name: str | None) -> str | None:
    return name.split()[0] if name else None


def version_tuple(v: str | None) -> tuple:
    try:
        return tuple(int(x) for x in (v or "0").split(".")[:3])
    except ValueError:
        return (0,)
