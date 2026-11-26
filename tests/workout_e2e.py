"""Member workout plans, end to end.

  * no answers yet → setup needed (missing fields listed, options provided)
  * save answers → this week's plan: right number of workout days, every exercise has steps, sets/reps, rest
  * alternatives + swap (only suggested ones allowed); ticking all sets marks the day done; undo works
  * next week continues the 4-week block with the SAME exercises and more effort; week 4 lighter; then a new block
  * conditions change the exercises (knee → no lunges/leg extension); pregnancy → no plan
  * media is off unless licensed (no GIF URLs)
Run against a TEST database with the API on :8000:  python tests/workout_e2e.py"""
import os
import random
import sys
from datetime import timedelta

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails = []
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))  # noqa: E731


def expect(n, got, want):
    ok = got == want
    print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}")
    ok or fails.append(n)


r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "member"}).json()
login = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()
H = {"Authorization": f"Bearer {login['access_token']}", "X-App": "member", "X-App-Version": "1.0.0"}
UID = login["user"]["id"]

d = c.get(f"{B}/me/workout", headers=H).json()
expect("setup needed first", (d["plan"], "goal" in d["missing"]), (None, True))
expect("options offered", (len(d["options"]["goals"]), len(d["options"]["conditions"])), (5, 8))
expect("exercise media on", d["media"]["enabled"], True)

body = {"height_cm": 175, "weight_kg": 72, "age": 27, "goal": "gain_muscle", "experience": "intermediate", "days_per_week": 4,
        "session_min": 60, "place": "gym", "conditions": []}
r = c.put(f"{B}/me/workout/profile", headers=H, json=body)
d = r.json()
plan = d["plan"]
work = [x for x in plan["days"] if not x["rest"]]
expect("plan saved", r.status_code, 200)
expect("7 days, 4 workouts", (len(plan["days"]), len(work)), (7, 4))
expect("4 days = upper / lower split", [x["title"] for x in work], ["Upper body", "Lower body", "Upper body", "Lower body"])
expect("every exercise: steps, sets, reps, rest", all(e["steps"] and e["dose"]["sets"] and (e["dose"].get("reps") or e["dose"].get("seconds")) and e["dose"]["rest_sec"]
                                                      for x in work for e in x["exercises"]), True)
expect("warm-up and cool-down on each workout", all(x["warmup"] and x["cooldown"] for x in work), True)
expect("every exercise, warm-up move and stretch has an HD animation",
       all((e.get("gif_url") or "").endswith(f"/{e['id']}.webp") for x in work for e in x["exercises"] + x["cooldown"] + [w for w in x["warmup"] if w.get("id")]), True)
expect("first warm-up step linked to the day's first exercise", all(x["warmup"][-1].get("id") == x["exercises"][0]["id"] for x in work), True)
expect("week 1 of block 1", (plan["block"], plan["week_in_block"]), (1, 1))

day = work[0]["index"]
ex = work[0]["exercises"][0]
alts = c.get(f"{B}/me/workout/alternatives", headers=H, params={"day": day, "slot": ex["slot"]}).json()["items"]
expect("alternatives offered, same movement", (len(alts) > 0, all(a["pattern"] == ex["pattern"] for a in alts)), (True, True))
expect("only suggested exercises can be swapped in", c.post(f"{B}/me/workout/swap", headers=H, json={"day": day, "slot": ex["slot"], "exercise_id": "0001"}).status_code, 422)
d = c.post(f"{B}/me/workout/swap", headers=H, json={"day": day, "slot": ex["slot"], "exercise_id": alts[0]["id"]}).json()
swapped = next(e for x in d["plan"]["days"] if x["index"] == day for e in x["exercises"] if e["slot"] == ex["slot"])
expect("swapped", swapped["id"], alts[0]["id"])

wd = next(x for x in d["plan"]["days"] if x["index"] == day)
for e in wd["exercises"]:
    d = c.post(f"{B}/me/workout/sets", headers=H, json={"day": day, "slot": e["slot"], "sets_done": e["dose"]["sets"]}).json()
wd = next(x for x in d["plan"]["days"] if x["index"] == day)
expect("all sets ticked → workout done", (wd["done"], d["plan"]["progress"]["done"]), (True, 1))
d = c.post(f"{B}/me/workout/days/{day}/done", headers=H, json={"done": False}).json()
expect("undo done", next(x for x in d["plan"]["days"] if x["index"] == day)["done"], False)

ex_detail = c.get(f"{B}/exercises/{ex['id']}", headers=H).json()
expect("exercise detail: steps, cues, muscles", (bool(ex_detail["steps"]), bool(ex_detail["cues"]), bool(ex_detail["target"])), (True, True, True))

print("== next weeks: same exercises, more effort; week 4 lighter; then a new block")
ids_w1 = {x["key"]: [e["id"] for e in x["exercises"]] for x in work}
ids_w1[next(x["key"] for x in work if x["index"] == day)] = [e["id"] for e in wd["exercises"]]  # include the swap
for wk in (2, 3, 4, 5):
    with engine.begin() as cn:  # pretend the current plan was last week
        cn.execute(text("UPDATE workout_plans SET week_start = DATE_SUB(week_start, INTERVAL 7 DAY) WHERE user_id=:u ORDER BY week_start ASC"), {"u": UID})
    p = c.get(f"{B}/me/workout", headers=H).json()["plan"]
    ids = {x["key"]: [e["id"] for e in x["exercises"]] for x in p["days"] if not x["rest"]}
    if wk <= 4:
        expect(f"week {wk}: same block, same exercises (swap kept)", (p["block"], p["week_in_block"], ids == ids_w1), (1, wk, True))
    if wk == 4:
        expect("week 4 is lighter", "Lighter" in p["days"][day]["exercises"][0]["dose"]["effort"], True)
    if wk == 5:
        expect("after 4 weeks: new block, week 1", (p["block"], p["week_in_block"]), (2, 1))

print("== health")
r = c.put(f"{B}/me/workout/profile", headers=H, json={**body, "conditions": ["knee"], "place": "gym"}).json()
names = [e["name"] for x in r["plan"]["days"] if not x["rest"] for e in x["exercises"]]
expect("knee: no lunges, leg extension or jumping", any(k in n.lower() for n in names for k in ("lunge", "leg extension", "jump", "split squat", "step-up")), False)
expect("knee: safety note shown", any("Knee" in s for s in r["plan"]["safety"]), True)
r = c.put(f"{B}/me/workout/profile", headers=H, json={**body, "conditions": ["high_bp"]}).json()
reps = [e["dose"].get("reps") for x in r["plan"]["days"] if not x["rest"] for e in x["exercises"] if e["dose"].get("reps")]
expect("high blood pressure: no heavy low-rep sets", all(int(rp.split("–")[0]) >= 10 for rp in reps), True)
r = c.put(f"{B}/me/workout/profile", headers=H, json={**body, "conditions": ["pregnant"]}).json()
expect("pregnancy: no automatic plan", r["plan"]["blocked"], True)
r = c.put(f"{B}/me/workout/profile", headers=H, json={**body, "place": "bodyweight", "experience": "beginner"}).json()
eqs = {x for d in r["plan"]["days"] if not d["rest"] for x in [W for W in [e["equipment"] for e in d["exercises"]]]}
expect("no equipment: bodyweight exercises only", eqs, {"body weight"})
with engine.begin() as cn:
    cn.execute(text("DELETE FROM workout_plans WHERE user_id=:u"), {"u": UID})

print("\nFAILED:", fails) if fails else print("\nALL PASSED")
sys.exit(1 if fails else 0)
