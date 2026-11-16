"""Gym deletion: only the owner can ask, only the console can delete.

Owner (Admin app):  GET/POST/DELETE /gyms/{gymId}/deletion-request
Console:            GET /internal/gym-deletions, POST …/{id}/approve | reject,
                    GET /internal/gym-archives, GET /internal/gym-archives/{id}/download

Approving copies every row of the gym into `gym_archives` and then deletes it (see services/gym_delete.py), so the
owner can register the same gym again from scratch, with the same code if they like."""
import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, current_user, load_gym_ctx, platform_admin
from app.errors import ApiError, validation
from app.models import Gym, GymArchive, GymDeletionRequest, GymMember, GymStaff, GymWhatsApp, Membership, Payment, User
from app.responses import DocRoute, ERRORS
from app.services import gym_delete
from app.services.audit import audit
from app.services.jobs import enqueue
from app.utils import iso, today_in, utcnow

log = logging.getLogger("dumbbel.gym_delete")
owner = APIRouter(tags=["Gym deletion"], route_class=DocRoute, responses=ERRORS)
console = APIRouter(prefix="/internal", tags=["Gym deletion"], route_class=DocRoute, responses=ERRORS)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def owner_any(gymId: str = Path(..., description="Gym id"), user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> GymCtx:
    """Owner of the gym, whatever its status (a suspended gym's owner may still ask to leave)."""
    ctx = load_gym_ctx(db, user, gymId, allow_suspended=True)
    ctx.require_owner()
    return ctx


def _summary(db: Session, gid: str) -> dict:
    g = db.get(Gym, gid)
    if not g:
        return {"exists": False}
    today = today_in(g.timezone)
    wa = db.get(GymWhatsApp, gid)
    return {
        "exists": True, "status": g.status,
        "members": db.scalar(select(func.count()).select_from(GymMember).where(GymMember.gym_id == gid)) or 0,
        "active_memberships": db.scalar(select(func.count()).select_from(Membership).where(
            Membership.gym_id == gid, Membership.state.in_(("active", "frozen")), Membership.end_date >= today)) or 0,
        "payments": db.scalar(select(func.count()).select_from(Payment).where(Payment.gym_id == gid, Payment.status == "recorded")) or 0,
        "payments_paise": int(db.scalar(select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
            Payment.gym_id == gid, Payment.status == "recorded")) or 0),
        "staff": db.scalar(select(func.count()).select_from(GymStaff).where(GymStaff.gym_id == gid, GymStaff.status != "deactivated")) or 0,
        "whatsapp_linked": bool(wa and wa.status == "connected"),
        "branch_of_group": bool(g.org_id and db.scalar(select(func.count()).select_from(Gym).where(Gym.org_id == g.org_id, Gym.id != gid))),
    }


def _ser(r: GymDeletionRequest, db: Session | None = None, live: bool = False) -> dict:
    d = {"id": r.id, "gym_id": r.gym_id, "gym_name": r.gym_name, "code_prefix": r.code_prefix, "city": r.city,
         "status": r.status, "reason": r.reason,
         "requested_by": {"id": r.requested_by, "name": r.requester_name, "phone": r.requester_phone},
         "created_at": iso(r.created_at), "decided_at": iso(r.decided_at), "decision_note": r.decision_note,
         "archive_id": r.archive_id}
    if live and db is not None:
        d["gym"] = _summary(db, r.gym_id) if r.status == "pending" else None
    return d


def _push(user_id: str, title: str, body: str, data: dict):
    def run():
        from app.services.push import send_push
        send_push(user_id, "admin", title, body, data)
    enqueue("notify.gym_deletion", run)


# ---------------------------------------------------------------- owner
class DeletionIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000, description="Why the gym is leaving (shown to the DUMBBEL team)")
    confirm_name: str = Field(..., max_length=200, description="The gym's name, typed again to confirm")


@owner.get("/gyms/{gymId}/deletion-request", summary="This gym's deletion request, if any, and what deleting would remove (owner)")
def get_request(ctx: GymCtx = Depends(owner_any), db: Session = Depends(get_db)):
    r = db.scalar(select(GymDeletionRequest).where(GymDeletionRequest.gym_id == ctx.gym.id)
                  .order_by(GymDeletionRequest.created_at.desc()).limit(1))
    return {"request": _ser(r) if r else None, "gym": _summary(db, ctx.gym.id)}


