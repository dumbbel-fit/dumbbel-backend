"""DUMBBEL Explore: find gyms near you on a map (photos, prices, hours, crowd) and book a visit / free trial.

Public:   GET  /discover/gyms              nearby or searched gyms (map pins + cards)
          GET  /discover/gyms/{gymId}      full listing: photos, plans, hours, amenities, crowd
Member:   POST /me/visits                  book a visit → gym staff get a push + dashboard badge
          GET  /me/visits                  my bookings
          POST /me/visits/{id}/cancel
Gym:      GET  /gyms/{gymId}/visits        requests & upcoming visits (members.view)
          PATCH /gyms/{gymId}/visits/{id}  confirm · decline · visited · no-show (members.edit) → member gets a push
          POST /gyms/{gymId}/visits/{id}/convert   add the visitor as a member (members.edit)
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from datetime import date as Date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import GymCtx, bearer, current_user, get_auth, perm_ctx
from app.errors import ApiError, validation
from app.models import Gym, GymMember, GymPhoto, GymStaff, GymVisit, Plan, User
from app.permissions import effective_permissions
from app.responses import DocRoute, ERRORS
from app.services.audit import audit
from app.services.cache import rate_limit
from app.services.core import gym_settings, next_member_code
from app.services.jobs import enqueue
from app.services.push import send_push
from app.services.storage import existing_keys, public_url
from app.utils import iso, today_in, tz, utcnow

public = APIRouter(tags=["Explore (find gyms & book visits)"], route_class=DocRoute, responses=ERRORS)
me = APIRouter(tags=["Explore (find gyms & book visits)"], route_class=DocRoute, responses=ERRORS)
gym = APIRouter(tags=["Visits (gym side)"], route_class=DocRoute, responses=ERRORS)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OPEN_VISIT = ("requested", "confirmed")


# ---------------- helpers ----------------
def day_ranges(g: Gym, d: date) -> list[tuple[str, str]] | None:
    """Opening ranges for a date. None = hours not set; [] = closed. Reads [["06:00","22:00"]] and older {open,close}."""
    hrs = g.hours if isinstance(g.hours, dict) else None
    if not hrs:
        return None
    v = hrs.get(DAY_KEYS[d.weekday()])
    if v is None:
        return None if DAY_KEYS[d.weekday()] not in hrs else []
    items = v if isinstance(v, list) else [v]
    out = []
    for it in items:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            out.append((str(it[0])[:5], str(it[1])[:5]))
        elif isinstance(it, dict) and not it.get("closed") and it.get("open") and it.get("close"):
            out.append((str(it["open"])[:5], str(it["close"])[:5]))
    return out


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def open_now(g: Gym) -> dict:
    now = utcnow().astimezone(tz(g.timezone))
    rng = day_ranges(g, now.date())
    if rng is None:
        return {"known": False, "open": None, "today": None}
    cur = now.hour * 60 + now.minute
    is_open = any(_mins(o) <= cur < (_mins(c) if _mins(c) > _mins(o) else 24 * 60) for o, c in rng)
    today = ", ".join(f"{o}–{c}" for o, c in rng) if rng else "Closed today"
    closes = next((c for o, c in rng if _mins(o) <= cur < (_mins(c) if _mins(c) > _mins(o) else 1440)), None)
    return {"known": True, "open": is_open, "today": today, "closes_at": closes}


def week_hours(g: Gym) -> list[dict]:
    base = today_in(g.timezone)
    monday = base - timedelta(days=base.weekday())
    out = []
    for i, k in enumerate(DAY_KEYS):
        rng = day_ranges(g, monday + timedelta(days=i))
        out.append({"day": k, "ranges": [list(r) for r in rng] if rng is not None else None,
                    "closed": rng == []})
    return out


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def eff_price(p: Plan) -> int:
    return p.offer_price_paise if p.offer_price_paise is not None and p.offer_price_paise < p.price_paise else p.price_paise


def price_from(plans: list[Plan]) -> dict | None:
    """Cheapest plan by monthly-equivalent price (so a ₹500/day pass doesn't look cheaper than ₹1,500/month)."""
    if not plans:
        return None
    longish = [p for p in plans if p.duration_days >= 28] or plans
    best = min(longish, key=lambda p: eff_price(p) * 30 / max(p.duration_days, 1))
    return {"amount_paise": eff_price(best), "list_price_paise": best.price_paise, "duration_days": best.duration_days,
            "plan_name": best.name, "per_month_paise": round(eff_price(best) * 30 / max(best.duration_days, 1)),
            "gst_mode": best.gst_mode, "gst_rate": float(best.gst_rate or 0)}


def listed(g: Gym) -> bool:
    return g.status == "active" and gym_settings(g).get("explore_listed", True) is not False


def _photos_by_gym(db: Session, ids: list[str], per_gym: int | None = 6) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    rows = db.scalars(select(GymPhoto).where(GymPhoto.gym_id.in_(ids or [""]))
                      .order_by(GymPhoto.gym_id, GymPhoto.sort_order, GymPhoto.created_at)).all()
    ok = existing_keys([p.file_key for p in rows])  # skip photos whose file was lost (old ephemeral disk)
    for p in rows:
        if p.file_key in ok and (per_gym is None or len(out[p.gym_id]) < per_gym):
            out[p.gym_id].append(public_url(p.file_key))
    return out


def _plans_by_gym(db: Session, ids: list[str]) -> dict[str, list[Plan]]:
    out: dict[str, list[Plan]] = defaultdict(list)
    for p in db.scalars(select(Plan).where(Plan.gym_id.in_(ids or [""]), Plan.active.is_(True))
                        .order_by(Plan.sort_order, Plan.price_paise)):
        out[p.gym_id].append(p)
    return out


def _facilities(g: Gym, plans: list[Plan]) -> list[str]:
    seen, out = set(), []
    for x in [*(gym_settings(g).get("amenities") or []), *[f for p in plans for f in (p.facilities or [])]]:
        k = str(x).strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def card(g: Gym, photos: list[str], plans: list[Plan], dist: float | None) -> dict:
    s = gym_settings(g)
    return {"id": g.id, "name": g.name, "area": g.area, "city": g.city, "address_line": g.address_line,
            "lat": float(g.lat) if g.lat is not None else None, "lng": float(g.lng) if g.lng is not None else None,
            "distance_km": round(dist, 2) if dist is not None else None,
            "cover_url": photos[0] if photos else public_url(g.logo_url), "photos": photos,
            "logo_url": public_url(g.logo_url), "price_from": price_from(plans), "plans_count": len(plans),
            "facilities": _facilities(g, plans)[:8], "hours": open_now(g),
            "visits_enabled": s.get("visits_enabled", True) is not False}


def optional_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                  db: Session = Depends(get_db)) -> User | None:
    if not creds:
        return None
    try:
        return get_auth(request, creds, db).user
    except ApiError:
        return None


