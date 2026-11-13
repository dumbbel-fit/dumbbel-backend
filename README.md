# DUMBBEL.fit API (v1)

FastAPI + SQLAlchemy + MySQL (Aiven). Implements the full v1 backend spec: OTP login (2Factor), gym setup and approval,
members, memberships, freezes, payments and receipts, QR check-ins, import, invites, staff, insights, member app,
internal console, uploads, push notifications and cron jobs.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in DATABASE_URL, secrets, TWOFACTOR_API_KEY
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Swagger UI: http://localhost:8000/docs (click **Authorize** and paste the `access_token`)
- ReDoc: http://localhost:8000/redoc · OpenAPI JSON: `/openapi.json`
- Tables are created automatically on startup (`AUTO_CREATE_TABLES=true`).
- Platform admin for the console: `.venv/bin/python -m scripts.make_admin 98XXXXXXXX "Name"`
- Run a cron job by hand: `.venv/bin/python -m app.services.scheduled nightly_stats`
  (also `membership_notifications`, `owner_digest`, `cleanup`, `account_deletion`, `sync_membership_states`)

## OTP (2Factor)

`POST /v1/auth/otp/send` generates a code, stores only its argon2 hash, and sends it through
`https://2factor.in/API/V1/{key}/SMS/{phone}/{otp}/Dumbble` (sender DMBL, approved template).
- Set `TWOFACTOR_API_KEY` in `.env`. Without it, the code is written to the server log (dev only).
- `OTP_DEV_CODE=123456` gives a fixed code for local testing. It is ignored when `ENV=production`.
- Invite and gym-status SMS use 2Factor transactional DLT SMS. Set `TWOFACTOR_TPL_*` once those templates
  are approved; until then the messages are logged instead of sent.

## Layout

```
app/main.py            app, middleware (request id, 426 version check, maintenance), Swagger
app/models.py          all 17 spec tables + idempotency_keys (MySQL 8)
app/deps.py            auth, per-gym permissions (owner / staff / payments), idempotency
app/security.py        JWT, rotating refresh tokens, OTP hashing, QR tokens (DBF1 HMAC)
app/services/rules.py  derived status, dues, days left, streaks, visits
app/services/importer.py  CSV/XLS/XLSX parse, auto-map, normalise, classify, batched commit
app/services/scheduled.py cron jobs (APScheduler, IST)
app/routers/*          one file per spec section
```

## v1.1 additions
- **Roles & permissions**: owner · manager · receptionist · trainer · staff, optional explicit permission list
  (`GET /gyms/{id}/roles`). Money fields are removed from responses without `payments.view`.
- **UPI payments**: gyms add their own UPI IDs; members pay straight to the gym (0% fees) via `upi://pay` QR/links,
  submit the UTR, staff verify → payment + receipt recorded. A UTR is a claim, not proof; staff confirm in their bank.
- Branches (`/gyms/{id}/branches`), membership suspend/resume, member notes history, plan offer price / joining fee /
  GST inclusive|exclusive / per-plan freeze & policies, revenue stats (`/payments/stats`), CSV exports,
  import progress / resumable commit / retry-failed, console admin management (`/internal/admins`).
- **No invites.** Members and staff are added by phone number and linked automatically when they log in
  (Firebase OTP) to the member or admin app. Gym approval/rejection reaches the owner as a push notification.
- Every response has a schema in Swagger. Schema changes are applied automatically and additively on startup
  (`app/migrate.py`: new tables, new columns, new enum values; never drops anything).

## Notes and deviations from the spec

- **Python/FastAPI and MySQL** instead of Node/Postgres, as requested. MySQL has no partial indexes, so the three
  partial-unique rules (active plan names, a UTR recorded only once, one owner per gym) use generated columns
  with UNIQUE constraints.
- **Redis is optional.** Without `REDIS_URL`, rate limits, phone locks and the QR replay cache run in memory, which
  only works for a **single worker**. Set `REDIS_URL` before running several workers.
- **Background jobs** run on an in-process thread pool, and cron runs through APScheduler. With several workers,
  set `RUN_SCHEDULER=false` on all but one.
- **Storage:** S3/R2 when `S3_BUCKET` is set, otherwise local `./storage` with signed URLs.
- **Extra columns:** `gym_members.best_streak` (streak cache), `import_rows.overrides` (owner edits survive
  re-mapping), `otp_codes.app` also allows `console`.
- **Status tabs:** the `active` filter on member lists covers active + expiring. `active_members` on the home
  summary also counts frozen members.
- **Encryption at rest** relies on Aiven's disk encryption. Phone numbers are masked in logs.
