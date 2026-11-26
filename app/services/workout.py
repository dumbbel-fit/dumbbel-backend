"""Weekly workout plans for members, built from their body, goal, experience, schedule, equipment and health.

Exercises come from app/data/exercises (MIT text data, see README there). Plans use a hand-picked LIBRARY of
well-known, coachable exercises (not random dataset entries), grouped by movement pattern, with the equipment they
need, a difficulty level, and the health conditions they don't suit. Rules a coach would use:

  * split by days a week: 2-3 full body · 4 upper/lower · 5 push/pull/legs + upper/lower · 6 push/pull/legs ×2
  * sets / reps / rest by goal, fewer and simpler exercises for beginners, more for advanced, fitted to session time
  * big lifts first, then accessories, then core; warm-up (easy cardio + 2 prep moves) and cool-down stretches
    matched to the muscles trained; cardio finishers for fat loss / stamina / general fitness
  * safety: knee / lower back / shoulder problems swap in friendlier exercises; high blood pressure or a heart
    condition keep effort moderate (no heavy low-rep sets); BMI 30+ or age 60+ means low-impact cardio only;
    pregnancy gets no plan (see a doctor or prenatal trainer)
  * the same exercises stay for a 4-week block so progress can be tracked; reps / sets step up weeks 1-3,
    week 4 is lighter (deload); a new block brings fresh accessories.
"""
import hashlib
import json
import random
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from app.config import settings

DATA = Path(__file__).resolve().parent.parent / "data" / "exercises" / "exercises.json"
MEDIA_ATTRIBUTION = "© Gym visual — https://gymvisual.com/"


@lru_cache(maxsize=1)
def dataset() -> dict[str, dict]:
    return {x["id"]: x for x in json.loads(DATA.read_text(encoding="utf-8"))}


# ---------------------------------------------------------------------------------------------------------------
# Library. places: g = gym, h = home with dumbbells, b = bodyweight only. level 1 beginner … 3 advanced.
# avoid: knee / back / shoulder / impact (high-impact) / heavy (unsuitable for heavy low-rep work).
# mode: reps (default) | hold (seconds) | cardio (minutes).  name = clear display name (dataset name kept as "dataset_name").
def L(id, pattern, places, level=1, avoid=(), name=None, mode="reps", compound=False, also=(), goals=None, fam=None):
    return {"id": id, "pattern": pattern, "also": tuple(also), "places": places, "level": level, "avoid": set(avoid), "name": name,
            "mode": mode, "compound": compound, "goals": set(goals) if goals else None, "fam": fam or id}


