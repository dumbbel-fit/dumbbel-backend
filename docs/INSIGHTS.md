# Executive console insights

Seven decision screens for the DUMBBEL.fit team, served from `app/routers/insights.py` under
`/v1/internal/insights/*`. Every endpoint requires a platform admin. Responses are cached for 2 minutes;
pass `refresh=true` to recompute.

| Screen | Endpoint | What it answers |
|---|---|---|
| Today | `GET /actions`, `POST /followups`, `GET /followups`, `DELETE /followups/snooze` | Which gyms should we contact today, and why? |
| Forecast | `GET /forecast?horizon=30\|60\|90`, `GET /forecast/{gymId}` | How much is due to renew, how much will we get, what already lapsed? |
| Retention | `GET /cohorts?kind=members\|gyms&metric=&months=6\|12&gym_id=` | How long do members and gyms stay? |
| Adoption | `GET /adoption` | Which gym uses which part of the product? |
| Benchmarks | `GET /benchmarks` | How does each gym compare with the platform and its city? |
| Impact | `GET /impact?range=` | What did WhatsApp reminders and Explore earn gyms? |
| Demand map | `GET /demand?range=` | Where do people look for a gym, and where do we have none? |
| Revenue ideas | `GET /revenue` | Who are the gyms and members, and what could DUMBBEL.fit charge for? |
| Capacity | `GET /capacity` | How many members and gyms can the current setup carry? |
| Overview strip | `GET /headline` | Three numbers for the top of the overview page. |

## How the numbers are worked out

**Renewal rate.** Of the memberships that ended in the last 180 days, the share followed by another
membership for the same member. Memberships that ended in the last 14 days are left out, because they
may still renew. A gym needs at least 5 ended memberships before its own rate is used; otherwise the
platform rate is used, and the response says so in `rate_basis` (`gym`, `platform` or `assumed`).

**Forecast.** `expected = value of memberships ending in the period × renewal rate`. A membership that
already has a later one is counted as "already renewed", not as due. The value is what the member paid,
or today's plan price when the stored price is ₹0 (imported rows). "New sales" are first-time membership
payments in the last 30 days, scaled to the period. The what-if sliders on the page only re-do this
arithmetic in the browser.

**Lapsed.** Memberships that ended in the last 90 days with no later membership, split into the last
14 days (still winnable) and older.

**Call list.** Each alert has a severity (3 urgent, 2 this week, 1 when free). A gym's score is
`10 × severity` summed over its alerts, plus up to 15 points for the money involved and up to 10 for
size. Snoozing hides one alert or the whole gym until a date; logging a call or message is recorded in
`exec_followups` and in the audit log.

**Cohorts.** Members are grouped by the month they joined. "Still paying" means a membership covers
some part of that calendar month; "still visiting" means at least one check-in. The current month is
marked `partial` and left out of the average. Imported members are excluded unless asked for, because
an import only contains the members who survived.

**Adoption.** 0 = not using, 1 = set up but not used in the last 30 days, 2 = in use.

**Impact.** WhatsApp: memberships sold within 14 days of a renewal reminder that was sent, counted
once each. The "link" numbers are the subset completed through the personal renewal link. Explore:
payments from people who booked a visit and were then added as a member.

**Capacity.** `app/services/metrics.py` counts every request in memory: one bucket per minute for 24 hours,
requests in flight, response times, and totals per route template (never the real path, user or IP). It
starts empty on each deploy and describes one server. The ceiling is
`min(worker threads, database connections, Cloud Run concurrency) × servers ÷ average response time`,
and the safe load is 60% of it. Until 50 requests have been seen, 200 ms is assumed and the response says
`basis: assumed`. Members served = `safe requests per hour ÷ requests per visit ÷ share of members in the
busiest hour`; the share is measured from check-ins, requests per visit (15) is a guess the page lets you
change. Cloud Run does not tell the container its limits, so set `RUN_MAX_INSTANCES` and `RUN_CONCURRENCY`
to match the deploy flags (defaults 1 and 80). `DB_STORAGE_LIMIT_MB` is optional. This is arithmetic, not a
load test. Not cached.

**Revenue ideas.** Each idea is `measured number × price or rate × take-up`. The measured numbers (gyms by
size, UPI money, visits booked, lapsed value, app users) come from the tables; every price and take-up is
a default guess that the page lets you change and keeps in the browser. DUMBBEL.fit has no billing, so
`platform_income_paise` is always 0.

## Demand log and privacy

`explore_events` is written from `GET /v1/discover/gyms` (search) and `GET /v1/discover/gyms/{id}`
(view), off the request path, and never fails the request. Before a location is stored it is rounded
to two decimals (about 1 km). No IP address or device is stored; a short hash of the caller's address
is used only as a 10-minute throttle key in the cache, so panning the map does not inflate demand.
Browsing the full list without a location or search term is not logged. The map is empty until this
release has been live for a while.

## Tables

Created automatically at startup by `app/migrate.py`: `explore_events`, `exec_followups`.

## Tests

```bash
DATABASE_URL=...dumbbel_test AUTH_PROVIDER=2factor OTP_DEV_CODE=123456 APP_ENV=dev \
  uvicorn app.main:app --port 8000 &
DATABASE_URL=...dumbbel_test python tests/insights_e2e.py   # 127 checks
```

The suite refuses to run unless the database name contains `test`.
