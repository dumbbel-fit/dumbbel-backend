"""Test gyms: gyms the DUMBBEL.fit team marks as "test" in the console (demo / QA gyms). Nothing about them is
changed or deleted, and the gym itself keeps working; platform-wide numbers in the console just leave them out.

Console SQL is scoped by rewriting `FROM <table>` / `JOIN <table>` into a filtered derived table, so every
aggregate query skips test gyms without each query having to remember the rule. Queries about one specific gym
(they bind `:g`) are left alone, so a test gym's own pages still show its data."""
import hashlib
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

TEST_IDS = "(SELECT tg.id FROM gyms tg WHERE tg.is_test = 1)"
NOT_TEST = f"(gym_id IS NULL OR gym_id NOT IN {TEST_IDS})"

# tables whose rows belong to a gym (gym_id); audit / deletion / follow-up history is deliberately not scoped
GYM_TABLES = ("gym_members", "memberships", "payments", "checkins", "gym_staff", "plans", "gym_upi_accounts",
              "upi_payment_requests", "gym_documents", "gym_photos", "gym_visits", "gym_whatsapp", "wa_messages",
              "wa_templates", "renewal_tokens", "credit_applications", "imports", "explore_events", "invites",
              "member_notes")

# people who only exist because of test gyms: every gym they belong to (as member or staff) is a test gym
TEST_ONLY_USERS = (f"(SELECT x.user_id FROM (SELECT user_id, gym_id FROM gym_members UNION ALL "
                   f"SELECT user_id, gym_id FROM gym_staff) x GROUP BY x.user_id "
                   f"HAVING SUM(x.gym_id NOT IN {TEST_IDS}) = 0)")

FILTERS = {t: f"(SELECT * FROM {t} WHERE {NOT_TEST})" for t in GYM_TABLES}
FILTERS["gyms"] = "(SELECT * FROM gyms WHERE is_test = 0)"
FILTERS["users"] = f"(SELECT * FROM users WHERE id NOT IN {TEST_ONLY_USERS})"

_KEYWORDS = ("WHERE", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "STRAIGHT_JOIN", "GROUP", "ORDER",
             "LIMIT", "HAVING", "UNION", "USING", "SET", "WINDOW", "FOR", "AND", "OR", "NATURAL", "FORCE", "USE",
             "IGNORE", "LATERAL")
_PATTERN = re.compile(
    r"\b(FROM|JOIN)\s+(" + "|".join(sorted(FILTERS, key=len, reverse=True)) + r")\b(?!\s*\()(?!\.)"
    r"(?:(\s+)(?:AS\s+)?(?!(?:" + "|".join(_KEYWORDS) + r")\b)([A-Za-z_]\w*))?",
    re.IGNORECASE)


def _sub(m: re.Match) -> str:
    kw, table, alias = m.group(1), m.group(2).lower(), m.group(4)
    return f"{kw} {FILTERS[table]} {alias or table}"


def scope(sql: str, params: dict | None = None) -> str:
    """Leave test gyms out of a console query. Queries for one gym (`:g` bound) are returned unchanged."""
    if params and params.get("g"):
        return sql
    return _PATTERN.sub(_sub, sql)


def test_gym_ids(db: Session) -> list[str]:
    return list(db.execute(text("SELECT id FROM gyms WHERE is_test = 1 ORDER BY id")).scalars())


def cache_tag(db: Session) -> str:
    """Changes whenever the set of test gyms changes, so cached console numbers never mix the two."""
    ids = test_gym_ids(db)
    return hashlib.sha1(",".join(ids).encode()).hexdigest()[:10] if ids else "0"