LIBRARY = [
    # squat pattern (quads, glutes)
    L("0043", "squat", "g", 2, ("knee", "back"), "Barbell back squat", compound=True),
    L("0770", "squat", "g", 1, ("knee",), "Smith machine squat", compound=True),
    L("0739", "squat", "g", 1, (), "Leg press", compound=True),
    L("0743", "squat", "g", 2, ("knee",), "Hack squat (machine)", compound=True),
    L("1760", "squat", "gh", 1, (), "Dumbbell goblet squat", compound=True),
    L("0413", "squat", "h", 1, ("knee",), "Dumbbell squat", compound=True),
    L("3533", "squat", "b", 1, (), "Bodyweight squat", compound=True),
    L("3132", "squat", "bh", 1, (), "Supported squat (hold a chair)", compound=True),
    # hinge (hamstrings, glutes, lower back)
    L("0032", "hinge", "g", 3, ("back", "heavy_bp"), "Barbell deadlift", compound=True),
    L("0085", "hinge", "g", 2, ("back",), "Barbell Romanian deadlift", compound=True),
    L("1459", "hinge", "gh", 1, ("back",), "Dumbbell Romanian deadlift", compound=True),
    L("0549", "hinge", "gh", 2, ("back",), "Kettlebell swing", compound=True, goals=("lose_fat", "improve_stamina", "stay_fit")),
    L("1409", "hinge", "g", 2, (), "Barbell hip thrust / glute bridge", compound=True),
    L("3013", "hinge", "ghb", 1, (), "Glute bridge", compound=True),
    # single leg
    L("0381", "lunge", "gh", 1, ("knee",), "Dumbbell reverse lunge"),
    L("0336", "lunge", "gh", 1, ("knee",), "Dumbbell lunge"),
    L("0410", "lunge", "gh", 2, ("knee",), "Dumbbell split squat"),
    L("0431", "lunge", "gh", 1, ("knee",), "Dumbbell step-up"),
    L("1460", "lunge", "b", 1, ("knee",), "Walking lunge"),
    L("2368", "lunge", "b", 1, ("knee",), "Split squat"),
    L("3561", "lunge", "bh", 1, (), "Glute bridge march"),
    # leg isolation
    L("0585", "quad_iso", "g", 1, ("knee",), "Leg extension (machine)"),
    L("0586", "ham_iso", "g", 1, (), "Lying leg curl (machine)"),
    L("0599", "ham_iso", "g", 1, (), "Seated leg curl (machine)"),
    L("0710", "glute_iso", "hb", 1, (), "Side-lying hip abduction"),
    L("0597", "glute_iso", "g", 1, (), "Hip abduction (machine)"),
    L("0605", "calf", "g", 1, (), "Standing calf raise (machine)"),
    L("0417", "calf", "gh", 1, (), "Dumbbell standing calf raise"),
    L("1373", "calf", "b", 1, (), "Bodyweight calf raise"),
    # horizontal push (chest)
    L("0025", "h_push", "g", 2, ("shoulder", "heavy_bp"), "Barbell bench press", compound=True),
    L("0289", "h_push", "g", 1, (), "Dumbbell bench press", compound=True),
    L("0314", "h_push", "g", 1, (), "Incline dumbbell press", compound=True, also=("incline_push",)),
    L("0757", "incline_push", "g", 1, (), "Incline press (Smith machine)", compound=True),
    L("1299", "incline_push", "g", 1, (), "Incline chest press (machine)", compound=True),
    L("0577", "h_push", "g", 1, (), "Chest press (machine)", compound=True),
    L("0662", "h_push", "hb", 2, (), "Push-up", compound=True),
    L("3211", "h_push", "hb", 1, (), "Kneeling push-up", compound=True),
    L("0493", "h_push", "hb", 1, (), "Incline push-up (hands on a bench)", compound=True),
    L("0659", "h_push", "hb", 1, (), "Wall push-up", compound=True),
    L("0308", "chest_iso", "gh", 1, ("shoulder",), "Dumbbell fly"),
    L("0596", "chest_iso", "g", 1, (), "Chest fly (machine)"),
    # vertical push (shoulders)
    L("0405", "v_push", "gh", 1, ("shoulder",), "Seated dumbbell shoulder press", compound=True),
    L("0603", "v_push", "g", 1, ("shoulder",), "Shoulder press (machine)", compound=True),
    L("0091", "v_push", "g", 2, ("shoulder", "heavy_bp"), "Seated barbell overhead press", compound=True),
    L("0426", "v_push", "h", 1, ("shoulder",), "Standing dumbbell overhead press", compound=True),
    L("0334", "lat_raise", "gh", 1, (), "Dumbbell lateral raise"),
    L("0178", "lat_raise", "g", 1, (), "Cable lateral raise"),
    # horizontal pull (upper back)
    L("0861", "h_pull", "g", 1, (), "Seated cable row", compound=True),
    L("1350", "h_pull", "g", 1, (), "Seated row (machine)", compound=True),
    L("0293", "h_pull", "gh", 1, ("back",), "Dumbbell bent-over row", compound=True),
    L("0027", "h_pull", "g", 2, ("back",), "Barbell bent-over row", compound=True),
    L("0541", "h_pull", "g", 1, (), "Kettlebell one-arm row", compound=True),
    L("3166", "h_pull", "b", 1, (), "Standing row (table / bar / door strap)", compound=True),
    L("0499", "h_pull", "b", 2, (), "Inverted row", compound=True),
    # vertical pull (lats)
    L("0198", "v_pull", "g", 1, (), "Lat pulldown", compound=True),
    L("0579", "v_pull", "g", 1, (), "Lat pulldown (machine)", compound=True),
    L("0017", "v_pull", "g", 1, (), "Assisted pull-up (machine)", compound=True),
    L("0652", "v_pull", "g", 3, (), "Pull-up", compound=True),
    L("1326", "v_pull", "g", 3, (), "Chin-up", compound=True),
    L("0375", "v_pull", "h", 1, ("shoulder",), "Dumbbell pullover", compound=True),
    # rear delts / upper back
    L("0383", "rear_delt", "gh", 1, (), "Dumbbell reverse fly"),
    L("0602", "rear_delt", "g", 1, (), "Reverse fly (machine)"),
    L("0203", "rear_delt", "g", 2, (), "Cable rear-delt row (rope)"),
    # arms
    L("0294", "biceps", "gh", 1, (), "Dumbbell biceps curl"),
    L("0313", "biceps", "gh", 1, (), "Dumbbell hammer curl"),
    L("0031", "biceps", "g", 1, (), "Barbell curl"),
    L("0195", "biceps", "g", 1, (), "Cable preacher curl"),
    L("1769", "biceps", "b", 1, (), "Bodyweight biceps curl (towel / band)"),
    L("0201", "triceps", "g", 1, (), "Cable triceps pushdown", fam="pushdown"),
    L("0200", "triceps", "g", 1, (), "Rope triceps pushdown", fam="pushdown"),
    L("2188", "triceps", "gh", 1, ("shoulder",), "Seated dumbbell triceps extension"),
    L("0333", "triceps", "gh", 1, (), "Dumbbell kickback"),
    L("0129", "triceps", "hb", 1, ("shoulder",), "Bench dip (knees bent)"),
    L("0259", "triceps", "hb", 2, (), "Close-grip push-up"),
    # core
    L("2135", "core_stab", "ghb", 1, (), "Plank (forearms)", mode="hold"),
    L("0276", "core_stab", "ghb", 1, (), "Dead bug"),
    L("3544", "core_stab", "ghb", 1, (), "Side plank (incline)", mode="hold"),
    L("0274", "core_flex", "ghb", 1, ("back",), "Crunch"),
    L("0872", "core_flex", "ghb", 1, (), "Reverse crunch"),
    L("0262", "core_flex", "ghb", 1, ("back",), "Cross-body crunch"),
    L("0620", "core_flex", "g", 2, ("back",), "Lying leg raise (bench)"),
    L("0472", "core_flex", "g", 3, ("back", "shoulder"), "Hanging leg raise"),
    L("0003", "core_flex", "ghb", 2, ("back",), "Bicycle crunch"),
    L("0687", "core_rot", "ghb", 2, ("back",), "Russian twist"),
    # cardio (minutes)
    L("2138", "cardio", "g", 1, (), "Stationary bike", mode="cardio"),
    L("3666", "cardio", "g", 1, (), "Incline treadmill walk", mode="cardio"),
    L("2141", "cardio", "g", 1, (), "Elliptical trainer", mode="cardio"),
    L("2311", "cardio", "g", 2, (), "Stair climber", mode="cardio"),
    L("0630", "cardio_hiit", "ghb", 2, ("impact", "knee", "heavy_bp"), "Mountain climbers", mode="hold"),
    L("3224", "cardio_hiit", "hb", 2, ("impact", "knee", "heavy_bp"), "Jumping jacks", mode="hold"),
    L("3636", "cardio_hiit", "ghb", 1, ("impact",), "High knees (hands on wall)", mode="hold"),
    L("0685", "cardio", "hb", 2, ("impact", "knee"), "Jog / run", mode="cardio"),
    # cool-down stretches (seconds)
    L("1511", "stretch_ham", "ghb", 1, (), "Hamstring stretch", mode="hold"),
    L("0613", "stretch_quad", "ghb", 1, (), "Lying quad stretch", mode="hold"),
    L("1398", "stretch_calf", "ghb", 1, (), "Standing calf stretch", mode="hold"),
    L("1424", "stretch_glute", "ghb", 1, (), "Seated glute stretch", mode="hold"),
    L("1271", "stretch_chest", "ghb", 1, (), "Chest and shoulder stretch", mode="hold"),
    L("1365", "stretch_back", "ghb", 1, (), "Upper back stretch", mode="hold"),
    L("0817", "stretch_tri", "ghb", 1, (), "Triceps stretch", mode="hold"),
    L("1363", "stretch_spine", "ghb", 1, (), "Spine stretch", mode="hold"),
    # warm-up prep moves
    L("1167", "prep_upper", "ghb", 1, (), "Dynamic chest stretch", mode="reps"),
    L("1368", "prep_lower", "ghb", 1, (), "Ankle circles", mode="reps"),
]
BY_ID = {x["id"]: x for x in LIBRARY}

