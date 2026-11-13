"""Member engagement & AI (v1.4): crowd meter, XP/levels/badges, leaderboard, nutrition + food log, AI coach,
daily insight; owner crowd view and AI briefing."""
import datetime as dt
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, current_user, gym_ctx
from app.errors import ApiError, validation
from app.models import Checkin, FoodLog, Gym, GymMember, User
from app.responses import DocRoute, ERRORS, Out
from app.services import ai
from app.services.cache import cache, rate_limit
from app.services.crowd import gym_crowd
from app.services.nutrition import estimate_food, targets
from app.services.progress import BADGES, badges_for, leaderboard, level_for, member_metrics, xp_from
from app.services.rules import load_states, week_start
from app.utils import first_name, iso, today_in, utcnow

me = APIRouter(prefix="/me", tags=["Member engagement & AI"], route_class=DocRoute, responses=ERRORS)
gym = APIRouter(tags=["Member engagement & AI"], route_class=DocRoute, responses=ERRORS)
IST = "Asia/Kolkata"
MEALS = ("breakfast", "lunch", "snack", "dinner")


def _my_member(db: Session, user: User, member_id: str | None) -> tuple[GymMember, Gym]:
    rows = db.execute(select(GymMember, Gym).join(Gym, Gym.id == GymMember.gym_id).where(
        GymMember.user_id == user.id, GymMember.claim_disputed_at.is_(None), GymMember.status == "active")).all()
    if member_id:
        rows = [(m, g) for m, g in rows if m.id == member_id]
    if not rows:
        raise ApiError(404, "NOT_FOUND", "No gym membership found.")
    return rows[0]


# ---------------- crowd ----------------
@me.get("/crowd", summary="How crowded is my gym right now? (+ today’s forecast, best times)",
        description="Estimated from check-ins: members who checked in during the last ~75 min. `level`: quiet · "
                    "moderate · busy · packed · closed · unknown (new gym, not enough history). `today[]` = usual "
                    "crowd per half hour for this weekday; `best_times` = quietest upcoming slots.")
