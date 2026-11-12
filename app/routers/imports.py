"""§11 — import (4 steps) and invites."""
import csv
import io
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, gym_ctx
from app.errors import ApiError, validation
from app.models import GymMember, Import, ImportRow, Invite, Plan, User
from app.schemas import ImportRowPatch, InvitesIn, MappingIn
from app.services import importer, storage
from app.services.audit import audit
from app.services.jobs import enqueue
from app.services.notify import queue_invite
from app.services.rules import LIVE, load_states
from app.utils import clamp_limit, decode_cursor, encode_cursor, iso, normalize_phone, utcnow, uuid7

router = APIRouter(tags=["Import & invites"])
public = APIRouter(tags=["Import & invites"])

TEMPLATE = ("Name,Mobile,Plan,Start Date,End Date,Paid,Email,DOB,Gender,Notes\r\n"
            "Ravi Teja Naidu,9849011234,Quarterly,01/11/2026,31/01/2027,5400,ravi@example.com,14/02/1995,Male,\r\n")


@public.get("/imports/template.csv", summary="Download import template (public)",
            responses={200: {"content": {"text/csv": {}}}})
def template():
    return Response(TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="dumbbel-members-template.csv"'})


def _get_import(db: Session, ctx: GymCtx, import_id: str) -> Import:
    imp = db.get(Import, import_id)
    if not imp or imp.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That import wasn’t found.")
    return imp


def ser_import(imp: Import) -> dict:
    return {"id": imp.id, "status": imp.status, "file_name": imp.file_name, "file_size": imp.file_size,
            "counts": imp.counts or {}, "columns": imp.column_info if imp.status not in ("uploaded", "parsing") else [],
            "mapping": imp.mapping, "error": imp.error, "created_at": iso(imp.created_at),
            "committed_at": iso(imp.committed_at), "completed_at": iso(imp.completed_at)}


@router.post("/gyms/{gymId}/imports", status_code=202, summary="Upload member file (multipart `file`, or JSON {file_key})",
             description="CSV / XLS / XLSX, max 10 MB / 5,000 rows. Parsing, auto-mapping and classification run as a job; "
                         "poll GET import until status is `previewed`.",
             openapi_extra={"requestBody": {"content": {
                 "multipart/form-data": {"schema": {"type": "object", "properties": {
                     "file": {"type": "string", "format": "binary"}}, "required": ["file"]}},
                 "application/json": {"schema": {"type": "object", "properties": {
                     "file_key": {"type": "string"}, "file_name": {"type": "string"}}, "required": ["file_key"]}}}}})
async def upload(request: Request, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise validation({"file": "Attach a file."})
        name = f.filename or "upload.csv"
        data = await f.read()
        key = f"import_file/{ctx.gym.id}/{uuid7()}.{name.rsplit('.', 1)[-1].lower() if '.' in name else 'csv'}"
        if not name.lower().endswith((".csv", ".xls", ".xlsx")):
            raise ApiError(415, "FILE_TYPE")
        if len(data) > importer.MAX_BYTES:
            raise ApiError(413, "FILE_TOO_LARGE")
        storage.put_bytes(key, data)
    else:
        try:
            body = await request.json()
        except Exception:
            raise validation({"file_key": "Send a multipart file or {file_key}."})
        key = (body or {}).get("file_key")
        if not key or not key.startswith("import_file/"):
            raise validation({"file_key": "Upload with purpose import_file first."})
        name = body.get("file_name") or key.rsplit("/", 1)[-1]
        if not key.lower().endswith((".csv", ".xls", ".xlsx")) and not name.lower().endswith((".csv", ".xls", ".xlsx")):
            raise ApiError(415, "FILE_TYPE")
        try:
            data = storage.get_bytes(key)
        except Exception:
            raise validation({"file_key": "That upload wasn’t found."})
        if not name.lower().endswith((".csv", ".xls", ".xlsx")):
            name = key.rsplit("/", 1)[-1]
    headers, rows = importer.parse_file(data, name)  # sync: raises 413/415/422 NO_HEADER_ROW
    imp = Import(gym_id=ctx.gym.id, created_by=ctx.user.id, file_key=key, file_name=name, file_size=len(data),
                 status="parsing", counts={"rows": len(rows)})
    db.add(imp)
    db.flush()
    audit(db, ctx.gym.id, ctx.user.id, "import.upload", "import", imp.id, f"Uploaded {name} ({len(rows)} rows)")
    db.commit()
    enqueue("import.parse", importer.job_parse, imp.id, headers, rows)
    return {"import_id": imp.id, "status": "parsing", "rows": len(rows)}


@router.get("/gyms/{gymId}/imports", summary="Import history")
def history(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db),
            limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
    q = select(Import).where(Import.gym_id == ctx.gym.id)
    c = decode_cursor(cursor)
    if c:
        q = q.where(Import.id < c["id"])
    lim = clamp_limit(limit)
    rows = db.scalars(q.order_by(Import.id.desc()).limit(lim + 1)).all()
    nxt = encode_cursor({"id": rows[lim - 1].id}) if len(rows) > lim else None
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_([r.created_by for r in rows] or [""])))}
    return {"items": [{**ser_import(r), "columns": None, "created_by": {"id": r.created_by, "name": names.get(r.created_by)}}
                      for r in rows[:lim]], "next_cursor": nxt}