GOALS = {"lose_fat": "Lose fat", "build_strength": "Build strength", "gain_muscle": "Build muscle", "stay_fit": "Stay fit",
         "improve_stamina": "Improve stamina"}
LEVELS = {"beginner": 1, "intermediate": 2, "advanced": 3}
PLACES = {"gym": "g", "home": "h", "bodyweight": "b"}
PLACE_LABEL = {"gym": "Gym", "home": "Home with dumbbells", "bodyweight": "No equipment"}
CONDITIONS = {"knee": "Knee pain or injury", "back": "Lower back pain", "shoulder": "Shoulder pain or injury",
              "high_bp": "High blood pressure", "heart": "Heart condition", "diabetes": "Diabetes", "asthma": "Asthma",
              "pregnant": "Pregnant"}

# muscles trained by each pattern → cool-down stretch
STRETCH_FOR = {"squat": "stretch_quad", "hinge": "stretch_ham", "lunge": "stretch_glute", "quad_iso": "stretch_quad",
               "ham_iso": "stretch_ham", "calf": "stretch_calf", "h_push": "stretch_chest", "chest_iso": "stretch_chest",
               "v_push": "stretch_tri", "lat_raise": "stretch_chest", "h_pull": "stretch_back", "v_pull": "stretch_back",
               "rear_delt": "stretch_back", "biceps": "stretch_chest", "triceps": "stretch_tri", "core_flex": "stretch_spine",
               "core_stab": "stretch_spine", "core_rot": "stretch_spine"}

