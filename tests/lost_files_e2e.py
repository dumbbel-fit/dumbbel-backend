"""An approved gym whose photos/logo were lost (uploaded before files were stored in the database).
Checks the owner can clean up and re-upload, and nothing anywhere points at a dead file.

Run against a TEST database with the API on :8000 (AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev):
    DATABASE_URL=...dumbbel_test python tests/lost_files_e2e.py
"""
import io, os, random, sys, uuid
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from app.db import engine

if "test" not in (engine.url.database or ""):
    sys.exit("Refusing to run: DATABASE_URL must point at a *test* database.")
B = os.environ.get("API", "http://localhost:8000/v1")
c = httpx.Client(timeout=60)
fails: list[str] = []
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x08" * 64 + bytes.fromhex("ffd9")


def H(t, idem=False):
    h = {"Authorization": f"Bearer {t}", "X-App": "admin", "X-App-Version": "1.0.0"}
    if idem: h["Idempotency-Key"] = str(uuid.uuid4())
    return h
def chk(n, r, code=None):
    ok = r.status_code == code if code else r.status_code < 400
    print("OK  " if ok else "FAIL", r.status_code, n, "" if ok else r.text[:300]); ok or fails.append(n)
    try: return r.json()
    except Exception: return None
def expect(n, got, want):
    ok = got == want; print("OK  " if ok else "FAIL", n, got if ok else f"got {got!r} want {want!r}"); ok or fails.append(n)
rnd = lambda: "9" + "".join(random.choices("0123456789", k=9))
def upload(T, purpose):
    p = c.post(f"{B}/uploads/presign", headers=H(T), json={"purpose": purpose, "content_type": "image/jpeg", "size": len(JPEG)}).json()
    c.put(p["upload_url"], content=JPEG, headers={"Content-Type": "image/jpeg"}); return p["file_key"]

r = c.post(f"{B}/auth/otp/send", json={"phone": rnd(), "app": "web"}).json()
T = c.post(f"{B}/auth/otp/verify", json={"request_id": r["request_id"], "code": "123456"}).json()["access_token"]
G = chk("gym", c.post(f"{B}/gyms", headers=H(T), json={"name": "Lost Files Gym " + rnd()[-4:], "city": "Hyderabad"}))["id"]
c.post(f"{B}/gyms/{G}/plans", headers=H(T), json={"name": "Monthly", "duration_days": 30, "price_paise": 59900})
# simulate the old situation: 3 photos, a logo and a member photo whose files no longer exist
for i in range(3):
    chk("old photo row", c.post(f"{B}/gyms/{G}/photos", headers=H(T), json={"file_key": f"gym_photo/old/lost-{i}.jpg"}), 201)
mem = c.post(f"{B}/gyms/{G}/members", headers=H(T, True), json={"name": "Prasad", "phone": rnd()}).json()["member"]
with engine.begin() as cn:
    cn.execute(text("UPDATE gyms SET status='active', lat=17.45, lng=78.39, logo_url='gym_logo/old/lost.jpg' WHERE id=:g"), {"g": G})
    cn.execute(text("UPDATE users u JOIN gym_members m ON m.user_id=u.id SET u.photo_url='member_photo/old/lost.jpg' WHERE m.id=:m"), {"m": mem["id"]})

print("\n== nothing points at a dead file")
ph = chk("photos", c.get(f"{B}/gyms/{G}/photos", headers=H(T)))
expect("all 3 flagged lost", [p["missing"] for p in ph["items"]], [True, True, True])
v = chk("verification", c.get(f"{B}/gyms/{G}/verification", headers=H(T)))
expect("lost photos don't count", (v["photos"], v["ready_for_review"]), (0, False))
d = chk("explore detail", c.get(f"{B}/discover/gyms/{G}"))
expect("explore: no dead photo or logo", (d["photos"], d["cover_url"], d["logo_url"]), ([], None, None))
lst = chk("explore list", c.get(f"{B}/discover/gyms", params={"lat": 17.45, "lng": 78.39, "radius_km": 2}))
it = [x for x in lst["items"] if x["id"] == G][0]
expect("explore list card: no dead cover", it["cover_url"], None)

print("\n== owner cleans up and re-uploads")
for p in ph["items"]:
    chk("remove lost photo (allowed even though the gym is approved)", c.delete(f"{B}/gyms/{G}/photos/{p['id']}", headers=H(T)), 204)
for i in range(3):
    chk("new photo", c.post(f"{B}/gyms/{G}/photos", headers=H(T), json={"file_key": upload(T, "gym_photo")}), 201)
ph = c.get(f"{B}/gyms/{G}/photos", headers=H(T)).json()
expect("3 real photos", [p["missing"] for p in ph["items"]], [False, False, False])
chk("can't drop below 3 real photos", c.delete(f"{B}/gyms/{G}/photos/{ph['items'][0]['id']}", headers=H(T)), 409)
chk("new logo", c.patch(f"{B}/gyms/{G}", headers=H(T), json={"logo_key": upload(T, "gym_logo")}))
d = c.get(f"{B}/discover/gyms/{G}").json()
expect("explore shows the new photos", (len(d["photos"]), all(c.get(u).status_code == 200 for u in d["photos"])), (3, True))
expect("new logo loads", c.get(d["logo_url"]).status_code, 200)
chk("member photo re-uploaded", c.patch(f"{B}/gyms/{G}/members/{mem['id']}", headers=H(T), json={"photo_key": upload(T, "member_photo")}))
m = c.get(f"{B}/gyms/{G}/members/{mem['id']}", headers=H(T)).json()
m = m.get("member") or m
expect("member photo loads", c.get(m["photo_url"]).status_code, 200)
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
