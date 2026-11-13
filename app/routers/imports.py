"""§11 — member import (4 steps)."""
import csv
import io
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, gym_ctx, perm_ctx
from app.errors import ApiError, validation
from app.models import GymMember, Import, ImportRow, Plan, User
from app.schemas import ImportRowPatch, MappingIn
from app.services import importer, storage
from app.services.audit import audit
from app.services.jobs import enqueue
from app.responses import DocRoute, ERRORS, ImportListOut, ImportOut, ImportRowPatchOut, ImportRowsOut, ImportUploadOut, MappingOut
from app.services.rules import LIVE, load_states
from app.utils import clamp_limit, decode_cursor, encode_cursor, iso, normalize_phone, utcnow, uuid7

router = APIRouter(tags=["Import"], route_class=DocRoute, responses=ERRORS)
public = APIRouter(tags=["Import"], route_class=DocRoute, responses=ERRORS)

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
    c = imp.counts or {}
    total = c.get("to_commit") if imp.status in ("committing", "completed") else c.get("rows")
    processed = c.get("processed", 0) if imp.status in ("committing", "completed") else (
        c.get("rows", 0) if imp.status not in ("uploaded", "parsing") else 0)
    progress = 1.0 if imp.status in ("completed", "previewed") else (
        round(processed / total, 3) if total else 0.0)
    return {"id": imp.id, "status": imp.status, "file_name": imp.file_name, "file_size": imp.file_size,
            "counts": c, "progress": progress, "processed": processed, "total": total,
            "columns": imp.column_info if imp.status not in ("uploaded", "parsing") else [],
            "detected_columns": imp.column_info if imp.status not in ("uploaded", "parsing") else [],
            "mapping": imp.mapping, "error": imp.error, "created_at": iso(imp.created_at),
            "committed_at": iso(imp.committed_at), "completed_at": iso(imp.completed_at)}


@router.post("/gyms/{gymId}/imports", response_model=ImportUploadOut, status_code=202, summary="Upload member file (multipart `file`, or JSON {file_key})",
             description="CSV / XLS / XLSX, max 10 MB / 5,000 rows. Parsing, auto-mapping and classification run as a job; "
                         "poll GET import until status is `previewed`.",
             openapi_extra={"requestBody": {"content": {
                 "multipart/form-data": {"schema": {"type": "object", "properties": {
                     "file": {"type": "string", "format": "binary"}}, "required": ["file"]}},
                 "application/json": {"schema": {"type": "object", "properties": {
                     "file_key": {"type": "string"}, "file_name": {"type": "string"}}, "required": ["file_key"]}}}}})
async def upload(request: Request, ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
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


@router.get("/gyms/{gymId}/imports", response_model=ImportListOut, summary="Import history")
def history(ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db),
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


@router.get("/gyms/{gymId}/imports/{importId}", response_model=ImportOut, summary="Import status, counts and detected columns")
def get_import(importId: str, ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
    return ser_import(_get_import(db, ctx, importId))


def _require_preview(imp: Import):
    if imp.status != "previewed":
        raise ApiError(409, "NOT_ACTIVE", f"This import is {imp.status}; it can’t be changed now.")


@router.patch("/gyms/{gymId}/imports/{importId}/mapping", response_model=MappingOut, summary="Confirm / change column mapping (re-classifies rows)")
def patch_mapping(importId: str, body: MappingIn, ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
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


@router.get("/gyms/{gymId}/imports/{importId}/rows", response_model=ImportRowsOut, summary="Preview rows by outcome")
def rows(importId: str, outcome: Literal["ready", "existing", "fix", "skipped", "imported", "failed"] | None = None,
         limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
         ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
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


@router.patch("/gyms/{gymId}/imports/{importId}/rows/{rowId}", response_model=ImportRowPatchOut, summary="Fix / skip a row (re-validated)")
def patch_row(importId: str, rowId: str, body: ImportRowPatch, ctx: GymCtx = Depends(perm_ctx("imports.manage")),
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


STALE_COMMIT = timedelta(minutes=10)


def _start_commit(db: Session, imp: Import) -> int:
    todo = db.scalar(select(func.count()).select_from(ImportRow).where(
        ImportRow.import_id == imp.id, ImportRow.outcome.in_(("ready", "existing"))))
    if not todo:
        raise validation({"rows": "There are no ready rows to import."})
    c = dict(imp.counts or {})
    c.update(to_commit=todo, processed=0, imported_before=c.get("imported", 0) if imp.status == "completed" else 0)
    imp.counts = c
    imp.status, imp.committed_at = "committing", utcnow()
    db.commit()
    enqueue("import.commit", importer.job_commit, imp.id)
    return todo


@router.post("/gyms/{gymId}/imports/{importId}/commit", response_model=ImportOut, status_code=202,
             summary="Commit import (job; poll GET import for progress)",
             description="Safe to call again: rows already imported are never imported twice. If a commit got stuck "
                         "(no progress for 10 min) calling this resumes it. After completion, rows you fixed later "
                         "can be committed by calling it again.")
def commit(importId: str, ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    if imp.status == "committing":
        if imp.updated_at and imp.updated_at > utcnow() - STALE_COMMIT:
            return ser_import(imp)  # already running: idempotent
    elif imp.status not in ("previewed", "completed"):
        raise ApiError(409, "NOT_ACTIVE", f"This import is {imp.status}; it can’t be committed.")
    _start_commit(db, imp)
    return ser_import(imp)


@router.post("/gyms/{gymId}/imports/{importId}/retry-failed", response_model=ImportOut, status_code=202,
             summary="Re-validate failed rows and commit the ones that are now OK",
             description="Rows that failed during commit are re-checked against current data; rows that pass are "
                         "committed (job). Rows still blocked move to `fix` so you can correct them.")
def retry_failed(importId: str, ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    if imp.status != "completed":
        raise ApiError(409, "NOT_ACTIVE", "Retry is available once the import has completed.")
    failed = db.scalars(select(ImportRow).where(ImportRow.import_id == imp.id, ImportRow.outcome == "failed")).all()
    if not failed:
        raise validation({"rows": "There are no failed rows."})
    for r in failed:
        r.outcome = "fix"
        r.issues = [i for i in (r.issues or []) if i.get("code") != "COMMIT_FAILED"]
    db.flush()
    importer.classify(db, imp)
    db.flush()
    _start_commit(db, imp)
    return ser_import(imp)


@router.post("/gyms/{gymId}/imports/{importId}/cancel", response_model=ImportOut, summary="Cancel import (only before commit)")
def cancel(importId: str, ctx: GymCtx = Depends(perm_ctx("imports.manage")), db: Session = Depends(get_db)):
    imp = _get_import(db, ctx, importId)
    if imp.status not in ("uploaded", "parsing", "previewed", "failed"):
        raise ApiError(409, "NOT_ACTIVE", "This import has already been committed.")
    imp.status = "cancelled"
    audit(db, ctx.gym.id, ctx.user.id, "import.cancel", "import", imp.id, f"Cancelled import of {imp.file_name}")
    db.commit()
    return ser_import(imp)


@router.get("/gyms/{gymId}/imports/{importId}/report.csv", summary="Import report CSV",
            description="Every row with outcome + reason. `?only=failed` = original columns for fix/skipped/failed rows, "
                        "ready to correct and re-upload.", responses={200: {"content": {"text/csv": {}}}})
def report(importId: str, only: Literal["all", "failed"] = "all", ctx: GymCtx = Depends(perm_ctx("imports.manage")),
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