# coaching cues per pattern (shown with the dataset's own steps)
CUES = {
    "squat": ["Feet about shoulder-width, toes slightly out.", "Knees follow the line of your toes; don't let them cave in.",
              "Keep your chest up and your whole foot on the floor.", "Go as low as you can with a straight back, then stand up through your heels."],
    "hinge": ["Push your hips back; keep a slight bend in the knees.", "Back stays flat, never rounded.",
              "Keep the weight close to your legs.", "Stand tall by squeezing your glutes at the top."],
    "lunge": ["Take a long enough step that both knees bend to about 90°.", "Front knee stays over the middle of the foot.",
              "Keep your upper body upright.", "Push through the front heel to come back."],
    "quad_iso": ["Move slowly and control the weight down.", "Don't lock your knees hard at the top."],
    "ham_iso": ["Hips stay down on the pad.", "Lift and lower slowly; no swinging."],
    "calf": ["Rise as high as you can on your toes.", "Pause at the top, lower all the way down slowly."],
    "h_push": ["Shoulder blades pulled back and down.", "Elbows at about 45° from your body, not flared wide.",
               "Lower with control until the chest (or hands) nearly touch, then press up.", "Keep your body in one straight line for push-ups."],
    "chest_iso": ["Keep a soft bend in the elbows the whole time.", "Open only until you feel a stretch across the chest."],
    "v_push": ["Brace your core; don't arch your lower back.", "Press straight up, finish with arms beside your ears.",
               "Lower to about chin height."],
    "lat_raise": ["Use a light weight.", "Lift out to the side to shoulder height, elbows slightly bent.", "Lower slowly."],
    "h_pull": ["Pull your elbows back towards your hips.", "Squeeze your shoulder blades together at the end.",
               "Don't shrug or swing your body."],
    "v_pull": ["Pull your elbows down towards your ribs.", "Lean back only slightly; chest up.", "Control the way up."],
    "rear_delt": ["Light weight, slow reps.", "Lead with the elbows, squeeze between the shoulder blades."],
    "biceps": ["Elbows stay at your sides.", "No swinging; lower slowly."],
    "triceps": ["Upper arms stay still; only the forearms move.", "Straighten fully, then return slowly."],
    "core_stab": ["Squeeze glutes and stomach; body in one line.", "Breathe normally; don't hold your breath.",
                  "Stop the set when your hips start to sag."],
    "core_flex": ["Move slowly; no jerking your neck.", "Breathe out as you curl up."],
    "core_rot": ["Turn from your ribs, not just your arms.", "Keep your back straight."],
    "cardio": ["Easy-to-moderate pace: you can talk in short sentences.", "Build up slowly in the first 2 minutes."],
    "cardio_hiit": ["Work hard for the set time, then rest.", "Land softly and stay in control."],
}
for _k in ("stretch_ham", "stretch_quad", "stretch_calf", "stretch_glute", "stretch_chest", "stretch_back", "stretch_tri", "stretch_spine"):
    CUES[_k] = ["Ease into the stretch; mild tension, never pain.", "Breathe slowly and relax into it."]
CUES["incline_push"] = CUES["h_push"][:3]
CUES["glute_iso"] = ["Move slowly and feel it in the side of your hip.", "Keep your body still; don't swing."]
STRETCH_FOR["incline_push"] = "stretch_chest"
STRETCH_FOR["glute_iso"] = "stretch_glute"
CUES["prep_upper"] = ["Small, easy movements to warm up the joints."]
CUES["prep_lower"] = CUES["prep_upper"]

# day templates: list of (slot, patterns to choose from in order of preference)
DAYS = {
    "full_a": ("Full body A", [("main_legs", ["squat"]), ("main_push", ["h_push"]), ("main_pull", ["h_pull"]),
                               ("hinge", ["hinge"]), ("shoulders", ["v_push", "lat_raise"]), ("legs2", ["lunge", "calf"]), ("core", ["core_stab"])]),
    "full_b": ("Full body B", [("main_legs", ["hinge"]), ("main_push", ["v_push", "h_push"]), ("main_pull", ["v_pull", "h_pull"]),
                               ("legs2", ["lunge", "squat"]), ("chest", ["h_push", "chest_iso"]), ("arms", ["biceps", "triceps"]), ("core", ["core_flex", "core_stab"])]),
    "full_c": ("Full body C", [("main_legs", ["squat", "lunge"]), ("main_pull", ["h_pull", "v_pull"]), ("main_push", ["h_push"]),
                               ("hinge", ["hinge", "ham_iso"]), ("back2", ["rear_delt", "lat_raise"]), ("arms", ["triceps", "biceps"]), ("core", ["core_rot", "core_stab"])]),
    "upper": ("Upper body", [("main_push", ["h_push"]), ("main_pull", ["h_pull"]), ("v_push", ["v_push"]), ("v_pull", ["v_pull", "h_pull"]),
                             ("shoulders", ["lat_raise", "rear_delt"]), ("biceps", ["biceps"]), ("triceps", ["triceps"])]),
    "lower": ("Lower body", [("main_legs", ["squat"]), ("hinge", ["hinge"]), ("lunge", ["lunge"]), ("ham", ["ham_iso", "glute_iso", "hinge"]),
                             ("quad", ["quad_iso", "lunge", "glute_iso"]), ("calf", ["calf"]), ("core", ["core_stab", "core_flex"])]),
    "push": ("Push: chest, shoulders, triceps", [("main_push", ["h_push"]), ("v_push", ["v_push"]), ("push2", ["incline_push", "h_push"]),
                                                 ("chest", ["chest_iso"]), ("shoulders", ["lat_raise"]), ("triceps", ["triceps"]), ("triceps2", ["triceps"])]),
    "pull": ("Pull: back, biceps", [("v_pull", ["v_pull"]), ("main_pull", ["h_pull"]), ("pull2", ["h_pull", "v_pull"]),
                                    ("rear", ["rear_delt"]), ("biceps", ["biceps"]), ("biceps2", ["biceps"]), ("core", ["core_flex"])]),
    "legs": ("Legs and core", [("main_legs", ["squat"]), ("hinge", ["hinge"]), ("lunge", ["lunge"]), ("quad", ["quad_iso", "squat"]),
                               ("ham", ["ham_iso", "glute_iso"]), ("calf", ["calf"]), ("core", ["core_stab"])]),
}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SCHEDULE = {2: [0, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4], 5: [0, 1, 2, 4, 5], 6: [0, 1, 2, 3, 4, 5]}


