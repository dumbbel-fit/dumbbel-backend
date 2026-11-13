"""Member import (spec §16): parse CSV/XLS/XLSX, auto-map columns, normalise, classify rows, commit in batches."""
import csv
import difflib
import io
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.errors import ApiError
from app.models import Gym, GymMember, Import, ImportRow, Membership, Plan, User
from app.services.audit import audit
from app.services.core import settle_credit, add_payment, create_membership, get_or_create_user, next_member_code, overlapping
from app.utils import local_midnight_utc, today_in, utcnow

log = logging.getLogger("dumbbel.import")

MAX_ROWS = 5000
MAX_BYTES = 10 * 1024 * 1024
BATCH = 200

FIELDS = ["name", "phone", "plan", "start_date", "end_date", "paid", "paid_on", "email", "dob", "gender", "notes"]
SYNONYMS = {
    "name": ["name", "member name", "full name", "customer", "client", "customer name", "client name"],
    "phone": ["mobile", "phone", "contact", "mobile no", "whatsapp", "number", "mobile number", "phone number",
              "contact no", "contact number", "phone no", "whatsapp no", "mob", "mob no", "cell"],
    "plan": ["plan", "package", "membership", "scheme", "plan name", "package name", "membership type"],
    "start_date": ["start", "join", "joining date", "doj", "from", "start date", "joined", "join date", "date of joining"],
    "end_date": ["end", "expiry", "valid till", "to", "due date", "end date", "expiry date", "expires", "valid upto",
                 "valid until"],
    "paid": ["paid", "amount", "amount paid", "fees paid", "fee paid", "fees", "paid amount"],
    "paid_on": ["paid on", "payment date", "paid date", "date of payment", "payment on", "receipt date"],
    "email": ["email", "mail", "mail id", "email id", "e-mail"],
    "dob": ["dob", "birthday", "date of birth", "birth date"],
    "gender": ["gender", "sex"],
    "notes": ["notes", "remarks", "note", "comments", "remark"],
}
BLOCKING = {"NAME_MISSING", "PHONE_MISSING", "PHONE_DIGITS", "PHONE_START", "PLAN_UNKNOWN", "DATE_INVALID",
            "END_BEFORE_START", "PAID_INVALID", "DATES_INCOMPLETE"}
MSG = {
    "NAME_MISSING": "Name is missing", "PHONE_MISSING": "Mobile number is missing",
    "PHONE_START": "Mobile should start with 6, 7, 8 or 9", "PLAN_UNKNOWN": "Plan “{v}” doesn’t match any plan",
    "DATE_INVALID": "“{v}” isn’t a date we understand", "END_BEFORE_START": "End date is before start date",
    "PAID_INVALID": "Paid amount “{v}” isn’t a number", "DATES_INCOMPLETE": "Need a plan or both start and end dates",
    "DUPLICATE_IN_FILE": "Same mobile appears earlier in the file (row {v})",
    "OVERLAPPING": "Overlaps this member’s existing membership; only details will be kept",
    "EMAIL_INVALID": "Email “{v}” looks wrong and will be skipped",
}


def issue(field, code, v=None, digits=None):
    msg = f"Mobile has {digits} digits" if code == "PHONE_DIGITS" else MSG.get(code, code).format(v=v)
    return {"field": field, "code": code, "message": msg, "blocking": code in BLOCKING}


# ---------------- file parsing ----------------
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v).strip()


def read_table(data: bytes, filename: str) -> list[list[str]]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
    if ext == "xls":
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sh = book.sheet_by_index(0)
        out = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(_cell(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)))
                else:
                    row.append(_cell(cell.value))
            out.append(row)
        return out
    raise ApiError(415, "FILE_TYPE")


