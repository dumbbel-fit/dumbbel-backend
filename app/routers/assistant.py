"""Gym performance score and Ask DUMBBEL (the owner assistant).

  GET  /gyms/{gymId}/score                     0–100 score from five parts, last month's score, what to work on
  POST /gyms/{gymId}/assistant                 ask a question; answered from this gym's numbers and the product guide
  POST /gyms/{gymId}/assistant/{logId}/feedback  helpful yes / no
  GET  /internal/insights/scores               every live gym's score (platform team)
  GET  /internal/insights/assistant-questions  what gyms asked, newest first (platform team)

Privacy: the model only gets counts and money totals for the gym. No member names, phone numbers or ids."""
import json
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, perm_ctx, platform_admin
from app.errors import ApiError
from app.models import AssistantLog, Gym, User
from app.responses import DocRoute, ERRORS
from app.services import ai
from app.services.cache import rate_limit
from app.services.gym_score import gym_numbers, gym_score
from app.utils import iso, rupees, utcnow

router = APIRouter(tags=["Gym score & assistant"], route_class=DocRoute, responses=ERRORS)
console = APIRouter(prefix="/internal/insights", tags=["Executive console"], route_class=DocRoute, responses=ERRORS)

# How the product works, in the words the dashboard uses. Keep in step with the dashboard's Help (src/pages/admin/Help.jsx).
GUIDE = """
DUMBBEL.fit dashboard, how things work (left menu names in quotes):
- Add a member: "Members" → Add member → Individual (one person) or Custom (several people, e.g. a couple or family). "Save & assign plan" gives a plan and records what they paid. Import an Excel/Google sheet with Import; nothing is saved until you confirm.
- Plans: "Membership Plans" → Create plan (name, days, price; optional offer price, joining fee, GST). Changing a price never changes what current members paid.
- Renew: open the member → Renew / Assign plan. A renewal starts the day after the current plan ends. Upgrade or change plan by choosing Replace.
- Payments: "Payments" → Record payment (cash, UPI, card, bank). Part payments are fine; the rest stays as due. A receipt is created (and sent on WhatsApp if linked). Undo with Void payment. Dues: Members list → filter "Dues".
- UPI: add your UPI ID in Settings; members can pay from their app; check your bank and tap Verify under "UPI to verify".
- Check-in: print the QR poster from "Check-in QR" and put it at the entrance. Members scan it with their phone camera; no location is needed. Expired, frozen or not-yet-started plans are stopped at the door. The desk can also scan a member's QR or check in by name at "Check-in desk". "Reset QR" if a copy leaked.
- Staff: "Staff & Roles" → Add staff with a role (manager, receptionist, trainer) and choose what they can do. Staff mark attendance by scanning the same QR when they arrive and leave; a shift left open ends by itself after 8 hours. Location for staff is optional (owner switch under Check-in QR → Rules & location).
- WhatsApp: "WhatsApp" → Link, scan with the gym's WhatsApp (Linked devices). Then welcome messages, receipts and expiry reminders go from the gym's own number. Automations can be switched on/off.
- Members list filters: Active, Expiring, Expired, Frozen, Dues, "Away 14+ days" (not visited), New this month, Renewed this month. Export downloads a sheet for Excel.
- Reports & Analytics: collections, renewals, new members and attendance by day and month.
- Member app: members sign in with the phone number the gym has; they see their plan and days left, check in, see streaks and badges, renew and pay online.
- Help: the ? button on every page, or WhatsApp the DUMBBEL.fit team from there.
""".strip()

SYSTEM = """You are "Ask DUMBBEL", the assistant inside the DUMBBEL.fit gym dashboard. You help gym owners and managers
understand their gym's numbers and use the dashboard.

Rules:
- For anything about the gym's performance, use ONLY the numbers in GYM DATA. Never invent or estimate numbers that are not there.
  If something isn't in the data, say you don't have it and point to where in the dashboard they can see it.
- Explain the "why" by connecting numbers (for example: revenue fell, and renewals and visits fell too).
  Percentages are already worked out for you where they matter; quote them as given.
- Money is in Indian rupees, already formatted (₹1,23,000). Keep that format.
- End with one to three short, concrete next steps, naming the exact place in the dashboard (from the GUIDE).
- For "how do I…" questions, answer from the GUIDE in short numbered steps.
- Be brief and plain: 2 to 6 short sentences or a short list. No tables, no headings, no markdown symbols except "-" lists and numbers.
- Reply in the language the user wrote in (English, Hindi, Telugu, or a mix like Hinglish).
- You do not have member names or phone numbers; for lists of people, point to the Members list and its filters.
- If asked something unrelated to running the gym or using DUMBBEL.fit, say politely that you can help with the gym and the dashboard."""