@owner.post("/gyms/{gymId}/deletion-request", status_code=201, summary="Ask DUMBBEL.fit to delete this gym (owner only)")
def create_request(body: DeletionIn, ctx: GymCtx = Depends(owner_any), db: Session = Depends(get_db)):
    if _norm(body.confirm_name) != _norm(ctx.gym.name):
        raise validation({"confirm_name": "Type the gym name exactly as it is shown."})
    if not body.reason.strip():
        raise validation({"reason": "Tell us why, in a few words."})
    if db.scalar(select(GymDeletionRequest.id).where(GymDeletionRequest.gym_id == ctx.gym.id,
                                                     GymDeletionRequest.status == "pending")):
        raise ApiError(409, "ALREADY_REQUESTED", "A deletion request for this gym is already waiting.")
    r = GymDeletionRequest(gym_id=ctx.gym.id, gym_name=ctx.gym.name, code_prefix=ctx.gym.code_prefix, city=ctx.gym.city,
                           requested_by=ctx.user.id, requester_name=ctx.user.name, requester_phone=ctx.user.phone,
                           reason=body.reason.strip(), status="pending")
    db.add(r)
    audit(db, ctx.gym.id, ctx.user.id, "gym.deletion_requested", "gym", ctx.gym.id,
          f"Owner asked to delete {ctx.gym.name}", None, {"reason": r.reason})
    try:
        db.commit()
    except Exception as e:  # two taps at once: the unique "one pending" guard caught the second
        db.rollback()
        if "uq_gym_del_one_pending" in str(e) or "Duplicate" in str(e):
            raise ApiError(409, "ALREADY_REQUESTED", "A deletion request for this gym is already waiting.")
        raise
    return {"request": _ser(r), "gym": _summary(db, ctx.gym.id)}


@owner.delete("/gyms/{gymId}/deletion-request", summary="Withdraw a waiting deletion request (owner only)")
def cancel_request(ctx: GymCtx = Depends(owner_any), db: Session = Depends(get_db)):
    r = db.scalar(select(GymDeletionRequest).where(GymDeletionRequest.gym_id == ctx.gym.id,
                                                   GymDeletionRequest.status == "pending").with_for_update())
    if not r:
        raise ApiError(404, "NOT_FOUND", "There’s no waiting deletion request for this gym.")
    r.status, r.decided_at, r.decided_by, r.decision_note = "cancelled", utcnow(), ctx.user.id, "Withdrawn by the owner"
    audit(db, ctx.gym.id, ctx.user.id, "gym.deletion_cancelled", "gym", ctx.gym.id, f"Owner withdrew the request to delete {ctx.gym.name}")
    db.commit()
    return {"request": _ser(r), "gym": _summary(db, ctx.gym.id)}


# ---------------------------------------------------------------- console
STATUSES = ("pending", "completed", "rejected", "cancelled")


