"""Delete a gym for good, after keeping a copy.

Which rows belong to a gym is worked out from the schema, not a hand-kept list, so a table added later is not
forgotten: every table with a `gym_id` column, the `gyms` row itself, and tables that hang off those through a
foreign key (membership freezes, import rows). They are copied into one compressed archive row and then deleted
children-first, all in the caller's transaction. Nothing is half-deleted: any error rolls the whole thing back.

People are platform accounts, not gym rows. An account is removed only when it never logged in, is not the owner,
not a platform admin, and nothing else (another gym, a visit, a session…) points at it. Anyone who uses the app
keeps their account and their own data (food log, progress); only their membership of this gym goes.

Uploaded files (photos, documents, logo, import sheets) are deleted after the commit unless another gym uses the
same file. Their keys stay in the archive; the bytes do not."""
import json
import zlib
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Table, delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Gym, GymArchive, GymDocument, GymPhoto, Import, User
from app.utils import utcnow

SKIP = {"gym_deletion_requests", "gym_archives"}   # these outlive the gym on purpose
USER_COLS = ("id", "phone", "name", "email", "dob", "gender", "first_login_at", "last_login_at", "created_at")
CHUNK = 1000


def _plan() -> list[tuple[Table, str]]:
    """(table, how) for every table holding this gym's rows, in a safe delete order (children first).
    how = "self" (gyms), "gym_id", or "child" (reached through a foreign key)."""
    import app.models  # noqa: F401
    tables = Base.metadata.tables
    scoped = {n for n, t in tables.items() if "gym_id" in t.c and n not in SKIP} | {"gyms"}
    out = []
    for t in reversed(Base.metadata.sorted_tables):
        if t.name in SKIP:
            continue
        if t.name == "gyms":
            out.append((t, "self"))
        elif t.name in scoped:
            out.append((t, "gym_id"))
        elif t.name != "users" and any(fk.column.table.name in scoped for fk in t.foreign_keys):
            out.append((t, "child"))
    return out


def _where(t: Table, how: str, gid: str):
    if how == "self":
        return t.c.id == gid
    if how == "gym_id":
        return t.c.gym_id == gid
    conds = []
    for fk in t.foreign_keys:
        parent = fk.column.table
        if parent.name == "gyms":
            conds.append(fk.parent == gid)
        elif "gym_id" in parent.c and parent.name not in SKIP:
            conds.append(fk.parent.in_(select(fk.column).where(parent.c.gym_id == gid)))
    return or_(*conds)