def my_crowd(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _, g = _my_member(db, user, member_id)
    key = f"crowd:{g.id}"
    hit = cache.get(key)
    if hit:
        import json
        return json.loads(hit)
    out = gym_crowd(db, g)
    import json
    cache.set(key, json.dumps(out), ex=60)
    return out


@gym.get("/gyms/{gymId}/crowd", summary="Live crowd at this branch (any staff)",
         description="Same estimate members see, plus `capacity` (set it in gym settings `capacity` for an accurate %).")
def gym_crowd_view(ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    return gym_crowd(db, ctx.gym)


# ---------------- progress / badges / leaderboard ----------------
MILESTONES = [3, 7, 14, 30]


@me.get("/progress", summary="My XP, level, badges, streak milestones and rank at this gym")
def my_progress(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m, g = _my_member(db, user, member_id)
    met = member_metrics(db, m, g, user.weekly_goal)
    xp = xp_from(met)
    today = today_in(g.timezone)
    days = met["_days"]
    ws = week_start(today)
    lb = leaderboard(db, g, m, "month", limit=3)
    nxt = next((t for t in MILESTONES if t > met["streak"]), None)
    return {"member_id": m.id, "gym": {"id": g.id, "name": g.name}, **level_for(xp),
            "streak": met["streak"], "best_streak": met["best_streak"], "visits": met["visits"],
            "week": {"visits": sum(1 for d in days if ws <= d <= today), "goal": user.weekly_goal,
                     "days": [1 if (ws + timedelta(days=i)) in set(days) else 0 for i in range(7)]},
            "month_visits": sum(1 for d in days if d.year == today.year and d.month == today.month),
            "milestones": [{"days": t, "label": f"{t}-day streak", "done": met["best_streak"] >= t,
                            "hint": f"{t - met['streak']} to go" if t == nxt else None} for t in MILESTONES],
            "badges": badges_for(met, dict(m.badges or {})),
            "earned_count": sum(1 for b in badges_for(met, {}) if b["earned"]), "badge_total": len(BADGES),
            "rank": lb["me"], "participants": lb["participants"],
            "xp_rules": "10 XP per gym day · +5 for each day in a streak of 3+ · +50 per week you hit your goal · "
                        "+25 for coming back after 2+ weeks"}


@me.get("/leaderboard", summary="This month’s (or week’s) most consistent members at my gym",
        description="Ranked by gym days. Names show as “Ananya R.”; hide yourself with PATCH /me {show_on_leaderboard:false}.")
def my_leaderboard(member_id: str | None = None, period: Literal["month", "week"] = "month",
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    m, g = _my_member(db, user, member_id)
    return leaderboard(db, g, m, period)


# ---------------- fitness profile & nutrition ----------------
class FitnessIn(BaseModel):
    age: int | None = Field(None, ge=12, le=100)
    height_cm: float | None = Field(None, ge=100, le=230)
    weight_kg: float | None = Field(None, ge=25, le=250)
    goal: Literal["lose_fat", "build_strength", "gain_muscle", "stay_fit", "improve_stamina"] | None = None
    diet: Literal["veg", "egg", "nonveg", "vegan"] | None = None
    activity: Literal["low", "moderate", "high"] | None = None
    workouts_per_week: int | None = Field(None, ge=0, le=14)
    allergies: list[str] | None = None
    likes: list[str] | None = None
    typical_day: Literal["morning", "evening", "flex"] | None = None


@me.get("/fitness", summary="My fitness & diet profile + computed daily targets")
def get_fitness(user: User = Depends(current_user)):
    p = user.fitness_profile or {}
    return {"profile": p, "targets": targets(p, user.gender, user.dob), "gender": user.gender}


@me.put("/fitness", summary="Save my fitness & diet profile (targets recalculate)")
def put_fitness(body: FitnessIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = dict(user.fitness_profile or {})
    p.update({k: v for k, v in body.model_dump(exclude_unset=True).items()})
    p["allergies"] = [a for a in (p.get("allergies") or []) if a and a.lower() != "none"][:12]
    p["likes"] = (p.get("likes") or [])[:20]
    user.fitness_profile = p
    if body.workouts_per_week:
        user.weekly_goal = max(2, min(6, body.workouts_per_week))
    db.commit()
    return {"profile": p, "targets": targets(p, user.gender, user.dob)}


def ser_log(f: FoodLog) -> dict:
    return {"id": f.id, "date": f.local_date.isoformat(), "meal": f.meal, "text": f.text, "items": f.items or [],
            "kcal": f.kcal, "protein_g": float(f.protein_g), "carbs_g": float(f.carbs_g), "fat_g": float(f.fat_g),
            "water_ml": f.water_ml, "source": f.source, "created_at": iso(f.created_at)}


def day_summary(db: Session, user: User, d: date) -> dict:
    logs = db.scalars(select(FoodLog).where(FoodLog.user_id == user.id, FoodLog.local_date == d)
                      .order_by(FoodLog.created_at)).all()
    tot = {"kcal": sum(f.kcal for f in logs), "protein_g": round(sum(float(f.protein_g) for f in logs), 1),
           "carbs_g": round(sum(float(f.carbs_g) for f in logs), 1), "fat_g": round(sum(float(f.fat_g) for f in logs), 1),
           "water_l": round(sum(f.water_ml for f in logs) / 1000, 2)}
    return {"logs": logs, "totals": tot}


@me.get("/nutrition", summary="Today’s targets, totals, food log by meal and the week’s protein")
def my_nutrition(day: date | None = Query(None, alias="date"), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    d = day or today_in(IST)
    t = targets(user.fitness_profile, user.gender, user.dob)
    s = day_summary(db, user, d)
    meals = {k: [ser_log(f) for f in s["logs"] if f.meal == k] for k in MEALS}
    ws = week_start(d)
    rows = db.execute(select(FoodLog.local_date, func.sum(FoodLog.protein_g), func.sum(FoodLog.kcal),
                             func.count()).where(FoodLog.user_id == user.id, FoodLog.local_date >= ws,
                                                 FoodLog.local_date < ws + timedelta(days=7), FoodLog.meal != "water")
                      .group_by(FoodLog.local_date)).all()
    per = {r[0]: (float(r[1] or 0), int(r[2] or 0)) for r in rows}
    week = [{"date": (ws + timedelta(days=i)).isoformat(), "label": "MTWTFSS"[i],
             "protein_g": round(per.get(ws + timedelta(days=i), (0, 0))[0]), "kcal": per.get(ws + timedelta(days=i), (0, 0))[1]}
            for i in range(7)]
    logged = [w for w in week if w["kcal"] > 0]
    avg_p = round(sum(w["protein_g"] for w in logged) / len(logged)) if logged else None
    summary = (f"Protein averaged {avg_p} g on the {len(logged)} day{'s' if len(logged) != 1 else ''} you logged — "
               f"about {round(avg_p / t['protein_g'] * 100)}% of your target.") if logged else "Log a meal to see your week."
    return {"date": d.isoformat(), "targets": t, "totals": s["totals"], "meals": meals, "week": week,
            "week_summary": summary, "ai_enabled": ai.enabled(),
            "disclaimer": "General wellness guidance, not medical advice. Talk to a doctor or dietitian about health conditions."}


class FoodIn(BaseModel):
    meal: Literal["breakfast", "lunch", "snack", "dinner"]
    text: str = Field(..., min_length=2, max_length=300, examples=["2 egg dosa and a bowl of curd"])
    on: dt.date | None = Field(None, alias="date")
    kcal: int | None = Field(None, ge=0, le=5000)
    protein_g: float | None = Field(None, ge=0, le=400)
    carbs_g: float | None = Field(None, ge=0, le=800)
    fat_g: float | None = Field(None, ge=0, le=400)


@me.post("/food-logs", status_code=201, summary="Log food — type it in plain words; AI estimates the macros",
         description="Send `text` (e.g. “2 egg dosa and a bowl of curd”). If you don’t send kcal/protein, the AI "
                     "estimates them (`source: ai`). 503 AI_UNAVAILABLE → ask for numbers manually.")
def add_food(body: FoodIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"food:{user.id}", 40, 3600)
    d = body.on or today_in(IST)
    if body.kcal is not None:
        est = {"items": [], "kcal": body.kcal, "protein_g": body.protein_g or 0, "carbs_g": body.carbs_g or 0,
               "fat_g": body.fat_g or 0}
        source = "manual"
    else:
        try:
            est = estimate_food(body.text, user.fitness_profile)
        except ai.AiUnavailable:
            raise ApiError(503, "AI_UNAVAILABLE", "We couldn’t estimate that right now. Enter the calories yourself.")
        source = "ai"
    f = FoodLog(user_id=user.id, local_date=d, meal=body.meal, text=body.text.strip(), items=est["items"],
                kcal=est["kcal"], protein_g=est["protein_g"], carbs_g=est["carbs_g"], fat_g=est["fat_g"], source=source)
    db.add(f)
    db.commit()
    cache.delete(f"insight:{user.id}:{d.isoformat()}")
    return ser_log(f)


@me.delete("/food-logs/{logId}", status_code=204, summary="Remove a food or water entry")
def delete_food(logId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = db.get(FoodLog, logId)
    if not f or f.user_id != user.id:
        raise ApiError(404, "NOT_FOUND", "That entry wasn’t found.")
    db.delete(f)
    db.commit()
    return Response(status_code=204)


class WaterIn(BaseModel):
    ml: int = Field(..., ge=50, le=3000, examples=[250])
    on: dt.date | None = Field(None, alias="date")


@me.post("/water", status_code=201, summary="Add water (ml)")
def add_water(body: WaterIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = body.on or today_in(IST)
    f = FoodLog(user_id=user.id, local_date=d, meal="water", text=f"{body.ml} ml water", water_ml=body.ml)
    db.add(f)
    db.commit()
    return ser_log(f)


# ---------------- AI coach & daily insight ----------------
def _context(db: Session, user: User) -> tuple[str, list[str]]:
    p = user.fitness_profile or {}
    t = targets(p, user.gender, user.dob)
    d = today_in(IST)
    s = day_summary(db, user, d)
    food = "; ".join(f"{f.meal}: {f.text} ({f.kcal} kcal, {float(f.protein_g):g} g protein)" for f in s["logs"]
                     if f.meal != "water") or "nothing logged yet"
    gm = db.scalar(select(GymMember).where(GymMember.user_id == user.id, GymMember.status == "active").limit(1))
    trained = None
    if gm:
        last = db.scalar(select(Checkin.at).where(Checkin.member_id == gm.id, Checkin.result == "success")
                         .order_by(Checkin.at.desc()).limit(1))
        if last:
            hrs = (utcnow() - last).total_seconds() / 3600
            trained = "trained today" if hrs < 18 else f"last trained {int(hrs // 24)} day(s) ago"
    based = ["your profile" if t["profile_complete"] else "default targets (profile incomplete)", "today’s food log"]
    if trained:
        based.append("your gym visits")
    ctx = (f"Member: {first_name(user.name) or 'there'}; {t['basis']}; diet {p.get('diet') or 'not set'}; "
           f"allergies {', '.join(p.get('allergies') or []) or 'none'}; likes {', '.join(p.get('likes') or []) or 'not set'}; "
           f"gym time {p.get('typical_day') or 'not set'}. Daily targets: {t['kcal']} kcal, {t['protein_g']} g protein, "
           f"{t['carbs_g']} g carbs, {t['fat_g']} g fat, {t['water_l']} L water. Today so far ({d.isoformat()}): "
           f"{s['totals']['kcal']} kcal, {s['totals']['protein_g']} g protein, {s['totals']['water_l']} L water. "
           f"Logged: {food}. Training: {trained or 'unknown'}.")
    return ctx, based


COACH_SYSTEM = ("You are DUMBBEL.fit's nutrition & training assistant for gym members in India. Give practical, specific "
                "advice using everyday Indian foods with rough prices in ₹ when helpful. Respect the member's diet type "
                "and NEVER suggest foods they are allergic to. Keep answers under 110 words, friendly, no markdown "
                "headings, at most 4 short bullet lines. You give general wellness guidance, not medical advice: for "
                "medical conditions, injuries, pregnancy or eating disorders, suggest a doctor or dietitian. If asked "
                "about something unrelated to fitness, food or health, briefly steer back.")


class CoachIn(BaseModel):
    messages: list[dict] = Field(..., description="[{role: user|assistant, content}] — last 12 kept")


@me.post("/coach", summary="AI nutrition & training assistant (Groq)",
         description="Send the conversation; the assistant answers using your profile, targets, today’s log and gym "
                     "visits. Returns `{reply, based_on[]}`. 503 AI_UNAVAILABLE when the AI service is down.")
def coach(body: CoachIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"coach:{user.id}", 30, 3600)
    msgs = [{"role": m.get("role"), "content": str(m.get("content", ""))[:800]} for m in body.messages[-12:]
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        raise validation({"messages": "End with a user message."})
    ctx, based = _context(db, user)
    try:
        reply = ai.chat([{"role": "system", "content": COACH_SYSTEM + "\nContext: " + ctx}, *msgs], max_tokens=900)
    except ai.AiUnavailable:
        raise ApiError(503, "AI_UNAVAILABLE", "The assistant is taking a break. Your targets and food log still work.")
    return {"reply": reply, "based_on": based}


@me.get("/insight", summary="Today’s one-line AI insight for the home screen (cached)")
def insight(member_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = today_in(IST)
    key = f"insight:{user.id}:{d.isoformat()}"
    hit = cache.get(key)
    if hit:
        import json
        return json.loads(hit)
    ctx, based = _context(db, user)
    try:
        text = ai.chat([{"role": "system", "content": COACH_SYSTEM + " Now write ONE encouraging, specific insight for "
                         "the member's home screen in at most 2 sentences (max 40 words). Mention a concrete food or "
                         "action. No greeting."}, {"role": "user", "content": ctx}], max_tokens=500, temperature=0.6)
        out = {"text": text.strip().strip('"'), "based_on": based, "kind": "ai"}
    except ai.AiUnavailable:
        t = targets(user.fitness_profile, user.gender, user.dob)
        out = {"text": f"Aim for about {t['protein_g']} g protein today — eggs, paneer, dal, curd and chicken are easy wins.",
               "based_on": ["your targets"], "kind": "rule"}
    import json
    cache.set(key, json.dumps(out), ex=3 * 3600)
    return out


# ---------------- owner AI briefing ----------------
@gym.get("/gyms/{gymId}/ai/briefing", summary="AI briefing for the owner: today in 3–4 lines + what to do next",
         description="Summarises today’s numbers, renewals, dues, crowd and away members into plain advice. Cached "
                     "30 min (`refresh=true` regenerates). Money is only included for roles with payments.view.")
def briefing(refresh: bool = False, ctx: GymCtx = Depends(gym_ctx), db: Session = Depends(get_db)):
    key = f"brief:{ctx.gym.id}:{int(ctx.can_pay)}"
    import json
    if not refresh:
        hit = cache.get(key)
        if hit:
            return json.loads(hit)
    else:
        rate_limit(f"brief:{ctx.user.id}", 6, 3600)
    from app.routers.staff import summary as summary_ep
    sm = summary_ep(ctx=ctx, db=db)
    cr = gym_crowd(db, ctx.gym)
    facts = {"numbers": sm["numbers"], "needs_attention": [{k: i.get(k) for k in ("title", "detail")} for i in sm["insights"]],
             "expiring_soon": len(sm["expiring"]), "crowd_now": {k: cr[k] for k in ("now_count", "label", "trend")},
             "quietest_upcoming": [b["label"] for b in cr["best_times"]]}
    try:
        data = ai.chat_json([
            {"role": "system", "content": "You are a sharp gym-business assistant for an Indian gym owner. From the JSON "
             "facts, write a briefing. Reply ONLY JSON {\"headline\": string (max 14 words), \"points\": [3-4 strings, each "
             "max 22 words, specific with numbers], \"actions\": [2-3 short imperative suggestions]}. Amounts in the facts "
             "are in paise; show rupees with ₹. No invented numbers."},
            {"role": "user", "content": json.dumps(facts, default=str)}], max_tokens=1400, temperature=0.3)
        out = {"headline": str(data.get("headline", ""))[:120], "points": [str(p)[:200] for p in data.get("points", [])][:4],
               "actions": [str(a)[:120] for a in data.get("actions", [])][:3], "kind": "ai"}
    except ai.AiUnavailable:
        n = sm["numbers"]
        out = {"headline": f"{n.get('checkins_today', 0)} check-ins today · {n.get('inside_now', 0)} inside now",
               "points": [i["title"] for i in sm["insights"]][:4],
               "actions": ["Call members expiring this week", "Follow up on dues"], "kind": "rule"}
    out["generated_at"] = utcnow().isoformat()
    cache.set(key, json.dumps(out), ex=1800)
    return out
