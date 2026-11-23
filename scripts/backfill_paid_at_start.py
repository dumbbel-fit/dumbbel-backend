"""One-off: payments taken together with a plan get the plan's start date (only when the plan started earlier).

Which payments: recorded at the desk (source = manual), the FIRST payment of its plan, saved in the same save as
the plan (within 5 seconds of it), where the plan's start date is before the payment's date (gym's local time).
Time of day is kept; only the date moves. Receipt numbers stay as they are. Nothing is deleted.
Not touched: dues paid later on their own, UPI from the member app, imports, plans that start today or later.

Every changed row's old date is saved first in the table payment_paid_at_backup, so it can be undone.

  python scripts/backfill_paid_at_start.py            # preview only, changes nothing
  python scripts/backfill_paid_at_start.py --apply    # save old dates, then update
  python scripts/backfill_paid_at_start.py --undo     # put the saved old dates back
"""
import os
import sys
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.utils import tz  # noqa: E402

FIND = """
SELECT p.id, g.name gym, g.timezone, gm.display_name member_name, p.receipt_no, p.amount_paise, p.paid_at,
       m.start_date, m.plan_name
FROM payments p
JOIN memberships m ON m.id = p.membership_id
JOIN gyms g ON g.id = p.gym_id
JOIN gym_members gm ON gm.id = p.member_id
WHERE p.source = 'manual'
  AND p.created_at BETWEEN m.created_at - INTERVAL 1 SECOND AND m.created_at + INTERVAL 5 SECOND
  AND NOT EXISTS (SELECT 1 FROM payments e WHERE e.membership_id = p.membership_id AND e.member_id = p.member_id
                  AND e.created_at < p.created_at)
  AND m.start_date < DATE(p.paid_at + INTERVAL 14 HOUR)   -- quick filter; the exact local-date check is below
  AND p.id NOT IN (SELECT payment_id FROM payment_paid_at_backup)
ORDER BY g.name, p.paid_at"""


def ensure_backup_table(cn):
    cn.execute(text("""CREATE TABLE IF NOT EXISTS payment_paid_at_backup (
        payment_id VARCHAR(36) PRIMARY KEY, old_paid_at DATETIME(6) NOT NULL, new_paid_at DATETIME(6) NOT NULL,
        changed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6))"""))


def find(cn):
    todo = []
    for r in cn.execute(text(FIND)).mappings():
        old = r["paid_at"].replace(tzinfo=None)  # stored as UTC
        local_day = old.replace(tzinfo=timezone.utc).astimezone(tz(r["timezone"])).date()
        days = (local_day - r["start_date"]).days
        if days > 0:
            todo.append((r, old, old - timedelta(days=days), local_day))
    return todo


def show(todo):
    gym = None
    for r, _, _, day in todo:
        if r["gym"] != gym:
            gym = r["gym"]
            print(f"\n== {gym}: {sum(1 for x in todo if x[0]['gym'] == gym)} payment(s)")
        print(f"  {r['receipt_no']:<22} {r['member_name'][:24]:<24} Rs {r['amount_paise'] / 100:>9,.0f}  "
              f"{(r['plan_name'] or '')[:18]:<18} paid {day:%d %b %Y} -> {r['start_date']:%d %b %Y}")
    print(f"\nTotal: {len(todo)} payment(s) to move to their plan's start date.")


def main(mode):
    print("Database:", engine.url.host, "/", engine.url.database, "| mode:", mode)
    with engine.begin() as cn:
        ensure_backup_table(cn)
    if mode == "--undo":
        with engine.begin() as cn:
            n = cn.execute(text("""UPDATE payments p JOIN payment_paid_at_backup b ON b.payment_id = p.id
                                   SET p.paid_at = b.old_paid_at WHERE p.paid_at = b.new_paid_at""")).rowcount
            cn.execute(text("DELETE FROM payment_paid_at_backup"))  # the backup notes only; payments are never deleted
        print(f"Put back the old date on {n} payment(s).")
        return
    with engine.begin() as cn:
        todo = find(cn)
        show(todo)
        if mode == "--preview":
            print("Preview only. Nothing changed. Run with --apply to update.")
            return
        for r, old, new, _ in todo:
            cn.execute(text("INSERT INTO payment_paid_at_backup (payment_id, old_paid_at, new_paid_at) VALUES (:i, :o, :n)"),
                       {"i": r["id"], "o": old, "n": new})
            cn.execute(text("UPDATE payments SET paid_at = :n WHERE id = :i AND paid_at = :o"),
                       {"i": r["id"], "o": old, "n": new})
    print(f"Updated {len(todo)} payment(s). Old dates saved in payment_paid_at_backup (undo with --undo).")


if __name__ == "__main__":
    mode = (sys.argv[1:] or ["--preview"])[0]
    if mode not in ("--preview", "--apply", "--undo"):
        sys.exit(__doc__)
    main(mode)