# ---------------- discover ----------------
@public.get("/discover/gyms", summary="Gyms near you (map pins + listing cards)",
            description="Pass `lat`/`lng` for gyms within `radius_km`, nearest first (or `sort=price`). Without a "
                        "location you get every listed gym (optionally filtered by `q` or `city`). Only approved gyms "
                        "that haven’t turned off Explore are shown. `price_from.per_month_paise` is the cheapest "
                        "monthly-equivalent price — use it for the map pin.")
def discover(lat: float | None = Query(None, ge=-90, le=90), lng: float | None = Query(None, ge=-180, le=180),
             radius_km: float = Query(15, ge=0.5, le=200), q: str | None = Query(None, max_length=80),
             city: str | None = Query(None, max_length=80), sort: Literal["distance", "price"] = "distance",
             limit: int = Query(60, ge=1, le=200), db: Session = Depends(get_db)):
    conds = [Gym.status == "active"]
    has_loc = lat is not None and lng is not None
    if has_loc:
        dlat = radius_km / 111.0
        dlng = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
        conds += [Gym.lat.is_not(None), Gym.lat.between(lat - dlat, lat + dlat), Gym.lng.between(lng - dlng, lng + dlng)]
    if q and q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Gym.name.ilike(like), Gym.area.ilike(like), Gym.city.ilike(like), Gym.pincode == q.strip()))
    if city and city.strip():
        conds.append(Gym.city.ilike(city.strip()))
    gyms = [g for g in db.scalars(select(Gym).where(*conds).limit(1000)) if listed(g)]
    rows = []
    for g in gyms:
        dist = haversine_km(lat, lng, float(g.lat), float(g.lng)) if has_loc and g.lat is not None else None
        if has_loc and (dist is None or dist > radius_km):
            continue
        rows.append((g, dist))
    ids = [g.id for g, _ in rows]
    photos, plans = _photos_by_gym(db, ids, 5), _plans_by_gym(db, ids)
    items = [card(g, photos.get(g.id, []), plans.get(g.id, []), d) for g, d in rows]
    if sort == "price":
        items.sort(key=lambda x: (x["price_from"] or {}).get("per_month_paise", 10 ** 12))
    elif has_loc:
        items.sort(key=lambda x: x["distance_km"])
    else:
        items.sort(key=lambda x: (x["photos"] == [], x["name"].lower()))
    cities = sorted({g.city for g in gyms if g.city})
    return {"items": items[:limit], "total": len(items), "center": {"lat": lat, "lng": lng} if has_loc else None,
            "radius_km": radius_km if has_loc else None, "cities": cities}


