"""Nutrition targets (Mifflin–St Jeor) and AI food estimates. General wellness guidance, not medical advice."""
from datetime import date

from app.services import ai

GOALS = {  # kcal adjustment, protein g/kg
    "lose_fat": (-400, 1.8), "build_strength": (150, 1.6), "gain_muscle": (300, 1.8),
    "stay_fit": (0, 1.2), "improve_stamina": (0, 1.4),
}
ACTIVITY = {"low": 1.375, "moderate": 1.55, "high": 1.725}  # includes their gym training


def age_of(profile: dict, dob: date | None) -> int | None:
    if profile.get("age"):
        return int(profile["age"])
    if dob:
        t = date.today()
        return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))
    return None


def targets(profile: dict | None, gender: str | None, dob: date | None) -> dict:
    p = profile or {}
    w, h, a = p.get("weight_kg"), p.get("height_cm"), age_of(p, dob)
    complete = bool(w and h and a)
    w, h, a = float(w or 65), float(h or 165), int(a or 28)
    sex = {"male": 5, "female": -161}.get(gender or "", -78)
    bmr = 10 * w + 6.25 * h - 5 * a + sex
    tdee = bmr * ACTIVITY.get(p.get("activity") or "moderate", 1.55)
    adj, pkg = GOALS.get(p.get("goal") or "stay_fit", (0, 1.2))
    kcal = int(round((tdee + adj) / 10) * 10)
    protein = int(round(w * pkg / 5) * 5)
    fat = int(round(kcal * 0.27 / 9))
    carbs = max(0, int(round((kcal - protein * 4 - fat * 9) / 4)))
    return {"kcal": kcal, "protein_g": protein, "carbs_g": carbs, "fat_g": fat,
            "water_l": round(w * 0.035 + 0.5, 1), "profile_complete": complete,
            "basis": f"{a} y, {int(h)} cm, {w:g} kg, {(p.get('activity') or 'moderate')} activity, "
                     f"goal: {(p.get('goal') or 'stay_fit').replace('_', ' ')}"}


def estimate_food(text: str, profile: dict | None) -> dict:
    """AI estimate of a free-text meal → {items, kcal, protein_g, carbs_g, fat_g}. Raises ai.AiUnavailable."""
    diet = (profile or {}).get("diet")
    data = ai.chat_json([
        {"role": "system", "content":
            "You estimate nutrition for meals eaten in India (home food, restaurant, street food). Use realistic "
            "Indian household portions (1 roti ≈ 40 g atta, 1 dosa ≈ 120 kcal plain, 1 bowl ≈ 150 g, 1 cup ≈ 200 ml) "
            "unless quantities are given. Reply ONLY with JSON: {\"items\":[{\"name\":string,\"qty\":string,"
            "\"kcal\":integer,\"protein_g\":number}],\"kcal\":integer,\"protein_g\":number,\"carbs_g\":number,"
            "\"fat_g\":number}. Totals must equal the sum of items." + (f" The person eats: {diet}." if diet else "")},
        {"role": "user", "content": text[:300]}], max_tokens=1200, temperature=0.2)
    def num(v, cast=float):
        try:
            return max(0, cast(round(float(v), 1)))
        except Exception:
            return 0
    items = [{"name": str(i.get("name", ""))[:60], "qty": str(i.get("qty", ""))[:30], "kcal": num(i.get("kcal"), int),
              "protein_g": num(i.get("protein_g"))} for i in (data.get("items") or [])[:12] if isinstance(i, dict)]
    return {"items": items, "kcal": num(data.get("kcal"), int), "protein_g": num(data.get("protein_g")),
            "carbs_g": num(data.get("carbs_g")), "fat_g": num(data.get("fat_g"))}