def split_for(days: int, level: int, goal: str) -> list[str]:
    if days <= 2:
        return ["full_a", "full_b"]
    if days == 3:
        if level >= 2 and goal in ("gain_muscle", "build_strength"):
            return ["upper", "lower", "full_a"]
        return ["full_a", "full_b", "full_c"]
    if days == 4:
        return ["upper", "lower", "upper", "lower"]
    if days == 5:
        return ["push", "pull", "legs", "upper", "lower"] if level >= 2 else ["upper", "lower", "full_a", "upper", "lower"]
    return ["push", "pull", "legs", "push", "pull", "legs"] if level >= 2 else ["upper", "lower", "full_a", "upper", "lower", "full_b"]


def dose(goal: str, level: int, compound: bool, mode: str, week_in_block: int, careful: bool, bodyweight: bool = False,
         pattern: str | None = None) -> dict:
    """Sets, reps (or seconds), rest and effort for one exercise."""
    if mode == "hold":
        secs = {1: 20, 2: 30, 3: 40}[level] + 5 * min(week_in_block - 1, 2)
        sets = 3 if level >= 2 else 2
        return {"sets": sets if week_in_block < 4 else max(2, sets - 1), "seconds": secs, "rest_sec": 45, "effort": "Steady, good form"}
    if goal == "build_strength" and not careful:
        sets, lo, hi, rest = (4, 5, 6, 150) if compound and level >= 2 else ((3, 6, 8, 120) if compound else (3, 8, 10, 90))
    elif goal == "gain_muscle":
        sets, lo, hi, rest = (4, 8, 10, 90) if compound and level >= 2 else ((3, 8, 12, 75) if compound else (3, 10, 12, 60))
    elif goal == "lose_fat":
        sets, lo, hi, rest = (3, 10, 12, 60) if compound else (3, 12, 15, 45)
    elif goal == "improve_stamina":
        sets, lo, hi, rest = (3, 12, 15, 45) if compound else (2, 15, 20, 30)
    else:  # stay_fit, or careful (blood pressure / heart): moderate effort
        sets, lo, hi, rest = (3, 10, 12, 75) if compound else (3, 12, 15, 60)
    if careful:
        lo, hi, rest = max(lo, 10), max(hi, 15), max(rest, 75)
    if bodyweight and goal in ("build_strength", "gain_muscle") and pattern != "v_pull":  # no weight to add: more reps
        lo, hi, rest = max(lo, 10), max(hi, 15), min(rest, 90)
    if pattern == "v_pull" and bodyweight:  # pull-ups / chin-ups: hard for everyone
        lo, hi = 5, 8
    if level == 1:
        sets = min(sets, 3)
    # 4-week block: week 1 bottom of the range, week 2-3 add reps / a set, week 4 lighter
    if week_in_block == 2:
        lo = min(lo + 1, hi)
    elif week_in_block == 3:
        lo, sets = hi, sets + (1 if level >= 2 and sets < 5 else 0)
    elif week_in_block == 4:
        sets = max(2, sets - 1)
    effort = {"build_strength": "Hard: 1–2 reps left in the tank", "gain_muscle": "Hard: 1–2 reps left in the tank",
              "lose_fat": "Moderately hard: 2–3 reps left", "improve_stamina": "Steady pace, short rests",
              "stay_fit": "Comfortable: 3 reps left"}.get(goal, "Comfortable: 3 reps left")
    if careful:
        effort = "Moderate: you could do 4–5 more reps; breathe out on the effort, never hold your breath"
    if week_in_block == 4:
        effort = "Lighter week: same moves, use about 20% less weight"
    return {"sets": sets, "reps": f"{lo}–{hi}" if lo != hi else str(lo), "rest_sec": rest, "effort": effort}


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def _bmi(p: dict) -> float | None:
    try:
        h = float(p["height_cm"]) / 100
        return round(float(p["weight_kg"]) / (h * h), 1)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def missing_inputs(p: dict) -> list[str]:
    need = ["height_cm", "weight_kg", "age", "goal", "experience", "days_per_week", "session_min", "place"]
    return [k for k in need if p.get(k) in (None, "")]


