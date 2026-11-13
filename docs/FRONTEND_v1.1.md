# Frontend changes for API v1.1

Base URL: `https://dumbbel-api-424586757598.asia-south1.run.app/v1` · Swagger: `/docs` (every response now has a schema).

Errors are always `{ "error": { code, message, details, request_id } }`, including 422 validation errors
(`details.fields`). Show `message`; branch on `code`.

---

## 1. Must change (behaviour changed)

### Invites (single and bulk)
- `POST /gyms/{id}/invites` now **sends immediately** and returns
  `{ batch_id, sent, sent_items[], skipped[{member_id, name, reason, next_allowed_at?}], failed[{member_id, name, reason, whatsapp_url}] }`.
  - Skip reasons: `ALREADY_ON_APP`, `INVITED_RECENTLY`, `ARCHIVED`, `CLAIM_DISPUTED`, `NO_ACTIVE_MEMBERSHIP`, `NOT_FOUND`, `BATCH_LIMIT_500`.
  - Fail reasons: `SMS_NOT_CONFIGURED` (the DLT template isn't set up yet), `SMS_PROVIDER_ERROR`.
  - If **every** send fails, the API returns **502 `UPSTREAM_SMS`**, with the same lists in `error.details`.
- `POST /gyms/{id}/members/{mid}/invite`: `200 {invite, status:"sent"}`, or **502** with
  `details.whatsapp_url` + `details.message`, **409 `ALREADY_ON_APP`**, or **429 `INVITE_TOO_SOON`** `{next_allowed_at}`.
- **UI:** show a result sheet ("12 sent · 3 skipped · 2 failed"). For failed rows, show a **"Share on WhatsApp"** button
  that opens `whatsapp_url`. Until invite SMS templates are approved on DLT, WhatsApp is the way to invite.

### Rate limits
Reads: 600/min. Writes: 120/min. Every 429 has a `Retry-After` header (already handled).

### Summary money
`numbers.collected_month_paise` now **includes** imported payments. Use `collected_month_excl_import_paise` for
"collected through the app this month".

### Permissions instead of owner/staff checks
Login (`staff_of[]`), `GET /me`, and `GET /gyms/{id}` (`me`) now return `staff_role` and **`permissions[]`**.
**Show or hide UI by permission, not by role:**

| Permission | Controls |
|---|---|
| `members.view` / `members.edit` / `members.archive` | member list / add-edit-notes / archive |
| `memberships.manage` / `memberships.cancel` | assign, renew, freeze, suspend / cancel |
| `payments.view` / `payments.record` / `payments.void` | see money / record payments + verify UPI / void |
| `checkins.manage` | scan + manual check-in |
| `imports.manage`, `invites.send` | import screens, invite buttons |
| `plans.manage`, `staff.manage`, `settings.manage` | settings tabs (settings also covers UPI IDs + branches) |
| `activity.view`, `reports.export` | activity log, CSV export buttons |

Without `payments.view`, money keys are **absent** from responses (not `null`). Check with `'due_paise' in item`.
A 403 now carries `details.permission`.

### CORS
Only your configured domain(s) and `localhost` on any port are allowed. Tell the backend about each new frontend domain.

---

## 2. Use the new data (dashboard)

### Home: `GET /gyms/{id}/summary` → `numbers`
`active_members, total_members, checkins_today (unique), visits_today, inside_now (last 90 min), new_members_month,
renewals_month, expiring_7d, expired_count, frozen_count, suspended_count, upcoming_count, no_plan_count, dues_count`
plus, with `payments.view`: `collected_month_paise, collected_month_excl_import_paise, today_collected_paise,
last_week_same_day_paise, last_week_same_time_paise (fair "vs last week" comparison), dues_paise, upi_to_verify`.
`expiring[]` items: `member_id, name, phone, member_code, plan_name, end_date, days_left, status, photo_url, due_paise*`.

### Revenue charts: `GET /gyms/{id}/payments/stats?range=12m&group=month`
`range`: `7d|30d|90d|3m|6m|12m|24m` · `group`: `day|week|month`.
Returns `series[{period, start, collected_paise, count, by_method{cash,upi,card,bank}, by_source{manual,import,app}}]`
and `totals`. **Stop paging through every payment to draw charts.**

### Members list: `GET /gyms/{id}/members`
- `counts` for every tab on every call (`all, active, expiring, expired, frozen, suspended, upcoming, cancelled, no_plan,
  away, not_on_app, archived` + `dues` with payments.view), plus `total` and `filtered_total`.
- Items: `current_membership{id, plan_name, start_date, end_date, status, days_left, due_paise*}`, `last_visit_at`,
  `on_app`, `photo_url`, `joined_at`, `source` (`manual|import|app`), `has_dues`, `due_paise*`.
- Search `q`: name (any case, partial, any word order), member code, or phone (spaces, dashes, `+91` and a leading 0
  are ignored). The frontend doesn't need to pre-clean the query.
- New status `suspended`; new sort `joined`.

### Plans: `GET /gyms/{id}/plans`
Each plan has `active_members`, `effective_price_paise` (= offer price if set), `offer_price_paise`, `joining_fee_paise`,
`gst_mode (inclusive|exclusive)`, `gst_rate`, `facilities[]`, `description`, `freeze_allowed`, `max_freeze_days`,
`cancellation_policy`, `refund_policy`. All are editable via POST/PATCH.
A membership now shows `joining_fee_paise` + `gst_paise` (the joining fee applies only to a member's **first** membership).

### Check-in stats: `GET /gyms/{id}/checkins/stats?range=today|week|month`
Adds `by_result{success, duplicate, expired, frozen, suspended, …}`, `unique_members`, and `by_day[]` (one entry per day).

### Payments list
New `q` filter (member name, phone, UTR/reference, receipt no).

---

## 3. New screens / buttons

### UPI payments (new) — members pay the gym directly, 0% fees
**Settings → Payments → UPI IDs** (`settings.manage`):
`GET/POST/PATCH/DELETE /gyms/{id}/upi-accounts` with `{vpa, payee_name, label, is_default}`.
Recommend a **business/merchant UPI ID** (PhonePe Business, Paytm for Business, bank merchant VPA). Some UPI apps
restrict pre-filled-amount payments to personal UPI IDs.

**Collect at the desk** (`payments.record`): `POST /gyms/{id}/upi-requests {member_id, amount_paise?, membership_id?}`
→ render **`upi_uri` as a QR** (any QR lib, error-correction M). Show the amount + payee name + a countdown to `expires_at`.
Re-posting the same member+amount returns the same open request.

**Member app → "Pay dues by UPI":**
1. `GET /me/upi?member_id=` → `{upi_enabled, payee_name, vpa, due_paise, open_request}`.
2. `POST /me/upi-requests {member_id?, amount_paise?}` → buttons from `app_links` (`gpay, phonepe, paytm, bhim, generic`).
   On Android, `generic` (`upi://pay?...`) opens the UPI app chooser.
3. After paying: `POST /me/upi-requests/{id}/claim {utr}` (12 digits; the UTR is in the UPI app's transaction details).
4. Poll `GET /me/upi-requests/{id}` until `verified` (show `receipt_no`) or `rejected` (show `reject_reason`).

**Verify queue** (`payments.view` to see, `payments.record` to act):
`GET /gyms/{id}/upi-requests?status=claimed` (badge count = `summary.numbers.upi_to_verify`) →
the owner checks the credit in their bank app → `POST …/upi-requests/{rid}/verify {utr?}` (records the payment + receipt
automatically; extra money becomes credit) or `…/reject {reason}`. `409 DUPLICATE_REFERENCE` means the UTR was already used.
UI copy: *"Check that ₹X from {member} with UTR {utr} reached your bank before verifying."* A UTR or screenshot is not proof.

Statuses: `pending → claimed → verified | rejected`; `pending → expired | cancelled`. Expired requests can still be claimed (late payers).

### Roles & staff
- `GET /gyms/{id}/roles` → role presets + permission labels (build the role picker / permission checklist from it).
- `POST /gyms/{id}/staff {name, phone, staff_role: manager|receptionist|trainer|staff, permissions?, can_record_payments?, branch_ids?}`
  → the response has `invite.status` (`sent|failed`) + `invite.whatsapp_url`.
- `PATCH /gyms/{id}/staff/{sid} {staff_role?, permissions?, can_record_payments?, status?}`
- `POST /gyms/{id}/staff/{sid}/resend-invite` (max 3/hour) · list items include `last_active_at`, `last_action`, `branches[]`.

### Branches
- `GET /gyms/{id}/branches` → every branch of the business, with `has_access`. Build the **branch switcher** from this or
  from login `staff_of[]` (group by `org_id`). Switching branch = switching `gymId` in every URL.
- `POST /gyms/{id}/branches {name, area, city, …, copy_plans, copy_settings}` (owner). A new branch starts `pending`
  until the DUMBBEL team approves it. Plans are per branch.

### Membership: suspend / resume
`POST /memberships/{id}/suspend {reason}` · `POST /memberships/{id}/resume` (`memberships.manage`).
Suspend pauses **access** while the days keep running (unlike freeze). Status `suspended`; check-ins return result `suspended`.
Freeze can now return `422 FREEZE_NOT_ALLOWED`, and `FREEZE_LIMIT` uses the plan's `max_freeze_days` when set.

### Member notes history
`GET/POST /gyms/{id}/members/{mid}/notes {body}` · `DELETE …/notes/{noteId}`. Member detail includes the latest 20 as `notes_history`.
Each note: `{id, body, author{id,name}, created_at}`. (The old single `notes` field still works.)

### CSV export
`GET /gyms/{id}/members/export.csv?q=&status=&sort=` · `GET /gyms/{id}/payments/export.csv?from=&to=&method=&status=&source=&q=`
(`reports.export`). Download with the auth header (fetch → blob → save); the files include every matching row.

### Import
- `GET import` → `progress (0–1)`, `processed`, `total`; `columns[].examples` (first 3 values). Show a progress bar while `committing`.
- `POST …/commit` is safe to call twice and resumes a stuck commit.
- `POST …/imports/{iid}/retry-failed` → re-checks failed rows and commits the ones that now pass.
- New optional column **"Paid On" / "Payment Date"**: imported payments keep that date (otherwise the start date).
  Add it to the template download and the mapping dropdown (`paid_on`).

### Console (internal)
`GET/POST /internal/admins {phone, name}` · `DELETE /internal/admins/{userId}`. Non-admins get **403 `NOT_PLATFORM_ADMIN`**.

---

## 4. Config / setup
- `GET /config` now returns `firebase{apiKey, authDomain, projectId, appId, messagingSenderId, storageBucket}`.
  **Initialise Firebase from this** instead of hard-coded `VITE_FIREBASE_*` (keep the env values as a fallback).
- `auth_provider` is still `firebase`: phone login = Firebase SDK → `POST /auth/firebase`.

## 5. Not built yet (needs a product decision)
- Automatic UPI confirmation from bank SMS (YouPI-style Android observer). Today a human verifies each UTR, which is the safe default.
- Payment gateway / online refunds (out of scope for v1).
