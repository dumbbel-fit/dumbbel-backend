"""Member workout plans: a weekly plan built from the member's body, goal, experience, schedule, equipment and health.

  GET  /me/workout                      setup options, my answers, this week's plan (made automatically each week)
  PUT  /me/workout/profile              save my answers → a new plan for this week
  POST /me/workout/new                  a fresh plan with different exercises (same answers)
  POST /me/workout/sets                 tick sets done for an exercise
  POST /me/workout/days/{day}/done      mark a workout done (or not)
  GET  /me/workout/alternatives         other exercises for one slot (same movement, suits my equipment / health)
  POST /me/workout/swap                 swap an exercise (kept for the rest of the 4-week block)
  GET  /exercises/{id}                  one exercise: muscles, steps, coaching cues, media (when licensed)
The plan follows a 4-week block: same exercises, effort steps up weeks 1–3, week 4 is lighter; then a new block."""
import copy
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db import get_db
from app.deps import current_user
from app.errors import ApiError
from app.models import User, WorkoutPlan
from app.responses import DocRoute, ERRORS
from app.services import workout as W
from app.utils import iso, today_in, utcnow

router = APIRouter(tags=["Member workout plans"], route_class=DocRoute, responses=ERRORS)
TZ = "Asia/Kolkata"
FIELDS = ("height_cm", "weight_kg", "age", "goal", "experience", "days_per_week", "session_min", "place", "conditions")


class WorkoutProfileIn(BaseModel):
    height_cm: float = Field(..., ge=120, le=230)
    weight_kg: float = Field(..., ge=30, le=250)
    age: int = Field(..., ge=13, le=90)
    goal: Literal["lose_fat", "build_strength", "gain_muscle", "stay_fit", "improve_stamina"]
    experience: Literal["beginner", "intermediate", "advanced"]
    days_per_week: int = Field(..., ge=2, le=6)
    session_min: Literal[30, 45, 60, 75, 90]
    place: Literal["gym", "home", "bodyweight"]
    conditions: list[Literal["knee", "back", "shoulder", "high_bp", "heart", "diabetes", "asthma", "pregnant"]] = []
    gender: Literal["female", "male", "other"] | None = None


def _inputs(user: User) -> dict:
    p = dict(user.fitness_profile or {})
    if p.get("days_per_week") is None and p.get("workouts_per_week"):
        p["days_per_week"] = max(2, min(6, int(p["workouts_per_week"])))
    return {k: p.get(k) for k in FIELDS} | {"conditions": p.get("conditions") or []}


def _hash(inp: dict) -> str:
    return hashlib.sha256(json.dumps(inp, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _options() -> dict:
    return {"goals": [{"value": k, "label": v} for k, v in W.GOALS.items()],
            "levels": [{"value": "beginner", "label": "Beginner", "hint": "New to the gym, or back after a long break"},
                       {"value": "intermediate", "label": "Intermediate", "hint": "Training regularly for 6+ months"},
                       {"value": "advanced", "label": "Advanced", "hint": "2+ years, comfortable with barbell lifts"}],
            "places": [{"value": k, "label": v} for k, v in W.PLACE_LABEL.items()],
            "conditions": [{"value": k, "label": v} for k, v in W.CONDITIONS.items()],
            "days_per_week": [2, 3, 4, 5, 6], "session_min": [30, 45, 60, 75, 90]}


def _make(db: Session, user: User, inp: dict, week_start, block_no=1, week_in_block=1, variant=0, keep=None) -> WorkoutPlan:
    plan = W.build_plan(inp, user.id, week_start, block_no, week_in_block, keep=keep, variant=variant)
    row = db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user.id, WorkoutPlan.week_start == week_start))
    if row is None:
        row = WorkoutPlan(user_id=user.id, week_start=week_start)
        db.add(row)
    row.block_no, row.week_in_block, row.variant = block_no, week_in_block, variant
    row.inputs, row.plan = {**inp, "_hash": _hash(inp)}, plan
    row.progress = {"days": {}}
    db.commit()
    return row