@public.get("/discover/gyms/{gymId}", summary="Gym listing: photos, plans & prices, hours, amenities, crowd")
def discover_gym(gymId: str, lat: float | None = Query(None, ge=-90, le=90), lng: float | None = Query(None, ge=-180, le=180),
                 user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    g = db.get(Gym, gymId)
    if not g or not listed(g):
        raise ApiError(404, "NOT_FOUND", "This gym isn’t listed right now.")
    s = gym_settings(g)
    plans = _plans_by_gym(db, [g.id]).get(g.id, [])
    photos = _photos_by_gym(db, [g.id], None).get(g.id, [])
    dist = haversine_km(lat, lng, float(g.lat), float(g.lng)) if lat is not None and lng is not None and g.lat is not None else None
    out = card(g, photos, plans, dist)
    from app.services.crowd import gym_crowd
    try:
        cr = gym_crowd(db, g)
        crowd = {"level": cr.get("level"), "label": cr.get("label"), "hint": cr.get("hint"),
                 "inside_now": cr.get("now_count"), "updated_at": cr.get("updated_at")}
    except Exception:
        crowd = None
    members = db.scalar(select(func.count()).select_from(GymMember).where(GymMember.gym_id == g.id,
                                                                          GymMember.status == "active")) or 0
    out.update({
        "phone": g.phone, "state": g.state, "pincode": g.pincode, "about": s.get("about"),
        "visit_note": s.get("visit_note"), "week_hours": week_hours(g), "timezone": g.timezone, "crowd": crowd,
        "members_band": "500+" if members >= 500 else "200+" if members >= 200 else "100+" if members >= 100 else "50+" if members >= 50 else None,
        "facilities": _facilities(g, plans),
        "plans": [{"id": p.id, "name": p.name, "description": p.description, "duration_days": p.duration_days,
                   "price_paise": p.price_paise, "offer_price_paise": p.offer_price_paise,
                   "joining_fee_paise": p.joining_fee_paise, "gst_mode": p.gst_mode, "gst_rate": float(p.gst_rate or 0),
                   "facilities": p.facilities or [], "freeze_allowed": p.freeze_allowed} for p in plans],
        "maps_url": (f"https://www.google.com/maps/dir/?api=1&destination={float(g.lat)},{float(g.lng)}" if g.lat is not None
                     else "https://www.google.com/maps/search/?api=1&query=" + "+".join(
                         x for x in [g.name, g.address_line, g.area, g.city] if x).replace(" ", "+")),
    })
    if user:
        mem = db.scalar(select(GymMember).where(GymMember.gym_id == g.id, GymMember.user_id == user.id))
        v = db.scalar(select(GymVisit).where(GymVisit.gym_id == g.id, GymVisit.user_id == user.id,
                                             GymVisit.status.in_(OPEN_VISIT),
                                             GymVisit.visit_date >= today_in(g.timezone)).order_by(GymVisit.visit_date))
        out["me"] = {"is_member": bool(mem and mem.status == "active"), "member_id": mem.id if mem else None,
                     "open_visit": ser_visit(v, g) if v else None}
    return out


# ---------------- visits ----------------
def ser_visit(v: GymVisit, g: Gym | None = None, cover: str | None = None, extra: dict | None = None) -> dict:
    d = {"id": v.id, "gym_id": v.gym_id, "date": v.visit_date.isoformat(), "time": v.slot, "status": v.status,
         "name": v.name, "phone": v.phone, "note": v.note, "goal": v.goal, "gym_note": v.gym_note,
         "cancelled_by": v.cancelled_by, "member_id": v.member_id, "created_at": iso(v.created_at),
         "handled_at": iso(v.handled_at)}
    if g is not None:
        d["gym"] = {"id": g.id, "name": g.name, "area": g.area, "city": g.city, "address_line": g.address_line,
                    "phone": g.phone, "cover_url": cover,
                    "maps_url": f"https://www.google.com/maps/dir/?api=1&destination={float(g.lat)},{float(g.lng)}" if g.lat is not None else None}
    if extra:
        d.update(extra)
    return d


def _when(v: GymVisit) -> str:
    return f"{v.visit_date.strftime('%a %d %b')} at {datetime.strptime(v.slot, '%H:%M').strftime('%I:%M %p').lstrip('0')}"


def _staff_to_notify(db: Session, g: Gym) -> list[str]:
    ids = {g.owner_user_id}
    for st in db.scalars(select(GymStaff).where(GymStaff.gym_id == g.id, GymStaff.status != "deactivated")):
        if "members.view" in effective_permissions(st.role, st.staff_role, bool(st.can_record_payments), st.permissions):
            ids.add(st.user_id)
    return list(ids)


class VisitIn(BaseModel):
    gym_id: str
    date: Date
    time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["18:30"])
    note: str | None = Field(None, max_length=500)
    goal: str | None = Field(None, max_length=40)
    name: str | None = Field(None, max_length=200)


