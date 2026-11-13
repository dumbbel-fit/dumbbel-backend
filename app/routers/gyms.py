"""§7 — gym registration, documents, verification, profile, plans."""
import random
import re
import string

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, current_user, gym_ctx, owner_ctx, perm_ctx
from app.errors import ApiError
from app.models import Gym, GymDocument, GymStaff, Membership, Plan, User
from app.schemas import DocumentIn, GymCreate, GymPatch, PlanCreate, PlanPatch
from app.services.audit import audit, diff
from app.services.core import DEFAULT_SETTINGS, gym_settings, ser_gym, ser_plan
from app.utils import iso, normalize_phone, rupees, today_in, tz, utcnow
from app.responses import DocRoute, ERRORS, DocumentOut, GymListOut, GymOut, PlanListOut, PlanOut, VerificationOut

PLAN_EXTRAS = ["offer_price_paise", "joining_fee_paise", "gst_mode", "gst_rate", "facilities", "description",
               "freeze_allowed", "max_freeze_days", "cancellation_policy", "refund_policy"]
NOT_NULL_PLAN_FIELDS = {"name", "duration_days", "price_paise", "active", "sort_order", "joining_fee_paise", "gst_mode",
                        "gst_rate", "freeze_allowed"}

router = APIRouter(tags=["Gym setup"], route_class=DocRoute, responses=ERRORS)

GENERIC = {"fitness", "gym", "the", "club", "studio", "centre", "center", "and", "&", "health", "pvt", "ltd"}


def suggest_prefix(db: Session, name: str, area: str | None) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", f"{name} {area or ''}")]
    core = [w for w in words if w.lower() not in GENERIC] or words or ["GYM"]
    letters = "".join(w[0] for w in core).upper()
    pool = "".join(core).upper()
    candidates = []
    if len(letters) >= 3:
        candidates.append(letters[:3])
        candidates += [letters[0] + letters[1] + c for c in letters[2:]]
    base2 = (letters[:2] if len(letters) >= 2 else pool[:2]).ljust(2, "X")
    candidates += [base2 + c for c in pool[1:]] + [pool[:3].ljust(3, "X")]
    candidates += [base2 + c for c in string.ascii_uppercase]
    taken = set(db.scalars(select(Gym.code_prefix)).all())
    for c in candidates:
        if len(c) == 3 and c not in taken:
            return c
    while True:
        c = "".join(random.choices(string.ascii_uppercase, k=3))
        if c not in taken:
            return c


@router.post("/gyms", response_model=GymOut, status_code=201, summary="Register a gym (web register flow)",
             description="Any logged-in user. Creates the gym as `pending` and the owner staff row. "
                         "409 ALREADY_OWNER: one gym per owner phone in v1.")
def create_gym(body: GymCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if db.scalar(select(GymStaff).where(GymStaff.user_id == user.id, GymStaff.role == "owner").limit(1)):
        raise ApiError(409, "ALREADY_OWNER")
    prefix = body.code_prefix.upper() if body.code_prefix else suggest_prefix(db, body.name, body.area)
    if db.scalar(select(Gym.id).where(Gym.code_prefix == prefix)):
        raise ApiError(422, "VALIDATION", None, {"fields": {"code_prefix": "That code is taken."},
                                                  "suggestion": suggest_prefix(db, body.name, body.area)})
    if body.owner_name and not user.name:
        user.name = body.owner_name
    g = Gym(name=body.name, code_prefix=prefix, owner_user_id=user.id, email=body.email,
            phone=normalize_phone(body.phone) if body.phone else user.phone, address_line=body.address_line,
            area=body.area, city=body.city, state=body.state, pincode=body.pincode, status="pending",
            settings=dict(DEFAULT_SETTINGS), timezone="Asia/Kolkata")
    db.add(g)
    db.flush()
    g.org_id = g.id  # a gym is its own business until branches are added
    db.add(GymStaff(gym_id=g.id, user_id=user.id, role="owner", can_record_payments=True, status="active",
                    invited_at=utcnow(), joined_at=utcnow()))
    audit(db, g.id, user.id, "gym.create", "gym", g.id, f"Registered {g.name}")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "ALREADY_OWNER")
    return ser_gym(g)


@router.post("/gyms/{gymId}/documents", response_model=DocumentOut, status_code=201, summary="Attach a verification document (owner)")
def add_document(body: DocumentIn, ctx: GymCtx = Depends(perm_ctx("settings.manage")), db: Session = Depends(get_db)):
    if not body.file_key.startswith("gym_document/"):
        raise ApiError(422, "VALIDATION", None, {"fields": {"file_key": "Upload with purpose gym_document first."}})
    doc = GymDocument(gym_id=ctx.gym.id, type=body.type, file_key=body.file_key, status="submitted")
    db.add(doc)
    db.flush()
    audit(db, ctx.gym.id, ctx.user.id, "gym.document", "gym_document", doc.id, f"Uploaded {body.type.replace('_', ' ')}")
    db.commit()
    return {"id": doc.id, "type": doc.type, "status": doc.status, "created_at": iso(doc.created_at)}