def candidates(pattern: str, place: str, level: int, avoid: set[str], goal: str | None = None) -> list[dict]:
    pl = PLACES.get(place, "g")
    out = [x for x in LIBRARY if (x["pattern"] == pattern or pattern in x["also"]) and pl in x["places"]
           and x["level"] <= max(level, 1) and not (x["avoid"] & avoid)
           and (goal is None or x["goals"] is None or goal in x["goals"])]
    ds = dataset()
    # in a gym, loaded exercises before bodyweight ones; beginners: easiest first; advanced: hardest first
    out.sort(key=lambda x: (pl == "g" and ds[x["id"]]["equipment"] == "body weight" and pattern not in ("v_pull", "core_stab", "core_flex", "core_rot"),
                            -x["level"] if level >= 3 else x["level"]))
    return out


def risk_profile(p: dict) -> tuple[set[str], list[str], bool]:
    """-> (avoid flags, safety notes to show, careful mode)."""
    cond = set(p.get("conditions") or [])
    avoid, notes = set(), []
    if "knee" in cond:
        avoid |= {"knee", "impact"}
        notes.append("Knee: no jumping, deep lunges or leg extensions. Squat only as low as feels fine; stop if the knee hurts.")
    if "back" in cond:
        avoid.add("back")
        notes.append("Lower back: supported exercises only (machines, cable rows, glute bridges, dead bugs). No heavy deadlifts or sit-ups.")
    if "shoulder" in cond:
        avoid.add("shoulder")
        notes.append("Shoulder: no overhead pressing or dips. Keep weights light on raises and stop if you feel pinching.")
    careful = bool(cond & {"high_bp", "heart"})
    if careful:
        avoid |= {"heavy_bp", "impact"}
        notes.append("Blood pressure / heart: moderate effort only, never hold your breath, and get your doctor's OK before starting. "
                     "Stop at once if you feel chest pain, dizziness or unusual breathlessness.")
    if "diabetes" in cond:
        notes.append("Diabetes: check your sugar before training, carry a quick snack, and train at a similar time each day.")
    if "asthma" in cond:
        notes.append("Asthma: keep your inhaler with you and warm up well; cardio stays easy to moderate.")
    bmi = _bmi(p)
    age = int(p.get("age") or 30)
    if (bmi and bmi >= 30) or age >= 60:
        avoid.add("impact")
        notes.append("Low-impact cardio (bike, elliptical, incline walk) is used instead of running or jumping, to protect your joints.")
    if age >= 60:
        notes.append("Take longer warm-ups and rest; balance and good form matter more than weight.")
    if age < 16:
        avoid.add("heavy_bp")
        notes.append("Under 16: learn the movements with light weights; train with a trainer present.")
    return avoid, notes, careful


HD_MEDIA_VERSION = 3  # realistic Real-ESRGAN x4plus, cropped to the figure


def media_for(ex_id: str | None) -> dict:
    """Media links for one exercise, worked out at read time so switching EXERCISE_MEDIA_BASE on or off applies to saved plans too."""
    base = (settings.exercise_media_base or "").rstrip("/")
    hd = (settings.exercise_hd_base or "").rstrip("/")
    x = dataset().get(ex_id) if ex_id else None
    if not base or not x:
        return {"gif_url": None, "image_url": None, "thumb_url": None, "attribution": None}
    if hd and ex_id in BY_ID:  # every exercise a plan can use has a sharp 3x animated WebP on our own site
        # gif_url: 720px HD animation (detail view); thumb_url: 200px animation for lists
        v = f"?v={HD_MEDIA_VERSION}"  # bump when the files are remade, so phones don't keep old cached copies
        return {"gif_url": f"{hd}/{ex_id}.webp{v}", "image_url": f"{hd}/thumb/{ex_id}.webp{v}", "thumb_url": f"{hd}/thumb/{ex_id}.webp{v}",
                "attribution": MEDIA_ATTRIBUTION}
    return {"gif_url": f"{base}/{x['gif']}", "image_url": f"{base}/{x['image']}", "thumb_url": f"{base}/{x['gif']}", "attribution": MEDIA_ATTRIBUTION}


def _cues_for(pattern, name):
    """Coaching cues for the pattern, leaving out push-up-only tips on presses (and the like)."""
    push = "push" in (name or "").lower()
    return [c for c in CUES.get(pattern, []) if push or "push-up" not in c.lower()]


def exercise_view(ex_id: str, lib: dict | None = None) -> dict:
    """Public view of one exercise: names, muscles, steps, coaching cues and media (only when licensed)."""
    x = dataset()[ex_id]
    lib = lib or BY_ID.get(ex_id) or {}
    media = media_for(ex_id)
    pattern = lib.get("pattern")
    return {"id": ex_id, "name": lib.get("name") or x["name"].capitalize(), "dataset_name": x["name"], "body_part": x["body_part"],
            "target": x["target"], "secondary": x["secondary"], "equipment": x["equipment"], "pattern": pattern,
            "steps": x["steps"]["en"], "steps_hi": x["steps"].get("hi") or [], "cues": _cues_for(pattern, lib.get("name") or x["name"]), **media}