def parse_file(data: bytes, filename: str) -> tuple[list[str], list[tuple[int, dict]]]:
    if len(data) > MAX_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE")
    table = read_table(data, filename)
    hi = next((i for i, r in enumerate(table) if any(c for c in r)), None)
    if hi is None:
        raise ApiError(422, "NO_HEADER_ROW")
    raw_headers = table[hi]
    if sum(1 for h in raw_headers if h) < 1 or all(re.fullmatch(r"[\d\s+\-().]+", h or "0") for h in raw_headers if h):
        raise ApiError(422, "NO_HEADER_ROW")
    headers, seen = [], {}
    for i, h in enumerate(raw_headers):
        h = (h or f"Column {i + 1}").strip()
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        headers.append(h)
    rows = []
    for i, r in enumerate(table[hi + 1:], start=hi + 2):
        if not any(c for c in r):
            continue
        rows.append((i, {headers[j]: (r[j] if j < len(r) else "") for j in range(len(headers))}))
    if len(rows) > MAX_ROWS:
        raise ApiError(413, "FILE_TOO_LARGE", f"That file has {len(rows)} rows. Max 5,000.")
    return headers, rows


# ---------------- normalisation ----------------
def norm_import_phone(v: str) -> tuple[str | None, str | None, int | None]:
    """-> (e164, error_code, digit_count)"""
    s = re.sub(r"[\s\-().]", "", str(v or ""))
    if s.endswith(".0"):
        s = s[:-2]
    if not s:
        return None, "PHONE_MISSING", 0
    if s.startswith("+91"):
        s = s[3:]
    elif s.startswith("91") and len(s) == 12:
        s = s[2:]
    elif s.startswith("0") and len(s) == 11:
        s = s[1:]
    digits = re.sub(r"\D", "", s)
    if len(digits) != 10 or digits != s:
        return None, "PHONE_DIGITS", len(digits)
    if digits[0] not in "6789":
        return None, "PHONE_START", 10
    return "+91" + digits, None, 10


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(v: str, order: str = "DMY") -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4,5}(\.0+)?", s):  # Excel serial
        n = int(float(s))
        if 20000 <= n <= 80000:
            return date(1899, 12, 30) + timedelta(days=n)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ,]*(\d{2,4})", s)
    if m and m[2][:3].lower() in MONTHS:
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        try:
            return date(y, MONTHS[m[2][:3].lower()], int(m[1]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        y += 2000 if y < 100 else 0
        d, mo = (a, b) if order == "DMY" else (b, a)
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def detect_date_order(values: list[str]) -> str:
    """Default DD/MM; switch to MM/DD only if the file clearly uses it."""
    dmy = mdy = 0
    for v in values:
        m = re.fullmatch(r"\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*", str(v or ""))
        if m:
            a, b = int(m[1]), int(m[2])
            if a > 12:
                dmy += 1
            elif b > 12:
                mdy += 1
    return "MDY" if mdy and not dmy else "DMY"


def parse_paid(v: str) -> tuple[int | None, bool]:
    s = str(v or "").strip()
    if not s:
        return None, True
    s = re.sub(r"(?i)(₹|rs\.?|inr|/-|,|\s)", "", s)
    try:
        val = float(s)
        if val < 0:
            return None, False
        return int(round(val * 100)), True
    except ValueError:
        return None, False


def duration_target(text: str) -> tuple[int | None, str | None]:
    t = text.lower()
    m = re.search(r"(\d+)\s*(m|mo|mon|mons|month|months|mth|mths)\b", t)
    if m:
        return int(m[1]) * 30, f"{m[1]}M"
    m = re.search(r"(\d+)\s*(d|day|days)\b", t)
    if m:
        n = int(m[1])
        return n, f"{n} days"
    m = re.search(r"(\d+)\s*(y|yr|yrs|year|years)\b", t)
    if m:
        return int(m[1]) * 365, f"{m[1]}Y"
    for kw, days, label in (("quarter", 90, "3M"), ("half", 180, "6M"), ("annual", 365, "12M"), ("year", 365, "12M"),
                            ("month", 30, "1M")):
        if kw in t:
            return days, label
    return None, None


def match_plan(text: str, plans: list[Plan]) -> tuple[Plan | None, str | None]:
    """exact -> fuzzy -> duration keyword. Returns (plan, reason) where reason is set for non-exact matches."""
    t = (text or "").strip().lower()
    if not t:
        return None, None
    by_name = {p.name.lower(): p for p in plans}
    if t in by_name:
        return by_name[t], None
    close = difflib.get_close_matches(t, list(by_name), n=1, cutoff=0.75)
    if close:
        return by_name[close[0]], f"similar to “{by_name[close[0]].name}”"
    target, label = duration_target(t)
    if target and plans:
        best = min(plans, key=lambda p: abs(p.duration_days - target))
        if abs(best.duration_days - target) <= max(5, target * 0.2):
            return best, f"closest to {label}"
    return None, None


# ---------------- mapping ----------------
def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", h.lower()).strip()


def auto_map(headers: list[str], rows: list[dict], plans: list[Plan]) -> tuple[dict, dict]:
    """-> (mapping header->field|None, confidence header->'high'|'low')."""
    mapping: dict = {h: None for h in headers}
    conf: dict = {}
    used = set()
    nh = {h: re.sub(r"\s+", " ", _norm_header(h)) for h in headers}
    for field in FIELDS:  # pass 1: exact synonyms
        for h in headers:
            if mapping[h] is None and field not in used and nh[h] in SYNONYMS[field]:
                mapping[h], conf[h] = field, "high"
                used.add(field)
    for field in FIELDS:  # pass 2: synonym as whole word inside header (skip short ambiguous ones)
        if field in used:
            continue
        for h in headers:
            if mapping[h] is None and any(len(s) > 3 and re.search(rf"\b{re.escape(s)}\b", nh[h]) for s in SYNONYMS[field]):
                mapping[h], conf[h] = field, "high"
                used.add(field)
                break
    sample = rows[:200]
    for h in headers:  # pass 3: by content
        if mapping[h] is not None:
            continue
        vals = [r.get(h, "") for r in sample if r.get(h, "")]
        if not vals:
            continue
        if "phone" not in used and sum(1 for v in vals if norm_import_phone(v)[0]) / len(vals) >= 0.8:
            mapping[h], conf[h] = "phone", "low"
            used.add("phone")
        elif "plan" not in used and plans and sum(1 for v in vals if match_plan(v, plans)[0]) / len(vals) >= 0.6:
            mapping[h], conf[h] = "plan", "low"
            used.add("plan")
        elif sum(1 for v in vals if parse_date(v)) / len(vals) >= 0.8:
            for f in ("start_date", "end_date"):
                if f not in used:
                    mapping[h], conf[h] = f, "low"
                    used.add(f)
                    break
    return mapping, conf


# ---------------- classify ----------------
def normalize_row(raw: dict, mapping: dict, overrides: dict, plans: list[Plan], plans_by_id: dict,
                  order: str) -> tuple[dict, list, dict | None]:
    val = {}
    for h, f in mapping.items():
        if f and f in FIELDS and f not in val:
            val[f] = raw.get(h, "")
    ov = overrides or {}
    issues, suggestion = [], None
    n: dict = {}

    n["name"] = (ov.get("name") or val.get("name") or "").strip() or None
    if not n["name"]:
        issues.append(issue("name", "NAME_MISSING"))

    if ov.get("phone"):
        n["phone"] = ov["phone"]
    else:
        e164, code, digits = norm_import_phone(val.get("phone", ""))
        n["phone"] = e164
        if code:
            issues.append(issue("phone", code, val.get("phone"), digits))

    plan = None
    plan_text = (val.get("plan") or "").strip()
    n["custom_plan"] = bool(ov.get("custom_plan"))
    if ov.get("plan_id"):
        plan = plans_by_id.get(ov["plan_id"])
    elif plan_text and not n["custom_plan"]:
        plan, reason = match_plan(plan_text, plans)
        if plan and reason:
            suggestion = {"plan_id": plan.id, "plan_name": plan.name, "reason": reason}
        if not plan:
            issues.append(issue("plan", "PLAN_UNKNOWN", plan_text))
    n["plan_id"] = plan.id if plan else None
    n["plan_name"] = plan.name if plan else (plan_text or None)
    n["plan_text"] = plan_text or None

    def getdate(field):
        if ov.get(field):
            return date.fromisoformat(ov[field])
        v = val.get(field, "")
        if not str(v).strip():
            return None
        d = parse_date(v, order)
        if d is None:
            issues.append(issue(field, "DATE_INVALID", v))
        return d

    start, end = getdate("start_date"), getdate("end_date")
    if plan:
        if start and not end:
            end = start + timedelta(days=plan.duration_days - 1)
            suggestion = {**(suggestion or {}), "end_date": end.isoformat(), "reason": (suggestion or {}).get("reason")
                          or f"start + {plan.duration_days} days"}
        elif end and not start:
            start = end - timedelta(days=plan.duration_days - 1)
            suggestion = {**(suggestion or {}), "start_date": start.isoformat(),
                          "reason": (suggestion or {}).get("reason") or f"end − {plan.duration_days} days"}
    elif (start or end) and not (start and end) and not any(i["code"] == "PLAN_UNKNOWN" for i in issues):
        issues.append(issue("end_date" if start else "start_date", "DATES_INCOMPLETE"))
    if start and end and end < start:
        issues.append(issue("end_date", "END_BEFORE_START"))
    n["start_date"], n["end_date"] = (start.isoformat() if start else None), (end.isoformat() if end else None)

    if "paid_paise" in ov and ov["paid_paise"] is not None:
        n["paid_paise"] = int(ov["paid_paise"])
    else:
        paid, ok = parse_paid(val.get("paid", ""))
        if not ok:
            issues.append(issue("paid", "PAID_INVALID", val.get("paid")))
        n["paid_paise"] = paid or 0
    if plan:
        n["price_paise"] = plan.price_paise
        n["due_paise"] = max(0, plan.price_paise - n["paid_paise"])

    po = val.get("paid_on", "")
    paid_on = parse_date(po, order) if str(po or "").strip() else None
    if str(po or "").strip() and not paid_on:
        issues.append(issue("paid_on", "DATE_INVALID", po))
    n["paid_on"] = paid_on.isoformat() if paid_on else None

    email = (ov.get("email") or val.get("email") or "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        issues.append(issue("email", "EMAIL_INVALID", email))
        email = ""
    n["email"] = email or None
    dob = parse_date(val.get("dob", ""), order) if val.get("dob") else None
    n["dob"] = dob.isoformat() if dob else None
    g = (val.get("gender") or "").strip().lower()
    n["gender"] = {"m": "male", "male": "male", "f": "female", "female": "female", "o": "other", "other": "other"}.get(g)
    n["notes"] = (val.get("notes") or "").strip() or None
    n["has_membership"] = bool(start and end)
    return n, issues, suggestion


def classify(db: Session, imp: Import):
    gym = db.get(Gym, imp.gym_id)
    plans = list(db.scalars(select(Plan).where(Plan.gym_id == gym.id, Plan.active.is_(True))))
    plans_by_id = {p.id: p for p in db.scalars(select(Plan).where(Plan.gym_id == gym.id))}
    rows = list(db.scalars(select(ImportRow).where(ImportRow.import_id == imp.id).order_by(ImportRow.row_number)))
    mapping = imp.mapping or {}
    date_cols = [h for h, f in mapping.items() if f in ("start_date", "end_date", "dob")]
    order = detect_date_order([r.raw.get(h, "") for r in rows for h in date_cols])

    existing = {u.phone: (m, u) for m, u in db.execute(
        select(GymMember, User).join(User, User.id == GymMember.user_id).where(GymMember.gym_id == gym.id)).all()}
    first_seen: dict[str, int] = {}
    counts = {"rows": len(rows), "ready": 0, "existing": 0, "fix": 0, "skipped": 0, "imported": 0, "failed": 0}
    for r in rows:
        if r.outcome in ("imported", "failed"):
            counts[r.outcome] += 1
            continue
        n, iss, sug = normalize_row(r.raw, mapping, r.overrides or {}, plans, plans_by_id, order)
        r.member_id = None
        outcome = "ready"
        if (r.overrides or {}).get("skip"):
            outcome = "skipped"
        elif any(i["blocking"] for i in iss):
            outcome = "fix"
        elif n["phone"] in first_seen:
            iss.append(issue("phone", "DUPLICATE_IN_FILE", first_seen[n["phone"]]))
            outcome = "skipped"
        elif n["phone"] in existing:
            outcome = "existing"
            m, _ = existing[n["phone"]]
            r.member_id = m.id
            if n["has_membership"] and overlapping(db, m.id, date.fromisoformat(n["start_date"]),
                                                    date.fromisoformat(n["end_date"])):
                iss.append(issue("start_date", "OVERLAPPING"))
                n["skip_membership"] = True
        if n.get("phone") and n["phone"] not in first_seen and outcome != "skipped":
            first_seen[n["phone"]] = r.row_number
        r.normalized, r.issues, r.suggestion, r.outcome = n, iss, sug, outcome
        counts[outcome] += 1
    imp.counts = counts
    imp.column_info = column_info(imp, rows, plans)


def column_info(imp: Import, rows: list[ImportRow], plans: list[Plan]) -> list[dict]:
    conf = (imp.column_info and {c["header"]: c.get("confidence") for c in imp.column_info}) or {}
    out = []
    for h in imp.headers or []:
        f = (imp.mapping or {}).get(h)
        vals = [r.raw.get(h, "") for r in rows if r.raw.get(h, "")]
        note = None
        if f == "plan" and vals:
            sample = vals[:200]
            note = f"{sum(1 for v in sample if match_plan(v, plans)[0])} of {len(sample)} plan names matched"
        elif f == "phone" and vals:
            note = f"{sum(1 for v in vals if norm_import_phone(v)[0])} of {len(vals)} numbers look valid"
        elif f is None:
            note = "Not imported"
        out.append({"header": h, "field": f, "confidence": conf.get(h) or ("high" if f else None),
                    "example": vals[0] if vals else None, "examples": vals[:3], "note": note})
    return out


# ---------------- jobs ----------------
def job_parse(import_id: str, headers: list[str], parsed_rows: list[tuple[int, dict]]):
    from app.services.push import send_push
    try:
        with session_scope() as db:
            imp = db.get(Import, import_id)
            plans = list(db.scalars(select(Plan).where(Plan.gym_id == imp.gym_id, Plan.active.is_(True))))
            mapping, conf = auto_map(headers, [r for _, r in parsed_rows], plans)
            imp.headers, imp.mapping = headers, mapping
            imp.column_info = [{"header": h, "confidence": conf.get(h)} for h in headers]
            for i in range(0, len(parsed_rows), 500):
                db.add_all([ImportRow(import_id=imp.id, row_number=n, raw=raw, outcome="fix")
                            for n, raw in parsed_rows[i:i + 500]])
                db.flush()
            classify(db, imp)
            imp.status = "previewed"
    except Exception as e:
        log.exception("import parse failed")
        with session_scope() as db:
            imp = db.get(Import, import_id)
            imp.status, imp.error = "failed", f"We couldn’t read this file ({type(e).__name__})."
            uid = imp.created_by
        if uid:
            send_push(uid, "admin", "Import failed", "We couldn’t read your member file.", {"type": "import_failed",
                                                                                              "import_id": import_id})


def _commit_row(db: Session, gym: Gym, imp: Import, r: ImportRow, plans_by_id: dict):
    n = r.normalized
    today = today_in(gym.timezone)
    if r.outcome == "existing":
        member = db.get(GymMember, r.member_id)
    else:
        user, _ = get_or_create_user(db, n["phone"], n["name"], email=n.get("email"),
                                     dob=date.fromisoformat(n["dob"]) if n.get("dob") else None, gender=n.get("gender"))
        member = db.scalar(select(GymMember).where(GymMember.gym_id == gym.id, GymMember.user_id == user.id))
        if not member:
            member = GymMember(gym_id=gym.id, user_id=user.id, member_code=next_member_code(db, gym.id),
                               display_name=n["name"], source="import", import_id=imp.id, notes=n.get("notes"),
                               status="active",
                               joined_on=date.fromisoformat(n["start_date"]) if n.get("start_date") else today)
            db.add(member)
            db.flush()
    ms = None
    if n.get("has_membership") and not n.get("skip_membership"):
        start, end = date.fromisoformat(n["start_date"]), date.fromisoformat(n["end_date"])
        plan = plans_by_id.get(n.get("plan_id")) if n.get("plan_id") else None
        if plan and not overlapping(db, member.id, start, end):
            ms = Membership(gym_id=gym.id, member_id=member.id, plan_id=plan.id, plan_name=plan.name,
                            price_paise=plan.price_paise, discount_paise=0, start_date=start, end_date=end,
                            state="scheduled" if start > today else "active", source="import", created_by=imp.created_by)
            db.add(ms)
            db.flush()
        elif not plan and not overlapping(db, member.id, start, end):
            ms = create_membership(db, gym, imp.created_by, member, plan=None, start=start, end=end,
                                   plan_name=n.get("plan_text") or "Imported plan",
                                   price_paise=n.get("paid_paise") or 0, source="import")
    paid = int(n.get("paid_paise") or 0)
    if paid > 0:
        pay_day = n.get("paid_on") or n.get("start_date")
        paid_at = (local_midnight_utc(date.fromisoformat(pay_day), gym.timezone) + timedelta(hours=12)) if pay_day else utcnow()
        if paid_at > utcnow():
            paid_at = utcnow()
        to_ms = min(paid, ms.price_paise) if ms else 0
        if to_ms > 0:
            add_payment(db, gym, imp.created_by, member, ms, to_ms, "cash", paid_at=paid_at, source="import",
                        note="Imported")
        if paid - to_ms > 0:
            add_payment(db, gym, imp.created_by, member, None, paid - to_ms, "cash", paid_at=paid_at, source="import",
                        note="Imported (credit)")
    db.flush()
    settle_credit(db, gym, member, imp.created_by, ms.id if ms else None)
    r.member_id = member.id
    r.outcome = "imported"


def job_commit(import_id: str):
    from app.services.push import send_push
    with session_scope() as db:
        imp = db.get(Import, import_id)
        row_ids = list(db.scalars(select(ImportRow.id).where(ImportRow.import_id == import_id,
                                                             ImportRow.outcome.in_(("ready", "existing")))
                                  .order_by(ImportRow.row_number)))
        gym_id, creator = imp.gym_id, imp.created_by
    imported = failed = 0

    def run_batch(ids):
        with session_scope() as db:
            gym = db.get(Gym, gym_id)
            imp = db.get(Import, import_id)
            plans_by_id = {p.id: p for p in db.scalars(select(Plan).where(Plan.gym_id == gym_id))}
            for r in db.scalars(select(ImportRow).where(ImportRow.id.in_(ids)).order_by(ImportRow.row_number)):
                _commit_row(db, gym, imp, r, plans_by_id)

    for i in range(0, len(row_ids), BATCH):
        ids = row_ids[i:i + BATCH]
        ok = False
        for _ in range(2):  # a failed batch retries once
            try:
                run_batch(ids)
                ok = True
                break
            except Exception as e:
                log.warning("import %s batch %s failed: %s", import_id, i // BATCH, e)
        if ok:
            imported += len(ids)
        else:  # isolate the bad rows, mark them failed with the reason
            for rid in ids:
                try:
                    run_batch([rid])
                    imported += 1
                except Exception as e:
                    failed += 1
                    with session_scope() as db:
                        r = db.get(ImportRow, rid)
                        r.outcome = "failed"
                        r.issues = (r.issues or []) + [{"field": None, "code": "COMMIT_FAILED",
                                                        "message": getattr(e, "message", None) or str(e)[:200],
                                                        "blocking": True}]
        with session_scope() as db:
            imp = db.get(Import, import_id)
            c = dict(imp.counts or {})
            c.update(imported=c.get("imported_before", 0) + imported, failed=failed, processed=imported + failed)
            imp.counts = c

    with session_scope() as db:
        imp = db.get(Import, import_id)
        imp.status = "completed"
        imp.completed_at = utcnow()
        c = dict(imp.counts or {})
        c.update(imported=c.get("imported_before", 0) + imported, failed=failed, processed=imported + failed)
        imp.counts = c
        audit(db, gym_id, creator, "import.commit", "import", import_id,
              f"Imported {imported} members from {imp.file_name}" + (f" ({failed} failed)" if failed else ""))
    if creator:
        send_push(creator, "admin", "Import finished", f"{imported} members imported" + (f", {failed} failed" if failed else ""),
                  {"type": "import_completed", "import_id": import_id})