@router.get("/gyms/{gymId}/verification", response_model=VerificationOut, summary="Verification progress (owner)")
def verification(ctx: GymCtx = Depends(perm_ctx("settings.manage")), db: Session = Depends(get_db)):
    g = ctx.gym
    docs = db.scalars(select(GymDocument).where(GymDocument.gym_id == g.id).order_by(GymDocument.created_at)).all()
    review_state = {"pending": "in_progress" if docs else "waiting", "active": "done",
                    "rejected": "rejected", "suspended": "suspended"}[g.status]
    return {"status": g.status,
            "steps": [{"key": "account", "done_at": iso(g.created_at)},
                      {"key": "documents", "done_at": iso(docs[-1].created_at) if docs else None,
                       "items": [{"id": d.id, "type": d.type, "status": d.status, "review_note": d.review_note}
                                 for d in docs]},
                      {"key": "review", "state": review_state, "done_at": iso(g.approved_at)}],
            "reason": g.status_reason if g.status in ("rejected", "suspended") else None}


@router.get("/gyms/{gymId}", response_model=GymOut, summary="Gym profile (any staff)")
def get_gym(ctx: GymCtx = Depends(gym_ctx)):
    d = ser_gym(ctx.gym)
    d["me"] = {"role": ctx.staff.role, "staff_role": ctx.role_name, "can_record_payments": ctx.has("payments.record"),
               "permissions": sorted(ctx.perms)}
    return d


@router.patch("/gyms/{gymId}", response_model=GymOut, summary="Update gym profile & settings (owner)")
def patch_gym(body: GymPatch, ctx: GymCtx = Depends(perm_ctx("settings.manage")), db: Session = Depends(get_db)):
    g = ctx.gym
    before = ser_gym(g)
    data = body.model_dump(exclude_unset=True)
    if "settings" in data:
        s = gym_settings(g)
        s.update({k: v for k, v in (data.pop("settings") or {}).items() if v is not None})
        g.settings = s
    if "logo_key" in data:
        g.logo_url = data.pop("logo_key")
    if "phone" in data:
        g.phone = normalize_phone(data.pop("phone")) if body.phone else None
    if "timezone" in data:
        tzname = data.pop("timezone")
        if tz(tzname).key != tzname:
            raise ApiError(422, "VALIDATION", None, {"fields": {"timezone": "Unknown timezone"}})
        g.timezone = tzname
    for k, v in data.items():
        setattr(g, k, v)
    b, a = diff(before, ser_gym(g))
    audit(db, g.id, ctx.user.id, "gym.update", "gym", g.id, "Updated gym profile", b, a)
    db.commit()
    return ser_gym(g)


# ---------------- plans ----------------
@router.get("/gyms/{gymId}/plans", response_model=PlanListOut, summary="List plans (any staff)")
def list_plans(include_inactive: bool = Query(False), ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    q = select(Plan).where(Plan.gym_id == ctx.gym.id)
    if not include_inactive:
        q = q.where(Plan.active.is_(True))
    plans = db.scalars(q.order_by(Plan.sort_order, Plan.duration_days, Plan.name)).all()
    today = today_in(ctx.gym.timezone)
    counts = dict(db.execute(select(Membership.plan_id, func.count(func.distinct(Membership.member_id)))
                             .where(Membership.gym_id == ctx.gym.id, Membership.state != "cancelled",
                                    Membership.start_date <= today, Membership.end_date >= today)
                             .group_by(Membership.plan_id)).all())
    return {"items": [ser_plan(p, int(counts.get(p.id, 0))) for p in plans]}


def _plan_conflict(db: Session, gym_id: str, name: str, exclude: str | None = None):
    q = select(Plan.id).where(Plan.gym_id == gym_id, Plan.active.is_(True), func.lower(Plan.name) == name.lower())
    if exclude:
        q = q.where(Plan.id != exclude)
    if db.scalar(q):
        raise ApiError(409, "PLAN_NAME_TAKEN")


@router.post("/gyms/{gymId}/plans", response_model=PlanOut, status_code=201, summary="Create plan (owner)")
def create_plan(body: PlanCreate, ctx: GymCtx = Depends(perm_ctx("plans.manage")), db: Session = Depends(get_db)):
    name = body.name.strip()
    _plan_conflict(db, ctx.gym.id, name)
    order = body.sort_order
    if order is None:
        order = (db.scalar(select(func.max(Plan.sort_order)).where(Plan.gym_id == ctx.gym.id)) or 0) + 1
    p = Plan(gym_id=ctx.gym.id, name=name, duration_days=body.duration_days, price_paise=body.price_paise,
             sort_order=order, active=True)
    for k in PLAN_EXTRAS:
        v = getattr(body, k)
        if v is not None:
            setattr(p, k, v)
    db.add(p)
    db.flush()
    audit(db, ctx.gym.id, ctx.user.id, "plan.create", "plan", p.id,
          f"Created plan {p.name} · {p.duration_days} days · {rupees(p.price_paise)}", None, ser_plan(p))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "PLAN_NAME_TAKEN")
    return ser_plan(p, 0)