def _pick(pattern_list, place, level, avoid, used, rnd, keep=None, goal=None, prefer_not=frozenset(), skip_patterns=frozenset(),
          used_fams=frozenset()):
    # Keep last week's exercise (incl. one the member swapped in) whenever it is still safe for them: right place, level and
    # health answers, and not already in today's workout. Variety rules only apply to fresh picks.
    if keep and keep not in used:
        for pat in pattern_list:
            if pat in skip_patterns:
                continue
            hit = next((c for c in candidates(pat, place, level, avoid) if c["id"] == keep), None)
            if hit:
                return hit
    for pat in pattern_list:
        if pat in skip_patterns:
            continue
        allc = [c for c in candidates(pat, place, level, avoid, goal) if c["id"] not in used and c["fam"] not in used_fams]
        opts = [c for c in allc if c["id"] not in prefer_not] or allc
        if not opts:
            continue
        if keep and keep in [o["id"] for o in opts]:
            return next(o for o in opts if o["id"] == keep)
        top = opts[:3]  # stay with the best fits; vary among them
        return top[rnd.randrange(len(top))]
    return None


def n_exercises(session_min: int, level: int, cardio_min: int) -> int:
    base = {30: 4, 45: 5, 60: 6, 75: 7, 90: 7}.get(int(session_min), 5)
    if cardio_min >= 15:
        base -= 1
    if level == 1:
        base = min(base, 5)
    return max(3, base)