@console.get("/gym-deletions", summary="Deletion requests from gym owners (pending ones show what would be deleted)")
def list_requests(status: Literal["pending", "completed", "rejected", "cancelled", "all"] = "pending",
                  limit: int = Query(100, ge=1, le=200), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    stmt = select(GymDeletionRequest)
    if status != "all":
        stmt = stmt.where(GymDeletionRequest.status == status)
    rs = db.scalars(stmt.order_by(GymDeletionRequest.created_at.desc()).limit(limit)).all()
    counts = dict(db.execute(select(GymDeletionRequest.status, func.count()).group_by(GymDeletionRequest.status)).all())
    return {"items": [_ser(r, db, live=True) for r in rs], "counts": {s: counts.get(s, 0) for s in STATUSES}}


@console.get("/gym-deletions/{id}", summary="One deletion request with a table-by-table count of what would go")
def get_one(id: str, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    r = db.get(GymDeletionRequest, id)
    if not r:
        raise ApiError(404, "NOT_FOUND", "That request wasn’t found.")
    d = _ser(r, db, live=True)
    if r.status == "pending" and db.get(Gym, r.gym_id):
        d["rows"] = gym_delete.preview(db, r.gym_id)
    return d


class ApproveIn(BaseModel):
    confirm_code: str = Field(..., max_length=3, description="The gym's 3-letter code, typed to confirm")
    note: str | None = Field(None, max_length=500)


class RejectIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500, description="Shown to the owner")


@console.post("/gym-deletions/{id}/approve", summary="Archive and delete the gym. Cannot be undone from the app")
def approve(id: str, body: ApproveIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    r = db.scalar(select(GymDeletionRequest).where(GymDeletionRequest.id == id).with_for_update())
    if not r:
        raise ApiError(404, "NOT_FOUND", "That request wasn’t found.")
    if r.status != "pending":
        raise ApiError(409, "NOT_PENDING", f"This request is already {r.status}.")
    g = db.scalar(select(Gym).where(Gym.id == r.gym_id).with_for_update())
    if not g:
        raise ApiError(409, "GYM_GONE", "That gym no longer exists.")
    if (body.confirm_code or "").strip().upper() != (g.code_prefix or "").upper():
        raise validation({"confirm_code": f"Type the gym code ({g.code_prefix}) to confirm."})
    wa = db.get(GymWhatsApp, g.id)
    unlink = bool(wa and wa.provider == "web" and wa.instance_name)
    owner_id, name, gid = g.owner_user_id, g.name, g.id
    try:
        arc, files = gym_delete.archive_and_delete(db, g, admin.id, r.id, r.reason)
    except gym_delete.TooBig as e:
        db.rollback()
        raise ApiError(413, "ARCHIVE_TOO_BIG", str(e))
    r.status, r.decided_by, r.decided_at, r.archive_id = "completed", admin.id, utcnow(), arc.id
    r.decision_note = (body.note or "").strip() or None
    audit(db, gid, admin.id, "gym.deleted", "gym", gid,
          f"Deleted {name} ({r.code_prefix}) at the owner’s request; archived {arc.counts['rows']} rows",
          None, {"archive_id": arc.id, "counts": arc.counts})
    db.commit()
    log.info("gym %s deleted by %s, archive %s (%s rows, %s bytes)", gid, admin.id, arc.id, arc.counts["rows"], arc.data_bytes)

    def after():
        from app.services import storage
        for k in files:
            storage.delete(k)
        if unlink:
            from app.services import whatsapp as wam
            wam.web_remove(gid)
    enqueue("gym.delete.cleanup", after)
    _push(owner_id, "Your gym was deleted", f"{name} has been removed from DUMBBEL.fit as you asked. "
          "You can register a gym again any time.", {"type": "gym_deleted", "gym_id": gid})
    return {"request": _ser(r), "archive": _arc(arc)}


@console.post("/gym-deletions/{id}/reject", summary="Turn a deletion request down (the owner is told why)")
def reject(id: str, body: RejectIn, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    r = db.scalar(select(GymDeletionRequest).where(GymDeletionRequest.id == id).with_for_update())
    if not r:
        raise ApiError(404, "NOT_FOUND", "That request wasn’t found.")
    if r.status != "pending":
        raise ApiError(409, "NOT_PENDING", f"This request is already {r.status}.")
    r.status, r.decided_by, r.decided_at, r.decision_note = "rejected", admin.id, utcnow(), body.reason.strip()
    audit(db, r.gym_id, admin.id, "gym.deletion_rejected", "gym", r.gym_id,
          f"Deletion of {r.gym_name} turned down: {r.decision_note}")
    db.commit()
    _push(r.requested_by, "Gym deletion request", f"We didn’t delete {r.gym_name}: {r.decision_note}",
          {"type": "gym_deletion_rejected", "gym_id": r.gym_id})
    return {"request": _ser(r)}


def _arc(a) -> dict:
    return {"id": a.id, "gym_id": a.gym_id, "gym_name": a.gym_name, "code_prefix": a.code_prefix, "city": a.city,
            "owner": {"id": a.owner_user_id, "name": a.owner_name, "phone": a.owner_phone}, "reason": a.reason,
            "counts": a.counts, "raw_bytes": a.raw_bytes, "data_bytes": a.data_bytes, "request_id": a.request_id,
            "deleted_by": a.deleted_by, "deleted_at": iso(a.deleted_at)}


ARC_COLS = [c for c in GymArchive.__table__.c if c.name != "data"]


@console.get("/gym-archives", summary="Deleted gyms kept as archives (newest first)")
def list_archives(limit: int = Query(100, ge=1, le=200), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    rs = db.execute(select(*ARC_COLS).order_by(GymArchive.deleted_at.desc()).limit(limit)).all()
    names = dict(db.execute(select(User.id, User.name).where(User.id.in_({r.deleted_by for r in rs}))).all()) if rs else {}
    return {"items": [{**_arc(r), "deleted_by_name": names.get(r.deleted_by)} for r in rs]}


@console.get("/gym-archives/{id}/download", summary="Download an archive as JSON (every row the gym had)",
             responses={200: {"content": {"application/json": {}}}})
def download_archive(id: str, admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    a = db.get(GymArchive, id)
    if not a:
        raise ApiError(404, "NOT_FOUND", "That archive wasn’t found.")
    audit(db, a.gym_id, admin.id, "gym_archive.downloaded", "gym_archive", a.id, f"Downloaded the archive of {a.gym_name}")
    db.commit()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", a.gym_name).strip("-")[:40] or "gym"
    return Response(gym_delete.read_archive(a), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-{a.code_prefix or "archive"}.json"',
                             "Cache-Control": "no-store"})