@router.get("/gyms/{gymId}/imports/{importId}", summary="Import status, counts and detected columns")
def get_import(importId: str, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    return ser_import(_get_import(db, ctx, importId))


def _require_preview(imp: Import):
    if imp.status != "previewed":
        raise ApiError(409, "NOT_ACTIVE", f"This import is {imp.status}; it can’t be changed now.")


@router.patch("/gyms/{gymId}/imports/{importId}/mapping", summary="Confirm / change column mapping (re-classifies rows)")
def patch_mapping(importId: str, body: MappingIn, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    _require_preview(imp)
    allowed = set(importer.FIELDS)
    mapping = dict(imp.mapping or {})
    conf = {c["header"]: c.get("confidence") for c in (imp.column_info or [])}
    for h, f in body.mapping.items():
        if h not in mapping:
            raise validation({h: "No such column in this file."})
        if f is not None and f not in allowed:
            raise validation({h: f"Unknown field. Use one of: {', '.join(sorted(allowed))}"})
        if f is not None:  # a field can only be mapped once
            for other, of in mapping.items():
                if other != h and of == f:
                    mapping[other] = None
        mapping[h] = f
        conf[h] = "high" if f else None
    imp.mapping = mapping
    imp.column_info = [{"header": h, "confidence": conf.get(h)} for h in imp.headers or []]
    if "name" not in mapping.values() or "phone" not in mapping.values():
        raise validation({"mapping": "Name and Mobile columns are required."})
    importer.classify(db, imp)
    db.commit()
    return {"counts": imp.counts, "columns": imp.column_info}


def _ser_row(r: ImportRow, existing: dict) -> dict:
    d = {"id": r.id, "row_number": r.row_number, "raw": r.raw, "normalized": r.normalized, "outcome": r.outcome,
         "issues": r.issues or [], "suggestion": r.suggestion, "member_id": r.member_id}
    if r.outcome == "existing" and r.member_id in existing:
        m = existing[r.member_id]
        d["existing_member"] = {"id": m.id, "name": m.display_name, "member_code": m.member_code}
    return d


@router.get("/gyms/{gymId}/imports/{importId}/rows", summary="Preview rows by outcome")
def rows(importId: str, outcome: Literal["ready", "existing", "fix", "skipped", "imported", "failed"] | None = None,
         limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
         ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    q = select(ImportRow).where(ImportRow.import_id == imp.id)
    if outcome:
        q = q.where(ImportRow.outcome == outcome)
    c = decode_cursor(cursor)
    if c:
        q = q.where(ImportRow.row_number > c["n"])
    lim = clamp_limit(limit)
    items = db.scalars(q.order_by(ImportRow.row_number).limit(lim + 1)).all()
    nxt = encode_cursor({"n": items[lim - 1].row_number}) if len(items) > lim else None
    items = items[:lim]
    existing = {m.id: m for m in db.scalars(select(GymMember).where(
        GymMember.id.in_([r.member_id for r in items if r.member_id] or [""])))}
    return {"items": [_ser_row(r, existing) for r in items], "next_cursor": nxt}


@router.patch("/gyms/{gymId}/imports/{importId}/rows/{rowId}", summary="Fix / skip a row (re-validated)")
def patch_row(importId: str, rowId: str, body: ImportRowPatch, ctx: GymCtx = Depends(gym_ctx),
              db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    _require_preview(imp)
    r = db.get(ImportRow, rowId)
    if not r or r.import_id != imp.id:
        raise ApiError(404, "NOT_FOUND", "That row wasn’t found.")
    data = body.model_dump(exclude_unset=True)
    ov = dict(r.overrides or {})
    if "phone" in data and data["phone"] is not None:
        ov["phone"] = normalize_phone(body.phone)
    if data.get("plan_id"):
        p = db.get(Plan, data["plan_id"])
        if not p or p.gym_id != ctx.gym.id:
            raise validation({"plan_id": "Pick a plan from this gym."})
        ov["plan_id"], ov["custom_plan"] = p.id, False
    for k in ("name", "paid_paise", "email", "skip", "custom_plan"):
        if k in data:
            ov[k] = data[k]
    for k in ("start_date", "end_date"):
        if k in data:
            ov[k] = data[k].isoformat() if data[k] else None
    r.overrides = ov
    importer.classify(db, imp)
    db.commit()
    db.refresh(r)
    existing = {r.member_id: db.get(GymMember, r.member_id)} if r.member_id else {}
    return {"row": _ser_row(r, existing), "counts": imp.counts}


@router.post("/gyms/{gymId}/imports/{importId}/commit", status_code=202, summary="Commit import (job; poll GET import)")
def commit(importId: str, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    _require_preview(imp)
    todo = db.scalar(select(func.count()).select_from(ImportRow).where(
        ImportRow.import_id == imp.id, ImportRow.outcome.in_(("ready", "existing"))))
    if not todo:
        raise validation({"rows": "There are no ready rows to import."})
    imp.status, imp.committed_at = "committing", utcnow()
    db.commit()
    enqueue("import.commit", importer.job_commit, imp.id)
    return {"status": "committing", "rows": todo}


@router.post("/gyms/{gymId}/imports/{importId}/cancel", summary="Cancel import (only before commit)")
def cancel(importId: str, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    if imp.status not in ("uploaded", "parsing", "previewed", "failed"):
        raise ApiError(409, "NOT_ACTIVE", "This import has already been committed.")
    imp.status = "cancelled"
    audit(db, ctx.gym.id, ctx.user.id, "import.cancel", "import", imp.id, f"Cancelled import of {imp.file_name}")
    db.commit()
    return {"status": "cancelled"}


@router.get("/gyms/{gymId}/imports/{importId}/report.csv", summary="Import report CSV",
            description="Every row with outcome + reason. `?only=failed` = original columns for fix/skipped/failed rows, "
                        "ready to correct and re-upload.", responses={200: {"content": {"text/csv": {}}}})
def report(importId: str, only: Literal["all", "failed"] = "all", ctx: GymCtx = Depends(gym_ctx),
           db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    q = select(ImportRow).where(ImportRow.import_id == imp.id)
    if only == "failed":
        q = q.where(ImportRow.outcome.in_(("fix", "skipped", "failed")))
    rows_ = db.scalars(q.order_by(ImportRow.row_number)).all()
    headers = imp.headers or []
    buf = io.StringIO()
    w = csv.writer(buf)
    if only == "failed":
        w.writerow(headers + ["Issue"])
    else:
        w.writerow(["Row", "Outcome", "Reason"] + headers)
    for r in rows_:
        reason = "; ".join(i["message"] for i in (r.issues or []))
        cells = [r.raw.get(h, "") for h in headers]
        w.writerow(cells + [reason] if only == "failed" else [r.row_number, r.outcome, reason] + cells)
    name = (imp.file_name or "import").rsplit(".", 1)[0]
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{only}-report.csv"'})


# ---------------- invites ----------------
INVITE_GAP = timedelta(days=7)


@router.post("/gyms/{gymId}/invites", status_code=202, summary="Send member-app invites (batch)",
             description="`member_ids` or `filter` (`not_on_app` | `import:{importId}`). Max 1 invite per member per 7 days. "
                         "Needs an approved gym (423 GYM_PENDING).")
def send_invites(body: InvitesIn, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    ctx.require_approved()
    gym = ctx.gym
    q = select(GymMember, User).join(User, User.id == GymMember.user_id).where(
        GymMember.gym_id == gym.id, GymMember.status == "active", GymMember.claim_disputed_at.is_(None))
    if body.member_ids:
        q = q.where(GymMember.id.in_(body.member_ids))
    elif body.filter and body.filter.startswith("import:"):
        q = q.where(GymMember.id.in_(select(ImportRow.member_id).where(
            ImportRow.import_id == body.filter.split(":", 1)[1], ImportRow.member_id.is_not(None))))
    elif body.filter != "not_on_app":
        raise validation({"filter": "Send member_ids or filter=not_on_app|import:{importId}."})
    targets = [(m, u) for m, u in db.execute(q).all() if u.first_login_at is None and not u.deleted_at]
    if body.filter == "not_on_app":
        states = load_states(db, gym, [m.id for m, _ in targets])
        targets = [(m, u) for m, u in targets if states[m.id].status in LIVE]
    recent = set(db.scalars(select(Invite.member_id).where(
        Invite.gym_id == gym.id, Invite.kind == "member_app", Invite.created_at >= utcnow() - INVITE_GAP,
        Invite.status != "failed")))
    batch = uuid7()
    queued, skipped = [], 0
    for m, u in targets:
        if m.id in recent:
            skipped += 1
            continue
        inv = Invite(gym_id=gym.id, member_id=m.id, kind="member_app", phone=u.phone, status="queued", batch_id=batch)
        db.add(inv)
        queued.append(inv)
    db.flush()
    audit(db, gym.id, ctx.user.id, "invite.batch", "invite_batch", batch,
          f"Sent {len(queued)} app invites ({skipped} skipped, invited in the last 7 days)")
    db.commit()
    for inv in queued:
        queue_invite(inv.id)
    return {"batch_id": batch, "queued": len(queued), "skipped_recent": skipped}


@router.post("/gyms/{gymId}/members/{memberId}/invite", status_code=202, summary="Invite one member to the app",
             description="429 INVITE_TOO_SOON {next_allowed_at} if invited in the last 7 days.")
def invite_one(memberId: str, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    ctx.require_approved()
    m = db.get(GymMember, memberId)
    if not m or m.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That member wasn’t found.")
    u = db.get(User, m.user_id)
    last = db.scalar(select(Invite).where(Invite.member_id == m.id, Invite.kind == "member_app",
                                          Invite.status != "failed").order_by(Invite.created_at.desc()).limit(1))
    if last and last.created_at > utcnow() - INVITE_GAP:
        nxt = last.created_at + INVITE_GAP
        raise ApiError(429, "INVITE_TOO_SOON", None, {"next_allowed_at": iso(nxt),
                                                       "retry_after_sec": int((nxt - utcnow()).total_seconds())})
    inv = Invite(gym_id=ctx.gym.id, member_id=m.id, kind="member_app", phone=u.phone, status="queued")
    db.add(inv)
    db.flush()
    audit(db, ctx.gym.id, ctx.user.id, "invite.send", "invite", inv.id, f"Sent app invite to {m.display_name}")
    db.commit()
    queue_invite(inv.id)
    return {"invite": {"id": inv.id, "status": inv.status, "phone": inv.phone, "created_at": iso(inv.created_at)}}