def current_plan(db: Session, user: User) -> WorkoutPlan | None:
    """This week's plan; made from last week's (next week of the block, or a new block) the first time it's asked for."""
    inp = _inputs(user)
    if W.missing_inputs(inp):
        return None
    ws = W.week_start_of(today_in(TZ))
    row = db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user.id, WorkoutPlan.week_start == ws))
    if row:
        return row
    prev = db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user.id, WorkoutPlan.week_start < ws)
                     .order_by(WorkoutPlan.week_start.desc()).limit(1))
    if prev and (prev.inputs or {}).get("_hash") == _hash(inp) and not (prev.plan or {}).get("blocked"):
        if prev.week_in_block < 4:
            return _make(db, user, inp, ws, prev.block_no, prev.week_in_block + 1, prev.variant, keep=W.keep_map(prev.plan))
        return _make(db, user, inp, ws, prev.block_no + 1, 1, 0)
    return _make(db, user, inp, ws, (prev.block_no + 1) if prev else 1, 1, 0)


def _out(user: User, row: WorkoutPlan | None) -> dict:
    inp = _inputs(user)
    plan = copy.deepcopy(row.plan) if row else None
    prog = (row.progress or {}).get("days", {}) if row else {}
    if plan and not plan.get("blocked"):
        today = today_in(TZ).isoformat()
        for d in plan["days"]:
            first = (d.get("exercises") or [{}])[0]
            for w in d.get("warmup") or []:  # "First exercise, light" shows that day's first exercise
                if not w.get("id") and w.get("name") == "First exercise, light" and first.get("id"):
                    w.update({"id": first["id"], "of": first.get("name")})
            for e in (d.get("exercises") or []) + (d.get("cooldown") or []) + (d.get("warmup") or []) + ([d["finisher"]] if d.get("finisher") else []):
                if "gif_url" in e or e.get("id"):
                    e.update(W.media_for(e.get("id")))
            p = prog.get(str(d["index"])) or {}
            d["is_today"] = d["date"] == today
            d["done"] = bool(p.get("done"))
            d["done_at"] = p.get("at")
            for e in d.get("exercises") or []:
                e["sets_done"] = int((p.get("sets") or {}).get(e["slot"], 0))
        work = [d for d in plan["days"] if not d["rest"]]
        plan["progress"] = {"done": sum(1 for d in work if d["done"]), "planned": len(work)}
    return {"profile": inp, "missing": W.missing_inputs(inp), "options": _options(), "plan": plan,
            "media": {"enabled": bool(W.settings.exercise_media_base), "attribution": W.MEDIA_ATTRIBUTION if W.settings.exercise_media_base else None},
            "gender": user.gender}


def _day(plan: dict, day: int) -> dict:
    d = next((x for x in plan.get("days", []) if x["index"] == day), None)
    if not d or d.get("rest"):
        raise ApiError(404, "NOT_FOUND", "That day has no workout.")
    return d