@me.post("/me/visits", status_code=201, summary="Book a visit / free trial",
         description="The gym’s staff get a push notification and see it under Visits in the dashboard. "
                     "409 VISIT_EXISTS if you already have an upcoming visit there · 422 GYM_CLOSED / OUTSIDE_HOURS.")
def book_visit(body: VisitIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rate_limit(f"visit:{user.id}", 8, 86400)
    g = db.get(Gym, body.gym_id)
    if not g or not listed(g):
        raise ApiError(404, "NOT_FOUND", "This gym isn’t listed right now.")
    if gym_settings(g).get("visits_enabled", True) is False:
        raise ApiError(409, "VISITS_OFF", "This gym isn’t taking visit bookings in the app. Call them instead.")
    today = today_in(g.timezone)
    if body.date < today or body.date > today + timedelta(days=30):
        raise validation({"date": "Pick a date in the next 30 days."})
    now_local = utcnow().astimezone(tz(g.timezone))
    if body.date == today and _mins(body.time) < now_local.hour * 60 + now_local.minute + 30:
        raise validation({"time": "Pick a time at least 30 minutes from now."})
    rng = day_ranges(g, body.date)
    if rng == []:
        raise ApiError(422, "GYM_CLOSED", "The gym is closed that day. Pick another date.")
    if rng and not any(_mins(o) <= _mins(body.time) < (_mins(c) if _mins(c) > _mins(o) else 1440) for o, c in rng):
        raise ApiError(422, "OUTSIDE_HOURS", "That’s outside the gym’s opening hours.",
                       {"hours": [list(r) for r in rng]})
    open_v = db.scalar(select(GymVisit).where(GymVisit.gym_id == g.id, GymVisit.user_id == user.id,
                                              GymVisit.status.in_(OPEN_VISIT), GymVisit.visit_date >= today))
    if open_v:
        raise ApiError(409, "VISIT_EXISTS", "You already have a visit booked here.", {"visit": ser_visit(open_v)})
    name = (body.name or user.name or "").strip() or None
    if name and not user.name:
        user.name = name
    v = GymVisit(gym_id=g.id, user_id=user.id, name=name, phone=user.phone, visit_date=body.date, slot=body.time,
                 note=(body.note or "").strip() or None, goal=body.goal)
    db.add(v)
    db.flush()
    who = name or user.phone
    audit(db, g.id, user.id, "visit.request", "visit", v.id, f"{who} booked a visit for {_when(v)}")
    db.commit()
    for uid in _staff_to_notify(db, g):
        enqueue("push.send", send_push, uid, "admin", "New visit request 👋",
                f"{who} wants to visit {g.name} on {_when(v)}. Tap to confirm.",
                {"type": "visit_request", "gym_id": g.id, "visit_id": v.id})
    return ser_visit(v, g, (_photos_by_gym(db, [g.id], 1).get(g.id) or [None])[0])


@me.get("/me/visits", summary="My visit bookings")
def my_visits(user: User = Depends(current_user), db: Session = Depends(get_db)):
    vs = db.scalars(select(GymVisit).where(GymVisit.user_id == user.id)
                    .order_by(GymVisit.visit_date.desc(), GymVisit.slot.desc()).limit(50)).all()
    gyms = {g.id: g for g in db.scalars(select(Gym).where(Gym.id.in_({v.gym_id for v in vs} or {""})))}
    covers = _photos_by_gym(db, list(gyms), 1)
    items = [ser_visit(v, gyms.get(v.gym_id), (covers.get(v.gym_id) or [None])[0]) for v in vs]
    items.sort(key=lambda x: (x["status"] not in OPEN_VISIT, x["date"] if x["status"] in OPEN_VISIT else "", x["time"]))
    return {"items": items}


@me.post("/me/visits/{visitId}/cancel", summary="Cancel my visit")
def cancel_my_visit(visitId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v = db.get(GymVisit, visitId)
    if not v or v.user_id != user.id:
        raise ApiError(404, "NOT_FOUND", "That booking wasn’t found.")
    if v.status not in OPEN_VISIT:
        raise ApiError(409, "VISIT_CLOSED", "This booking can’t be cancelled any more.")
    g = db.get(Gym, v.gym_id)
    v.status, v.cancelled_by, v.handled_at = "cancelled", "member", utcnow()
    audit(db, g.id, user.id, "visit.cancel", "visit", v.id, f"{v.name or v.phone} cancelled their visit ({_when(v)})")
    db.commit()
    for uid in _staff_to_notify(db, g):
        enqueue("push.send", send_push, uid, "admin", "Visit cancelled",
                f"{v.name or v.phone} cancelled their visit on {_when(v)}.", {"type": "visit_cancelled", "visit_id": v.id})
    return ser_visit(v, g)


@gym.get("/gyms/{gymId}/visits", summary="Visit requests & bookings (members.view)",
         description="`view`: requests (awaiting your reply) · upcoming (confirmed, today onwards) · past · all.")
def gym_visits(view: Literal["requests", "upcoming", "past", "all"] = "requests", limit: int = Query(100, ge=1, le=200),
               ctx: GymCtx = Depends(perm_ctx("members.view")), db: Session = Depends(get_db)):
    g = ctx.gym
    today = today_in(g.timezone)
    conds = [GymVisit.gym_id == g.id]
    order = [GymVisit.visit_date, GymVisit.slot]
    if view == "requests":
        conds += [GymVisit.status == "requested", GymVisit.visit_date >= today]
    elif view == "upcoming":
        conds += [GymVisit.status == "confirmed", GymVisit.visit_date >= today]
    elif view == "past":
        conds.append(or_(GymVisit.visit_date < today, GymVisit.status.in_(("cancelled", "visited", "no_show", "joined"))))
        order = [GymVisit.visit_date.desc(), GymVisit.slot.desc()]
    else:
        order = [GymVisit.visit_date.desc(), GymVisit.slot.desc()]
    vs = db.scalars(select(GymVisit).where(*conds).order_by(*order).limit(limit)).all()
    members = {m.user_id: m for m in db.scalars(select(GymMember).where(
        GymMember.gym_id == g.id, GymMember.user_id.in_({v.user_id for v in vs} or {""})))}
    base = [GymVisit.gym_id == g.id, GymVisit.visit_date >= today]
    counts = {"requests": db.scalar(select(func.count()).select_from(GymVisit).where(*base, GymVisit.status == "requested")) or 0,
              "upcoming": db.scalar(select(func.count()).select_from(GymVisit).where(*base, GymVisit.status == "confirmed")) or 0,
              "today": db.scalar(select(func.count()).select_from(GymVisit).where(
                  GymVisit.gym_id == g.id, GymVisit.visit_date == today, GymVisit.status.in_(OPEN_VISIT))) or 0}
    return {"items": [ser_visit(v, extra={"existing_member_id": members[v.user_id].id if v.user_id in members else None,
                                          "past": v.visit_date < today}) for v in vs],
            "counts": counts, "today": today.isoformat()}


class VisitPatch(BaseModel):
    status: Literal["confirmed", "cancelled", "visited", "no_show"] | None = None
    date: Date | None = Field(None, description="Reschedule (keeps or sets status confirmed)")
    time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    gym_note: str | None = Field(None, max_length=500, description="Message shown to the visitor")


ALLOWED = {"requested": {"confirmed", "cancelled"}, "confirmed": {"cancelled", "visited", "no_show"},
           "visited": {"no_show"}, "no_show": {"visited"}, "cancelled": set(), "joined": set()}


@gym.patch("/gyms/{gymId}/visits/{visitId}", summary="Confirm, decline, reschedule or mark a visit (members.edit)")
def update_visit(visitId: str, body: VisitPatch, ctx: GymCtx = Depends(perm_ctx("members.edit")),
                 db: Session = Depends(get_db)):
    g = ctx.gym
    v = db.get(GymVisit, visitId)
    if not v or v.gym_id != g.id:
        raise ApiError(404, "NOT_FOUND", "That visit wasn’t found.")
    data = body.model_dump(exclude_unset=True)
    resched = "date" in data or "time" in data
    new_status = data.get("status") or ("confirmed" if resched and v.status == "requested" else None)
    if resched:
        if v.status not in OPEN_VISIT:
            raise ApiError(409, "VISIT_CLOSED", "Only open visits can be rescheduled.")
        if body.date and body.date < today_in(g.timezone):
            raise validation({"date": "Pick today or a later date."})
        v.visit_date = body.date or v.visit_date
        v.slot = body.time or v.slot
    if new_status and new_status != v.status:
        if new_status not in ALLOWED.get(v.status, set()):
            raise ApiError(409, "BAD_TRANSITION", f"A {v.status.replace('_', '-')} visit can’t be marked {new_status.replace('_', '-')}.")
        v.status = new_status
        if new_status == "cancelled":
            v.cancelled_by = "gym"
    if "gym_note" in data:
        v.gym_note = (data["gym_note"] or "").strip() or None
    v.handled_by, v.handled_at = ctx.user.id, utcnow()
    audit(db, g.id, ctx.user.id, "visit.update", "visit", v.id,
          f"Visit by {v.name or v.phone}: {v.status.replace('_', ' ')} ({_when(v)})")
    db.commit()
    msg = {"confirmed": ("Visit confirmed ✅", f"{g.name} is expecting you on {_when(v)}."),
           "cancelled": ("Visit declined", f"{g.name} can’t take your visit on {_when(v)}." + (f" {v.gym_note}" if v.gym_note else ""))}
    if resched and v.status == "confirmed":
        msg["confirmed"] = ("Visit time updated", f"{g.name} has you down for {_when(v)}.")
    if v.status in msg and (new_status or resched):
        t, b = msg[v.status]
        enqueue("push.send", send_push, v.user_id, "member", t, b, {"type": "visit_" + v.status, "visit_id": v.id})
    return ser_visit(v)


@gym.post("/gyms/{gymId}/visits/{visitId}/convert", summary="Add the visitor as a member (members.edit)",
          description="Creates the member (no plan yet) and marks the visit `joined`. Then assign a plan as usual. "
                      "If they’re already a member, returns that member.")
def convert_visit(visitId: str, ctx: GymCtx = Depends(perm_ctx("members.edit")), db: Session = Depends(get_db)):
    g = ctx.gym
    v = db.get(GymVisit, visitId)
    if not v or v.gym_id != g.id:
        raise ApiError(404, "NOT_FOUND", "That visit wasn’t found.")
    u = db.get(User, v.user_id)
    m = db.scalar(select(GymMember).where(GymMember.gym_id == g.id, GymMember.user_id == u.id))
    created = False
    if not m:
        m = GymMember(gym_id=g.id, user_id=u.id, member_code=next_member_code(db, g.id),
                      display_name=(v.name or u.name or u.phone).strip(), source="app", status="active",
                      joined_on=today_in(g.timezone), notes=v.note)
        db.add(m)
        db.flush()
        created = True
        audit(db, g.id, ctx.user.id, "member.create", "member", m.id, f"Added member {m.display_name} from a visit")
    elif m.status == "archived":
        m.status = "active"
    v.status, v.member_id, v.handled_by, v.handled_at = "joined", m.id, ctx.user.id, utcnow()
    db.commit()
    return {"member_id": m.id, "created": created, "visit": ser_visit(v)}