SUGGESTED = ["Why did my revenue change this month?", "How many members haven't visited in 10 days?",
             "How can I improve my gym score?", "How do I send expiry reminders on WhatsApp?",
             "How do I add a couple on one plan?", "Who should I call today to renew?"]


def _readable(obj):
    """paise → ₹ text, so the model never has to do money maths."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.endswith("_paise") and isinstance(v, int):
                out[k[:-6]] = rupees(v)
            else:
                out[k] = _readable(v)
        return out
    if isinstance(obj, list):
        return [_readable(x) for x in obj]
    return obj


def _facts(db: Session, gym, s: dict | None = None) -> dict:
    s = s or gym_score(db, gym)
    return {"numbers": gym_numbers(db, gym),
            "score": {"overall": s["score"], "grade": s["grade"], "last_month": s["previous_score"],
                      "parts": {p["label"]: {"score": p["score"], "rate_pct": p["rate_pct"], "last_month": p["previous_score"]}
                                for p in s["parts"]},
                      "how_it_is_worked_out": "Retention 25%, Renewal 25%, Attendance 20%, Collection 15%, New members 15%. "
                                              "Parts without enough data are left out."}}


def _chg(now, before) -> str:
    if not before:
        return f"{now} (none in the same days last month)"
    d = now - before
    return f"{now} vs {before} last month ({'+' if d >= 0 else ''}{round(100 * d / before)}%)"


def plain_answer(raw: dict, question: str) -> str:
    """Used when the AI model is slow or down: a short, correct answer built straight from the numbers / guide."""
    q = question.lower()
    if any(w in q for w in ("how do", "how to", "how can i", "where", "kaise", "ela ")):
        words = [w for w in q.replace("?", " ").split() if len(w) > 3]
        hits = [ln for ln in GUIDE.splitlines() if ln.startswith("-") and any(w in ln.lower() for w in words)]
        if hits:
            return "Here’s how:\n" + "\n".join(hits[:3])
    n, s = raw["numbers"], raw["score"]
    rv = n["revenue"]
    lines = []
    if rv["last_month_same_days_paise"]:
        pc = rv["change_pct"]
        lines.append(f"Revenue so far this month is {rupees(rv['this_month_paise'])}, against {rupees(rv['last_month_same_days_paise'])} "
                     f"in the same days last month ({'+' if pc >= 0 else ''}{pc}%).")
    else:
        lines.append(f"Revenue so far this month is {rupees(rv['this_month_paise'])}.")
    lines.append(f"Renewals: {_chg(n['renewals']['this_month'], n['renewals']['last_month_same_days'])}. "
                 f"New members: {_chg(n['new_members']['this_month'], n['new_members']['last_month_same_days'])}.")
    lines.append(f"{n['members_not_visited_10_days']} of your {n['active_members']} active members haven’t visited in the past 10 days, "
                 f"and {n['expiring_next_7_days']} plans end in the next 7 days.")
    if n["dues"]["members"]:
        lines.append(f"{n['dues']['members']} members owe {rupees(n['dues']['total_paise'])} in dues.")
    if s["score"] is not None:
        lines.append(f"Your gym score is {s['score']} ({s['grade']}).")
    lines.append("Next: call the members whose plans end this week (Members → Expiring), collect dues (Members → Dues), "
                 "and message members who haven’t come in (Members → Away 14+ days).")
    return " ".join(lines)


@router.get("/gyms/{gymId}/score", summary="Gym performance score (0–100) from retention, attendance, renewals, new members and collection")
def score(ctx: GymCtx = Depends(perm_ctx("payments.view")), db: Session = Depends(get_db)):
    return gym_score(db, ctx.gym)


class AskIn(BaseModel):
    question: str = Field(..., min_length=2, max_length=600)
    history: list[dict] = Field(default_factory=list, description="Earlier turns: [{role: user|assistant, content}] (last 6 kept)")


@router.post("/gyms/{gymId}/assistant", summary="Ask DUMBBEL: answers from this gym's own numbers and the dashboard guide")
def ask(body: AskIn, ctx: GymCtx = Depends(perm_ctx("payments.view")), db: Session = Depends(get_db)):
    rate_limit(f"assistant:{ctx.user.id}", 30, 3600)
    gym = ctx.gym
    sc = gym_score(db, gym)
    raw = _facts(db, gym, sc)
    raw["score"]["score"], raw["score"]["grade"] = sc["score"], sc["grade"]
    facts = _readable(raw)
    hist = [{"role": h.get("role"), "content": str(h.get("content", ""))[:1500]} for h in body.history[-6:]
            if h.get("role") in ("user", "assistant") and h.get("content")]
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "system", "content": "GUIDE:\n" + GUIDE},
            {"role": "system", "content": f"GYM DATA for {gym.name} (asked by {ctx.role_name or 'staff'}):\n" + json.dumps(facts, ensure_ascii=False)},
            *hist, {"role": "user", "content": body.question.strip()}]
    t0 = time.perf_counter()
    try:
        answer, provider = ai.ask(msgs)
    except ai.AiUnavailable:  # model slow or down: still answer, straight from the numbers
        answer, provider = plain_answer(raw, body.question), "numbers"
    ms = int((time.perf_counter() - t0) * 1000)
    q = body.question.lower()
    topic = "how_to" if any(w in q for w in ("how do", "how to", "how can i add", "where", "kaise", "ela")) else "numbers"
    log = AssistantLog(gym_id=gym.id, user_id=ctx.user.id, question=body.question.strip()[:600], answer=answer[:4000],
                       topic=topic, provider=provider, ms=ms)
    db.add(log)
    db.commit()
    return {"id": log.id, "answer": answer, "ms": ms, "quick": provider == "numbers"}


@router.get("/gyms/{gymId}/assistant", summary="Ask DUMBBEL: is it on, and suggested questions")
def assistant_info(ctx: GymCtx = Depends(perm_ctx("payments.view"))):
    return {"enabled": True, "ai": ai.assistant_enabled(), "suggested": SUGGESTED}


class FeedbackIn(BaseModel):
    helpful: bool


@router.post("/gyms/{gymId}/assistant/{logId}/feedback", status_code=204, summary="Was the answer helpful?")
def feedback(logId: str, body: FeedbackIn, ctx: GymCtx = Depends(perm_ctx("payments.view")), db: Session = Depends(get_db)):
    row = db.get(AssistantLog, logId)
    if not row or row.gym_id != ctx.gym.id or row.user_id != ctx.user.id:
        raise ApiError(404, "NOT_FOUND")
    row.helpful = body.helpful
    db.commit()


# ---------------- platform team ----------------
@console.get("/scores", summary="Every live gym's performance score, lowest first, with its five parts")
def all_scores(admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    out = []
    for g in db.scalars(select(Gym).where(Gym.status == "active")):
        s = gym_score(db, g)
        out.append({"gym_id": g.id, "name": g.name, "area": g.area, "city": g.city, "score": s["score"], "grade": s["grade"],
                    "change": s["change"], "focus": s["focus"], "parts": {p["key"]: p["score"] for p in s["parts"]}})
    out.sort(key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0))
    have = [r["score"] for r in out if r["score"] is not None]
    return {"gyms": out, "average": round(sum(have) / len(have)) if have else None, "generated_at": iso(utcnow())}


@console.get("/assistant-questions", summary="What gyms asked Ask DUMBBEL (newest first): helps us see what's hard to understand")
def assistant_questions(days: int = Query(30, ge=1, le=365), admin: User = Depends(platform_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(AssistantLog, Gym.name).join(Gym, Gym.id == AssistantLog.gym_id)
                      .where(AssistantLog.created_at >= utcnow() - timedelta(days=days))
                      .order_by(AssistantLog.created_at.desc()).limit(500)).all()
    items = [{"id": r.id, "gym": name, "question": r.question, "answer": r.answer, "topic": r.topic, "helpful": r.helpful,
              "ms": r.ms, "at": iso(r.created_at)} for r, name in rows]
    rated = [i for i in items if i["helpful"] is not None]
    return {"items": items, "count": len(items), "generated_at": iso(utcnow()), "how_to_share_pct": round(100 * sum(i["topic"] == "how_to" for i in items) / len(items)) if items else None,
            "helpful_pct": round(100 * sum(1 for i in rated if i["helpful"]) / len(rated)) if rated else None}