@router.get("/me/workout", summary="My workout: setup options, my answers and this week's plan (made automatically each week)")
def my_workout(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _out(user, current_plan(db, user))


@router.put("/me/workout/profile", summary="Save my answers (body, goal, experience, schedule, equipment, health) → new plan this week")
def save_profile(body: WorkoutProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = dict(user.fitness_profile or {})
    data = body.model_dump()
    gender = data.pop("gender", None)
    p.update(data)
    p["workouts_per_week"] = body.days_per_week
    user.fitness_profile = p
    flag_modified(user, "fitness_profile")
    user.weekly_goal = body.days_per_week
    if gender and not user.gender:
        user.gender = gender
    db.commit()
    ws = W.week_start_of(today_in(TZ))
    prev = db.scalar(select(WorkoutPlan).where(WorkoutPlan.user_id == user.id).order_by(WorkoutPlan.week_start.desc()).limit(1))
    row = _make(db, user, _inputs(user), ws, (prev.block_no + 1) if prev else 1, 1, 0)
    return _out(user, row)


@router.post("/me/workout/new", summary="A fresh plan with different exercises (same answers); starts a new 4-week block")
def new_plan(user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = current_plan(db, user)
    if not row:
        raise ApiError(409, "SETUP_NEEDED", "Answer a few questions first so we can build your plan.")
    row = _make(db, user, _inputs(user), row.week_start, row.block_no, 1, row.variant + 1)
    return _out(user, row)


class SetsIn(BaseModel):
    day: int = Field(..., ge=0, le=6, description="0 = Monday … 6 = Sunday")
    slot: str
    sets_done: int = Field(..., ge=0, le=10)


@router.post("/me/workout/sets", summary="Tick sets done for one exercise")
def log_sets(body: SetsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = current_plan(db, user)
    if not row:
        raise ApiError(409, "SETUP_NEEDED", "Set up your plan first.")
    d = _day(row.plan, body.day)
    ex = next((e for e in d["exercises"] if e["slot"] == body.slot), None)
    if not ex:
        raise ApiError(404, "NOT_FOUND", "That exercise isn't in this workout.")
    prog = copy.deepcopy(row.progress or {"days": {}})
    dp = prog["days"].setdefault(str(body.day), {})
    dp.setdefault("sets", {})[body.slot] = min(body.sets_done, ex["dose"]["sets"])
    if all(dp["sets"].get(e["slot"], 0) >= e["dose"]["sets"] for e in d["exercises"]) and not dp.get("done"):
        dp["done"], dp["at"] = True, iso(utcnow())
    row.progress = prog
    db.commit()
    return _out(user, row)


class DoneIn(BaseModel):
    done: bool = True


@router.post("/me/workout/days/{day}/done", summary="Mark a workout done (or undo)")
def mark_done(day: int, body: DoneIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = current_plan(db, user)
    if not row:
        raise ApiError(409, "SETUP_NEEDED", "Set up your plan first.")
    _day(row.plan, day)
    prog = copy.deepcopy(row.progress or {"days": {}})
    dp = prog["days"].setdefault(str(day), {})
    dp["done"], dp["at"] = body.done, iso(utcnow()) if body.done else None
    row.progress = prog
    db.commit()
    return _out(user, row)


@router.get("/me/workout/alternatives", summary="Other exercises for one slot: same movement, suits my equipment and health")
def alternatives(day: int = Query(..., ge=0, le=6), slot: str = Query(...), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    row = current_plan(db, user)
    if not row:
        raise ApiError(409, "SETUP_NEEDED", "Set up your plan first.")
    ex = next((e for e in _day(row.plan, day)["exercises"] if e["slot"] == slot), None)
    if not ex:
        raise ApiError(404, "NOT_FOUND", "That exercise isn't in this workout.")
    return {"items": W.alternatives(_inputs(user), ex["pattern"], ex["id"])}


class SwapIn(BaseModel):
    day: int = Field(..., ge=0, le=6)
    slot: str
    exercise_id: str = Field(..., pattern=r"^\d{4}$")


@router.post("/me/workout/swap", summary="Swap an exercise for one of its alternatives (kept for the rest of the 4-week block)")
def swap(body: SwapIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = current_plan(db, user)
    if not row:
        raise ApiError(409, "SETUP_NEEDED", "Set up your plan first.")
    plan = copy.deepcopy(row.plan)
    d = _day(plan, body.day)
    ex = next((e for e in d["exercises"] if e["slot"] == body.slot), None)
    if not ex:
        raise ApiError(404, "NOT_FOUND", "That exercise isn't in this workout.")
    allowed = {a["id"] for a in W.alternatives(_inputs(user), ex["pattern"], ex["id"])}
    if body.exercise_id not in allowed:
        raise ApiError(422, "VALIDATION", "Pick one of the suggested alternatives.")
    lib = W.BY_ID[body.exercise_id]
    new = {"slot": ex["slot"], **W.exercise_view(body.exercise_id, lib), "mode": lib["mode"], "dose": ex["dose"]}
    d["exercises"] = [new if e["slot"] == body.slot else e for e in d["exercises"]]
    row.plan = plan
    prog = copy.deepcopy(row.progress or {"days": {}})
    (prog["days"].get(str(body.day)) or {}).get("sets", {}).pop(body.slot, None)
    row.progress = prog
    db.commit()
    return _out(user, row)


@router.get("/exercises/{exerciseId}", summary="One exercise: target and other muscles, equipment, step-by-step, coaching cues, media")
def exercise(exerciseId: str, user: User = Depends(current_user)):
    if exerciseId not in W.dataset():
        raise ApiError(404, "NOT_FOUND", "Exercise not found.")
    return W.exercise_view(exerciseId)
