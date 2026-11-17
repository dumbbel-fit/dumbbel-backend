"""§14 — internal approval console (platform admins) + file uploads."""
import time
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, platform_admin
from app.errors import ApiError, validation
from app.models import Gym, GymDocument, GymMember, GymStaff, Session as DbSession, User
from app.schemas import AdminIn, ChangePhoneIn, PresignIn, ReasonIn
from app.services import storage
from app.services.audit import audit
from app.services.core import ser_gym, ser_user
from app.services.notify import queue_gym_status_sms
from app.utils import clamp_limit, decode_cursor, encode_cursor, iso, normalize_phone, utcnow, uuid7
from app.responses import DocRoute, ERRORS, AdminsOut, GymListOut, GymOut, PresignOut, UserWrapOut

router = APIRouter(prefix="/internal", tags=["Internal console"], route_class=DocRoute, responses=ERRORS)
"""Console access = users.is_platform_admin. Grant it with POST /internal/admins (by an existing admin) or
`python -m scripts.make_admin <phone>`. Everyone else gets 403 NOT_PLATFORM_ADMIN on /internal/*."""
uploads = APIRouter(tags=["Files"], route_class=DocRoute, responses=ERRORS)


MIN_GYM_PHOTOS = 3


def _gym_out(db: Session, g: Gym, docs: bool = False) -> dict:
    owner = db.get(User, g.owner_user_id)
    d = {**ser_gym(g), "owner": {"id": owner.id, "name": owner.name, "phone": owner.phone} if owner else None,
         "is_test": bool(g.is_test), "test_marked_at": iso(g.test_marked_at),
         "counts": {"members": db.scalar(select(func.count()).select_from(GymMember).where(GymMember.gym_id == g.id)),
                    "staff": db.scalar(select(func.count()).select_from(GymStaff).where(
                        GymStaff.gym_id == g.id, GymStaff.status != "deactivated"))}}
    if docs:
        from app.models import GymPhoto
        rows = db.scalars(select(GymPhoto).where(GymPhoto.gym_id == g.id).order_by(GymPhoto.sort_order, GymPhoto.created_at)).all()
        ok = storage.existing_keys([p.file_key for p in rows])
        d["photos"] = [{"id": p.id, "url": storage.public_url(p.file_key)} for p in rows if p.file_key in ok]
        d["photos_missing"] = sum(1 for p in rows if p.file_key not in ok)
        d["photos_required"] = MIN_GYM_PHOTOS
        d["documents"] = [{"id": x.id, "type": x.type, "status": x.status, "review_note": x.review_note,
                           "url": storage.signed_get_url(x.file_key, 300), "created_at": iso(x.created_at)}
                          for x in db.scalars(select(GymDocument).where(GymDocument.gym_id == g.id)
                                              .order_by(GymDocument.created_at))]
    return d


@router.get("/gyms", response_model=GymListOut, summary="List gyms for review")
def list_gyms(status: Literal["pending", "active", "suspended", "rejected"] | None = None, q: str | None = None,
              limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
              admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    stmt = select(Gym)
    if status:
        stmt = stmt.where(Gym.status == status)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Gym.name.ilike(like), Gym.city.ilike(like), Gym.area.ilike(like),
                              Gym.code_prefix.ilike(like), Gym.phone.ilike(like)))
    c = decode_cursor(cursor)
    if c:
        stmt = stmt.where(Gym.id < c["id"])
    lim = clamp_limit(limit)
    rows = db.scalars(stmt.order_by(Gym.id.desc()).limit(lim + 1)).all()
    nxt = encode_cursor({"id": rows[lim - 1].id}) if len(rows) > lim else None
    return {"items": [_gym_out(db, g, docs=True) for g in rows[:lim]], "next_cursor": nxt}