@router.patch("/gyms/{gymId}/plans/{planId}", response_model=PlanOut, summary="Edit / disable plan (owner). Never touches existing memberships")
def patch_plan(planId: str, body: PlanPatch, ctx: GymCtx = Depends(perm_ctx("plans.manage")), db: Session = Depends(get_db)):
    p = db.get(Plan, planId)
    if not p or p.gym_id != ctx.gym.id:
        raise ApiError(404, "NOT_FOUND", "That plan wasn’t found.")
    before = ser_plan(p)
    data = body.model_dump(exclude_unset=True)
    new_name = (data.get("name") or p.name).strip()
    if data.get("active", p.active) and (new_name.lower() != p.name.lower() or (data.get("active") and not p.active)):
        _plan_conflict(db, ctx.gym.id, new_name, exclude=p.id)
    if "name" in data:
        data["name"] = new_name
    for k, v in data.items():
        if v is None and k in NOT_NULL_PLAN_FIELDS:
            continue
        setattr(p, k, v)
    b, a = diff(before, ser_plan(p))
    audit(db, ctx.gym.id, ctx.user.id, "plan.update", "plan", p.id, f"Updated plan {p.name}", b, a)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "PLAN_NAME_TAKEN")
    return ser_plan(p)


# ---------------- branches ----------------
class BranchCreate(GymCreate):
    copy_plans: bool = True
    copy_settings: bool = True


@router.get("/gyms/{gymId}/branches", response_model=GymListOut, summary="Branches of this business (any staff)",
            description="All gyms sharing this gym’s business (org). The frontend branch switcher uses the ids "
                        "from here or from `staff_of` at login (each branch is a separate gymId).")
def list_branches(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    org = ctx.gym.org_id or ctx.gym.id
    gyms = db.scalars(select(Gym).where(or_(Gym.org_id == org, Gym.id == org)).order_by(Gym.created_at)).all()
    mine = set(db.scalars(select(GymStaff.gym_id).where(GymStaff.user_id == ctx.user.id,
                                                        GymStaff.status != "deactivated")).all())
    return {"items": [{**ser_gym(g, full=False), "org_id": g.org_id, "has_access": g.id in mine} for g in gyms],
            "next_cursor": None}


@router.post("/gyms/{gymId}/branches", response_model=GymOut, status_code=201,
             summary="Add a branch (owner). Copies plans & settings by default",
             description="Creates a new gym under the same business, owned by the same owner. Starts `pending` "
                         "until the DUMBBEL team approves it, like any new gym.")
def create_branch(body: BranchCreate, ctx: GymCtx = Depends(owner_ctx), db: Session = Depends(get_db)):
    src = ctx.gym
    org = src.org_id or src.id
    if not src.org_id:
        src.org_id = org
    prefix = body.code_prefix.upper() if body.code_prefix else suggest_prefix(db, body.name, body.area)
    if db.scalar(select(Gym.id).where(Gym.code_prefix == prefix)):
        raise ApiError(422, "VALIDATION", None, {"fields": {"code_prefix": "That code is taken."},
                                                  "suggestion": suggest_prefix(db, body.name, body.area)})
    g = Gym(name=body.name, code_prefix=prefix, owner_user_id=src.owner_user_id, org_id=org,
            email=body.email or src.email, phone=normalize_phone(body.phone) if body.phone else src.phone,
            address_line=body.address_line, area=body.area, city=body.city or src.city, state=body.state or src.state,
            pincode=body.pincode, status="pending", timezone=src.timezone, gstin=src.gstin, logo_url=src.logo_url,
            settings=dict(gym_settings(src)) if body.copy_settings else dict(DEFAULT_SETTINGS))
    db.add(g)
    db.flush()
    db.add(GymStaff(gym_id=g.id, user_id=src.owner_user_id, role="owner", can_record_payments=True, status="active",
                    invited_at=utcnow(), joined_at=utcnow()))
    if body.copy_plans:
        for p in db.scalars(select(Plan).where(Plan.gym_id == src.id, Plan.active.is_(True))):
            np = Plan(gym_id=g.id, name=p.name, duration_days=p.duration_days, price_paise=p.price_paise,
                      sort_order=p.sort_order, active=True)
            for k in PLAN_EXTRAS:
                setattr(np, k, getattr(p, k))
            db.add(np)
    audit(db, src.id, ctx.user.id, "gym.branch_create", "gym", g.id, f"Added branch {g.name}")
    audit(db, g.id, ctx.user.id, "gym.create", "gym", g.id, f"Branch of {src.name}")
    db.commit()
    return ser_gym(g)
