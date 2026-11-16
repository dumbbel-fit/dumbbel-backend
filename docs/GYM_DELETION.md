# Gym deletion

Only the gym's **owner** can ask; only a **platform admin** in the console can delete.

1. Owner: Admin app → Gym profile → **Delete gym**. They give a reason and type the gym name.
   `POST /v1/gyms/{gymId}/deletion-request` (also `GET`, and `DELETE` to withdraw). Staff get 403.
   One waiting request per gym. The gym keeps working until it is deleted.
2. Console: **Deletions** (red count in the menu, also in `/internal/attention`). Shows members, active plans,
   payments and a row count per table. **Turn down** (owner sees the reason) or **Delete gym** (type the 3-letter code).
3. Delete (`POST /v1/internal/gym-deletions/{id}/approve`), in one transaction:
   - every row of the gym is copied into `gym_archives` (zlib JSON), then deleted. The tables come from the schema:
     every table with `gym_id`, plus `membership_freezes` and `import_rows`, so new tables are covered automatically;
   - member accounts that never logged in and belong to no other gym are removed. Anyone who uses the app keeps their
     account; the owner keeps theirs and can register a gym again (the code becomes free too);
   - after the commit: photos, logo, documents and import files are deleted unless another gym uses them (a branch
     shares its parent's logo); a QR-linked WhatsApp number is unlinked; the owner gets a push.
4. Archive: console → Deletions → Archive → **Download** (JSON with every row, plus people's names and phones).
   Downloads are written to the audit log. File bytes are not archived, only their keys.

Tests: `tests/gym_deletion_e2e.py` (104 checks, test database only).