@router.get("/gyms/{id}", response_model=GymOut, summary="Gym detail with documents (signed URLs, 5 min)")
def get_gym(id: str, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    g = db.get(Gym, id)
    if not g:
        raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
    return _gym_out(db, g, docs=True)


TRANSITIONS = {"approve": (("pending", "rejected"), "active", "gym_approved"),
               "reject": (("pending",), "rejected", "gym_rejected"),
               "suspend": (("active", "pending"), "suspended", "gym_suspended"),
               "reinstate": (("suspended", "rejected"), "active", "gym_reinstated")}


def _transition(action: str):
    def handler(id: str, body: ReasonIn | None = None, admin: User = Depends(platform_admin),
                db: Session = Depends(get_db)):
        g = db.get(Gym, id)
        if not g:
            raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
        allowed, new, sms_kind = TRANSITIONS[action]
        if g.status not in allowed:
            raise ApiError(409, "NOT_ACTIVE", f"Can’t {action} a gym that is {g.status}.")
        reason = body.reason if body else None
        if action == "approve":
            from app.models import GymPhoto
            keys = db.scalars(select(GymPhoto.file_key).where(GymPhoto.gym_id == g.id)).all()
            real = storage.existing_keys(keys)
            n_photos = sum(1 for k in keys if k in real)
            if n_photos < MIN_GYM_PHOTOS:
                raise ApiError(409, "PHOTOS_REQUIRED", f"This gym has {n_photos} photo{'s' if n_photos != 1 else ''}. "
                               f"At least {MIN_GYM_PHOTOS} are needed before approval.", {"photos": n_photos, "required": MIN_GYM_PHOTOS})
        if action in ("reject", "suspend") and not reason:
            raise validation({"reason": "A reason is required; it’s shown to the owner."})
        before = g.status
        g.status, g.status_reason = new, reason if new != "active" else None
        now = utcnow()
        if new == "active":
            g.approved_at, g.approved_by = g.approved_at or now, g.approved_by or admin.id
        if action == "approve":
            db.execute(update(GymDocument).where(GymDocument.gym_id == g.id, GymDocument.status == "submitted")
                       .values(status="accepted", reviewed_by=admin.id, reviewed_at=now))
        if action == "reject":
            db.execute(update(GymDocument).where(GymDocument.gym_id == g.id, GymDocument.status == "submitted")
                       .values(status="rejected", reviewed_by=admin.id, reviewed_at=now, review_note=reason))
        audit(db, g.id, admin.id, f"gym.{action}", "gym", g.id, f"Gym {new} by platform" + (f": {reason}" if reason else ""),
              {"status": before}, {"status": new, "reason": reason})
        db.commit()
        queue_gym_status_sms(g.id, sms_kind, reason)
        return _gym_out(db, g)
    handler.__name__ = f"gym_{action}"
    return handler


for _a in TRANSITIONS:
    router.add_api_route(f"/gyms/{{id}}/{_a}", _transition(_a), methods=["POST"], response_model=GymOut,
                         summary=f"{_a.title()} gym (SMS to owner)")


class TestFlagIn(BaseModel):
    is_test: bool = Field(..., description="true = test gym: left out of console numbers. Nothing is deleted or changed.")


@router.post("/gyms/{id}/test", response_model=GymOut, summary="Mark / unmark a test gym (console numbers leave it out)",
             description="Only a flag: the gym, its members, payments and everything else stay exactly as they are and "
                         "the gym keeps working. Platform-wide console numbers (overview, insights, gyms totals) skip "
                         "test gyms; the gym's own pages still show its data. Undo any time.")
def set_test_gym(id: str, body: TestFlagIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    g = db.get(Gym, id)
    if not g:
        raise ApiError(404, "NOT_FOUND", "That gym wasn’t found.")
    if bool(g.is_test) != body.is_test:
        g.is_test = body.is_test
        g.test_marked_at, g.test_marked_by = (utcnow(), admin.id) if body.is_test else (None, None)
        audit(db, g.id, admin.id, "gym.test_mark" if body.is_test else "gym.test_unmark", "gym", g.id,
              f"{'Marked' if body.is_test else 'Unmarked'} {g.name} as a test gym",
              {"is_test": not body.is_test}, {"is_test": body.is_test})
        db.commit()
    return _gym_out(db, g, docs=True)


@router.post("/users/{id}/change-phone", response_model=UserWrapOut, summary="Support-only phone change (revokes sessions)")
def change_phone(id: str, body: ChangePhoneIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    u = db.get(User, id)
    if not u or u.deleted_at:
        raise ApiError(404, "NOT_FOUND", "That user wasn’t found.")
    new = normalize_phone(body.new_phone, "new_phone")
    if db.scalar(select(User.id).where(User.phone == new, User.id != u.id)):
        raise ApiError(409, "MEMBER_EXISTS", "Another account already uses that number.")
    old = u.phone
    u.phone = new
    db.execute(update(DbSession).where(DbSession.user_id == u.id, DbSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, None, admin.id, "user.change_phone", "user", u.id, f"Phone changed by support (ticket {body.ticket_ref})",
          {"phone": old}, {"phone": new, "ticket_ref": body.ticket_ref})
    db.commit()
    return {"user": ser_user(u)}


# ---------------- console admins ----------------
@router.get("/admins", response_model=AdminsOut, summary="Who has console access (platform admins)")
def list_admins(admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.is_platform_admin.is_(True), User.deleted_at.is_(None))).all()
    return {"items": [{**ser_user(u), "is_platform_admin": True} for u in rows]}


@router.post("/admins", response_model=UserWrapOut, summary="Grant console access to a phone number")
def add_admin(body: AdminIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    u = db.scalar(select(User).where(User.phone == phone))
    if not u:
        u = User(phone=phone, name=body.name)
        db.add(u)
        db.flush()
    u.is_platform_admin = True
    audit(db, None, admin.id, "admin.grant", "user", u.id, f"Granted console access to {u.name or phone}")
    db.commit()
    return {"user": {**ser_user(u), "is_platform_admin": True}}


@router.delete("/admins/{userId}", status_code=204, summary="Revoke console access")
def remove_admin(userId: str, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    u = db.get(User, userId)
    if not u or not u.is_platform_admin:
        raise ApiError(404, "NOT_FOUND", "That admin wasn’t found.")
    if u.id == admin.id:
        raise ApiError(409, "NOT_ACTIVE", "You can’t remove your own console access.")
    u.is_platform_admin = False
    audit(db, None, admin.id, "admin.revoke", "user", u.id, f"Revoked console access from {u.name or u.phone}")
    db.commit()
    return Response(status_code=204)


# ---------------- uploads ----------------
LIMITS = {"member_photo": ({"image/jpeg", "image/png", "image/webp"}, 10 * 1024 * 1024),
          "gym_logo": ({"image/jpeg", "image/png", "image/webp"}, 10 * 1024 * 1024),
          "gym_photo": ({"image/jpeg", "image/png", "image/webp"}, 10 * 1024 * 1024),
          "gym_document": ({"application/pdf", "image/jpeg", "image/png"}, 5 * 1024 * 1024),
          "import_file": ({"text/csv", "application/vnd.ms-excel", "application/csv", "text/plain",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}, 10 * 1024 * 1024)}
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf", "text/csv": "csv",
       "application/csv": "csv", "text/plain": "csv", "application/vnd.ms-excel": "xls",
       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx"}


@uploads.post("/uploads/presign", response_model=PresignOut, summary="Get a presigned upload URL",
              description="PUT the file bytes to `upload_url` with the same Content-Type, then pass `file_key` to the API. "
                          "Limits: images 10 MB (jpg/png/webp) · documents 5 MB (pdf/jpg/png) · import 10 MB (csv/xls/xlsx).")
def presign(body: PresignIn, user: User = Depends(current_user)):
    types, max_size = LIMITS[body.purpose]
    if body.content_type not in types:
        raise ApiError(415, "FILE_TYPE", "That file type isn’t allowed here.", {"allowed": sorted(types)})
    if body.size > max_size:
        raise ApiError(413, "FILE_TOO_LARGE", f"Max {max_size // (1024 * 1024)} MB.")
    key = f"{body.purpose}/{user.id}/{uuid7()}.{EXT[body.content_type]}"
    return {"upload_url": storage.presign_put(key, body.content_type, 600), "file_key": key, "expires_in_sec": 600,
            "method": "PUT", "headers": {"Content-Type": body.content_type}}


@uploads.put("/uploads/local/{key:path}", status_code=204, include_in_schema=False)
async def local_put(key: str, exp: int, sig: str, request: Request):
    if storage.use_s3() or not storage.verify_local_sig("PUT", key, exp, sig):
        raise ApiError(403, "FORBIDDEN", "Upload link expired.")
    purpose = key.split("/", 1)[0]
    max_size = LIMITS.get(purpose, (None, 5 * 1024 * 1024))[1]
    data = await request.body()
    if len(data) > max_size:
        raise ApiError(413, "FILE_TOO_LARGE")
    from starlette.concurrency import run_in_threadpool
    await run_in_threadpool(storage.put_bytes, key, data, request.headers.get("content-type") or "application/octet-stream")
    return Response(status_code=204)


@uploads.get("/files/{key:path}", include_in_schema=False)
def local_get(key: str, exp: int, sig: str):
    if storage.use_s3() or not storage.verify_local_sig("GET", key, exp, sig):
        raise ApiError(403, "FORBIDDEN", "Link expired.")
    try:
        data = storage.get_bytes(key)
    except Exception:
        raise ApiError(404, "NOT_FOUND")
    ext = key.rsplit(".", 1)[-1].lower()
    mt = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf",
          "csv": "text/csv"}.get(ext, "application/octet-stream")
    cache = "private, max-age=86400" if key.startswith(("member_photo/", "gym_logo/", "gym_photo/")) else "private, max-age=300"
    return Response(data, media_type=mt, headers={"Cache-Control": cache})


_ = time