def build_plan(p: dict, user_key: str, week_start: date, block_no: int = 1, week_in_block: int = 1,
               keep: dict | None = None, variant: int = 0) -> dict:
    """The whole week. `keep` = {day_key: {slot: exercise_id}} to keep the same exercises during a block."""
    if "pregnant" in (p.get("conditions") or []):
        return {"blocked": True, "reason": "During pregnancy please follow a plan from your doctor or a certified prenatal trainer. "
                                            "We don't generate one automatically."}
    goal = p.get("goal") or "stay_fit"
    level = LEVELS.get(p.get("experience") or "beginner", 1)
    days = max(2, min(6, int(p.get("days_per_week") or 3)))
    session_min = int(p.get("session_min") or 45)
    place = p.get("place") or "gym"
    avoid, notes, careful = risk_profile(p)
    cardio_min = {"lose_fat": 15, "improve_stamina": 20, "stay_fit": 10}.get(goal, 0)
    if session_min <= 30:
        cardio_min = min(cardio_min, 10)
    n = n_exercises(session_min, level, cardio_min)
    rnd = random.Random(_seed(user_key, block_no, variant))
    split = split_for(days, level, goal)
    schedule = SCHEDULE[days]
    keep = keep or {}
    out_days, used_week = [], set()
    for i, wd in enumerate(range(7)):
        day = {"index": wd, "weekday": WEEKDAYS[wd], "date": (week_start + timedelta(days=wd)).isoformat()}
        if wd not in schedule:
            day.update({"rest": True, "title": "Rest and recover",
                        "tips": ["A 20–30 minute easy walk helps recovery.", "Sleep 7–9 hours; drink 2–3 litres of water.",
                                 "Gentle stretching for 10 minutes if you feel stiff."]})
            out_days.append(day)
            continue
        key = split[schedule.index(wd)]
        tkey = f"{key}#{schedule.index(wd)}"
        title, slots = DAYS[key]
        used, used_pats = set(), set()
        main = []
        for slot, pats in slots:
            if len(main) >= n:
                break
            want = (keep.get(tkey) or {}).get(slot)
            c = _pick(pats, place, level, avoid, used, rnd, keep=want, goal=goal, prefer_not=frozenset(used_week),
                      skip_patterns=frozenset(used_pats) if key.startswith("full") else frozenset(),
                      used_fams=frozenset(BY_ID[i]["fam"] for i in used))
            if not c:
                continue
            used.add(c["id"])
            used_pats.add(c["pattern"])
            bw = dataset()[c["id"]]["equipment"] == "body weight"
            main.append({"slot": slot, **exercise_view(c["id"], c), "mode": c["mode"],
                         "dose": dose(goal, level, c["compound"], c["mode"], week_in_block, careful, bw, c["pattern"]),
                         "_base": dose(goal, level, c["compound"], c["mode"], 1, careful, bw, c["pattern"])})
        used_week |= used
        patterns = [m["pattern"] for m in main]
        # warm-up: 5 min easy cardio + 2 prep moves for this day
        wu_cardio = next(iter(candidates("cardio", place, 1, avoid | {"impact"})), None)
        warm = [{"name": wu_cardio and BY_ID[wu_cardio["id"]]["name"] or "Brisk walk or marching on the spot", "minutes": 5,
                 "note": "Easy pace, just getting warm", **({"id": wu_cardio["id"]} if wu_cardio else {})}]
        lower = any(pt in ("squat", "hinge", "lunge", "quad_iso", "ham_iso", "calf") for pt in patterns)
        upper = any(pt in ("h_push", "v_push", "h_pull", "v_pull", "lat_raise", "chest_iso", "rear_delt") for pt in patterns)
        if lower:
            warm.append({"id": "3013", "name": BY_ID["3013"]["name"], "reps": "10", "note": "Wakes up the glutes"})
            warm.append({"id": "1368", "name": BY_ID["1368"]["name"], "reps": "10 each way"})
        if upper:
            warm.append({"id": "1167", "name": BY_ID["1167"]["name"], "reps": "10"})
            warm.append({"id": "0659", "name": BY_ID["0659"]["name"], "reps": "10", "note": "Easy, to warm the shoulders"})
        warm.append({"name": "First exercise, light", "note": "Do 1 easy set of your first exercise with about half the weight"})
        # finisher cardio
        finisher = None
        if cardio_min:
            hiit_ok = goal in ("lose_fat", "improve_stamina") and level >= 2 and "impact" not in avoid and not careful
            if hiit_ok and rnd.random() < 0.5:
                c = next(iter(candidates("cardio_hiit", place, level, avoid)), None)
                if c:
                    finisher = {**exercise_view(c["id"], c), "mode": "intervals", "rounds": 8, "work_sec": 30, "rest_sec": 30,
                                "note": "8 rounds: 30 s hard, 30 s easy"}
            if not finisher:
                c = next(iter(candidates("cardio", place, 1, avoid)), None)
                if c:
                    finisher = {**exercise_view(c["id"], c), "mode": "cardio", "minutes": cardio_min,
                                "note": "Steady: you can talk in short sentences"}
                else:
                    finisher = {"name": "Brisk walk", "mode": "cardio", "minutes": cardio_min, "note": "Outside or on the spot"}
        # cool-down: stretches for what was trained (max 4)
        st_keys = []
        for pt in patterns:
            k = STRETCH_FOR.get(pt)
            if k and k not in st_keys:
                st_keys.append(k)
        cool = []
        for k in st_keys[:4]:
            c = next(iter(candidates(k, place, 1, set())), None)
            if c:
                cool.append({**exercise_view(c["id"], c), "seconds": 30, "note": "30 s each side"})
        def _est(k):  # minutes: warm-up 8 + sets × (work + rest) + finisher + cool-down 4
            return 8 + sum((m[k]["sets"] * ((m[k].get("seconds") or 40) + m[k]["rest_sec"])) / 60 for m in main) \
                + ((finisher.get("minutes") or 8) if finisher else 0) + 4
        # trim accessories until the day fits the time chosen; measured on week-1 effort so the block keeps its exercises
        while len(main) > 3 and _est("_base") > session_min + 5:
            main.pop()
        est = _est("dose")
        for m in main:
            m.pop("_base", None)
        day.update({"rest": False, "key": tkey, "title": title, "focus": sorted({m["target"] for m in main})[:5],
                    "minutes": int(round(est / 5) * 5), "warmup": warm, "exercises": main, "finisher": finisher, "cooldown": cool})
        out_days.append(day)
    bmi = _bmi(p)
    return {"blocked": False, "week_start": week_start.isoformat(), "block": block_no, "week_in_block": week_in_block,
            "summary": {"goal": GOALS.get(goal, goal), "level": (p.get("experience") or "beginner").capitalize(), "days": days,
                        "session_min": session_min, "place": PLACE_LABEL.get(place, place), "bmi": bmi,
                        "bmi_label": None if bmi is None else "Underweight" if bmi < 18.5 else "Healthy" if bmi < 25 else "Overweight" if bmi < 30 else "Obese"},
            "week_note": {1: "Week 1 of 4: learn the moves. Pick weights you can lift with good form for the top of the rep range.",
                          2: "Week 2 of 4: same weights, aim for 1 more rep on each set.",
                          3: "Week 3 of 4: hit the top of the rep range? Add a little weight (2–5%).",
                          4: "Week 4 of 4: lighter week. Same moves with about 20% less weight, so your body recovers. A fresh block starts next week."}[week_in_block],
            "safety": notes, "days": out_days,
            "disclaimer": "General guidance, not medical advice. Warm up, use a weight you can control, and stop if anything hurts. "
                          "If you have a medical condition, check with your doctor before starting."}


def keep_map(plan: dict) -> dict:
    return {d["key"]: {e["slot"]: e["id"] for e in d.get("exercises", [])} for d in plan.get("days", []) if not d.get("rest")}


def alternatives(p: dict, pattern: str, exclude: str) -> list[dict]:
    level = LEVELS.get(p.get("experience") or "beginner", 1)
    avoid, _, _ = risk_profile(p)
    return [exercise_view(c["id"], c) for c in candidates(pattern, p.get("place") or "gym", level, avoid) if c["id"] != exclude][:8]


def week_start_of(d: date) -> date:
    return d - timedelta(days=d.weekday())