def _plain(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return v


def preview(db: Session, gid: str) -> dict:
    """How many rows each table holds for this gym (for the confirmation screens)."""
    n = {}
    for t, how in _plan():
        c = db.scalar(select(func.count()).select_from(t).where(_where(t, how, gid)))
        if c:
            n[t.name] = c
    return n


def _file_keys(db: Session, gid: str) -> set[str]:
    keys = set(db.scalars(select(GymPhoto.file_key).where(GymPhoto.gym_id == gid)))
    keys |= set(db.scalars(select(GymDocument.file_key).where(GymDocument.gym_id == gid)))
    keys |= {k for k in db.scalars(select(Import.file_key).where(Import.gym_id == gid)) if k}
    logo = db.scalar(select(Gym.logo_url).where(Gym.id == gid))
    if logo:
        keys.add(logo)
    return {k for k in keys if k and not k.startswith(("http://", "https://"))}


def _still_used(db: Session, keys: set[str]) -> set[str]:
    """Keys some other gym or person still points at (a branch copies its parent's logo)."""
    if not keys:
        return set()
    k = list(keys)
    used = set(db.scalars(select(GymPhoto.file_key).where(GymPhoto.file_key.in_(k))))
    used |= set(db.scalars(select(GymDocument.file_key).where(GymDocument.file_key.in_(k))))
    used |= set(db.scalars(select(Import.file_key).where(Import.file_key.in_(k))))
    used |= set(db.scalars(select(Gym.logo_url).where(Gym.logo_url.in_(k))))
    used |= set(db.scalars(select(User.photo_url).where(User.photo_url.in_(k))))
    return used


def _user_refs() -> list:
    """Every column with a foreign key to users.id."""
    import app.models  # noqa: F401
    return [fk.parent for t in Base.metadata.sorted_tables for fk in t.foreign_keys
            if fk.column.table.name == "users" and fk.column.name == "id"]


def _orphans(db: Session, cand: list[str]) -> list[str]:
    left = set(cand)
    for col in _user_refs():
        for i in range(0, len(cand), CHUNK):
            part = [c for c in cand[i:i + CHUNK] if c in left]
            if part:
                left -= set(db.scalars(select(col).where(col.in_(part))))
    return [c for c in cand if c in left]


def max_packet(db: Session) -> int:
    try:
        return int(db.execute(text("SELECT @@max_allowed_packet")).scalar())
    except Exception:
        return 64 * 1024 * 1024


class TooBig(Exception):
    pass


def archive_and_delete(db: Session, gym: Gym, actor_id: str, request_id: str | None = None,
                       reason: str | None = None) -> tuple[GymArchive, list[str]]:
    """Copy the gym into an archive, delete it, and return (archive, file keys to delete after commit).
    Does not commit."""
    gid = gym.id
    plan = _plan()
    rows: dict[str, list] = {}
    for t, how in plan:
        got = [{k: _plain(v) for k, v in r._mapping.items()} for r in db.execute(select(t).where(_where(t, how, gid)))]
        if got:
            rows[t.name] = got
    owner = db.get(User, gym.owner_user_id)
    people = {r["user_id"] for name in ("gym_members", "gym_staff", "gym_visits") for r in rows.get(name, []) if r.get("user_id")}
    people.add(gym.owner_user_id)
    ulist = sorted(people)
    users = []
    for i in range(0, len(ulist), CHUNK):
        for u in db.execute(select(*[getattr(User, c) for c in USER_COLS]).where(User.id.in_(ulist[i:i + CHUNK]))):
            users.append({k: _plain(v) for k, v in u._mapping.items()})
    keys = _file_keys(db, gid)
    doc = {"format": "dumbbel.gym-archive/1", "gym_id": gid, "archived_at": utcnow().isoformat(),
           "tables": rows, "people": users, "files": sorted(keys),
           "note": "Rows as they were in the database. Uploaded files were deleted; only their keys are listed."}
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    blob = zlib.compress(raw, 6)
    if len(blob) > max_packet(db) * 0.9:
        raise TooBig(f"The archive would be {len(blob) // 1048576} MB, more than the database accepts in one write.")

    # accounts that exist only because of this gym (imported or added, never logged in)
    cand = sorted({r["user_id"] for name in ("gym_members", "gym_staff") for r in rows.get(name, []) if r.get("user_id")}
                  - {gym.owner_user_id})
    if cand:
        quiet = []
        for i in range(0, len(cand), CHUNK):
            quiet += db.scalars(select(User.id).where(User.id.in_(cand[i:i + CHUNK]), User.first_login_at.is_(None),
                                                      User.is_platform_admin.is_(False)))
        cand = sorted(quiet)

    snap = {"name": gym.name, "code_prefix": gym.code_prefix, "city": gym.city, "owner_user_id": gym.owner_user_id}
    for t, how in plan:      # children first; gyms is last
        db.execute(delete(t).where(_where(t, how, gid)))
    removed_users = _orphans(db, cand) if cand else []
    for i in range(0, len(removed_users), CHUNK):
        db.execute(delete(User).where(User.id.in_(removed_users[i:i + CHUNK])))
    db.flush()
    gone = sorted(keys - _still_used(db, keys))

    counts = {"tables": {k: len(v) for k, v in rows.items()}, "rows": sum(len(v) for v in rows.values()),
              "files_deleted": len(gone), "files_kept_shared": len(keys) - len(gone), "accounts_removed": len(removed_users)}
    arc = GymArchive(gym_id=gid, gym_name=snap["name"], code_prefix=snap["code_prefix"], city=snap["city"],
                     owner_user_id=snap["owner_user_id"], owner_name=owner.name if owner else None,
                     owner_phone=owner.phone if owner else None, request_id=request_id, reason=reason, counts=counts,
                     raw_bytes=len(raw), data_bytes=len(blob), data=blob, deleted_by=actor_id,
                     deleted_at=utcnow())
    db.add(arc)
    db.flush()
    return arc, gone


def read_archive(arc: GymArchive) -> bytes:
    return zlib.decompress(arc.data)
