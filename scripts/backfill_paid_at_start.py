"""One-off: payments taken together with a plan get the plan's start date (only when the plan started earlier).

Which payments: recorded at the desk (source = manual), the FIRST payment of its plan, saved in the same save as
the plan (within 5 seconds of it), where the plan's start date is before the payment's date (gym's local time).
Time of day is kept; only the date moves. Receipt numbers stay as they are. Nothing is deleted.
Not touched: dues paid later on their own, UPI from the member app, imports, plans that start today or later.

Every changed row's old date is saved first in the table payment_paid_at_backup, so it can be undone.

  python scripts/backfill_paid_at_start.py            # preview only, changes nothing
  python scripts/backfill_paid_at_start.py --apply    # save old dates, then update
  python scripts/backfill_paid_at_start.py --undo     # put the saved old dates back

Safety: a payment that would move more than 60 days back is left as it is and listed under "Left as is" (an old
start date is usually a typo or an old member re-added, and moving it would shift revenue into an earlier month/year).
  --max-days 400                  # allow bigger moves
  --skip BSK/2026-27/000123,...   # leave these receipts as they are
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


def find(cn, max_days=60, skip=frozenset()):
    todo, held = [], []
    for r in cn.execute(text(FIND)).mappings():
        old = r["paid_at"].replace(tzinfo=None)  # stored as UTC
        local_day = old.replace(tzinfo=timezone.utc).astimezone(tz(r["timezone"])).date()
        days = (local_day - r["start_date"]).days
        if days > 0:
            row = (r, old, old - timedelta(days=days), local_day)
            if r["receipt_no"] in skip:
                held.append((row, "skipped by you"))
            elif days > max_days:
                held.append((row, f"would move {days} days back"))
            else:
                todo.append(row)
    return todo, held


def show(todo):
    gym = None
    for r, _, _, day in todo:
        if r["gym"] != gym:
            gym = r["gym"]
            print(f"\n== {gym}: {sum(1 for x in todo if x[0]['gym'] == gym)} payment(s)")
        print(f"  {r['receipt_no']:<22} {r['member_name'][:24]:<24} Rs {r['amount_paise'] / 100:>9,.0f}  "
              f"{(r['plan_name'] or '')[:18]:<18} paid {day:%d %b %Y} -> {r['start_date']:%d %b %Y}")
    print(f"\nTotal: {len(todo)} payment(s) to move to their plan's start date.")


def show_held(held):
    if not held:
        return
    print(f"\nLeft as is ({len(held)}), please check these by hand:")
    for (r, _, _, day), why in held:
        print(f"  {r['receipt_no']:<22} {r['gym'][:16]:<16} {r['member_name'][:20]:<20} paid {day:%d %b %Y}, "
              f"plan starts {r['start_date']:%d %b %Y} ({why})")


def main(mode, max_days=60, skip=frozenset()):
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
        todo, held = find(cn, max_days, skip)
        show(todo)
        show_held(held)
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
    args = sys.argv[1:]
    mode, max_days, skip = "--preview", 60, set()
    while args:
        a = args.pop(0)
        if a in ("--preview", "--apply", "--undo"):
            mode = a
        elif a == "--max-days" and args:
            max_days = int(args.pop(0))
        elif a == "--skip" and args:
            skip |= {x.strip() for x in args.pop(0).split(",") if x.strip()}
        else:
            sys.exit(__doc__)
    main(mode, max_days, frozenset(skip))
