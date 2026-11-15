# WhatsApp integration — architecture, setup and operations

## Why Meta's official Cloud API (and not the other three)

| Option | How it connects | Verdict |
|---|---|---|
| whatsapp-web.js | Drives WhatsApp Web in a headless Chrome (Puppeteer) | **Rejected.** Its README: *"WhatsApp does not allow bots or unofficial clients … not guaranteed you will not be blocked."* A gym's real number can be banned. Needs a Chrome per number. |
| Baileys | Reverse-engineered WhatsApp Web WebSocket protocol, QR pairing | **Rejected.** README: *"not affiliated … with WhatsApp"*, *"We discourage … bulk or automated messaging."* Reminders to hundreds of members are exactly that. |
| Evolution API | Node.js gateway (Postgres/MySQL + optional Redis) wrapping Baileys **or** the official Cloud API | **Not needed.** In Cloud API mode it only adds a second service, database and auth layer in front of the same Meta API we call directly. |
| **Meta WhatsApp Cloud API** | Official HTTPS API, approved templates, signed webhooks | **Chosen.** Allowed for commercial and automated messages, delivery/read receipts, no extra service to run. |

So the integration lives inside the existing Python backend. No Node.js service or extra database.

## How it works

```
Gym dashboard ──► FastAPI (/v1/gyms/{id}/whatsapp/…)
                    │  connect / templates / settings / send / history
                    ▼
               wa_messages (outbox, unique dedupe_key per gym)
                    │
   APScheduler ─────┤  every 20 s: process_queue()  ── Graph API POST /{phone_number_id}/messages
   (in the API)     │  hourly :05: run_expiry_automation()  (each gym at its own send hour)
                    ▼
Meta ──► POST /v1/webhooks/whatsapp  (X-Hub-Signature-256 checked) ── delivered / read / failed,
                                                                       STOP replies, template reviews
Member ──► https://dumbbel.fit/renew/<token> ──► /v1/renew/<token>… ──► UPI request ──► gym verifies ──► membership
```

* **Multi-tenant:** one Meta app for the platform; each gym connects its own WhatsApp Business number
  (`gym_whatsapp`, one row per gym). Every gym endpoint goes through the existing gym permission check. Webhooks are
  routed by `phone_number_id`, and a number can only be connected to one gym.
* **Templates only for business-initiated messages.** Reminders are outside the 24-hour service window, so the
  backend never sends free text. Each gym gets 9 default templates (`wa_templates`), edits them, and submits them
  to Meta. Sending is blocked until Meta approves.
* **No duplicates:** each automatic reminder has a dedupe key, such as `expiry:<membership>:<days_left>`, and
  `UNIQUE(gym_id, dedupe_key)` enforces it. Re-running the job, or two instances running it, can't double-send.
  Manual sends use `manual:<event>:<member>:<date>`.
* **Queue and retries:** rows are claimed with `SELECT … FOR UPDATE SKIP LOCKED`. Temporary Meta errors retry after
  1 min, 5 min, 30 min and 2 h; permanent errors fail with Meta's reason. A token error marks the gym's connection
  `error` and pauses sending. Sending is throttled to `WA_SEND_PER_SEC`.
* **Honest status:** `sent` means Meta accepted the message. `delivered`, `read` and `failed` come only from
  webhooks, and status never moves backwards.
* **Renewal links:** `secrets.token_urlsafe(24)`. Only the SHA-256 is stored, the link expires after
  `RENEW_TOKEN_DAYS`, it's bound to one gym and member, it's rate-limited per IP, and it contains no IDs. The page
  shows only the member's first name and the last 4 digits of their phone. Payment uses the existing UPI flow: the
  member enters the UTR, and **the membership is created only when gym staff verify the payment** (or immediately if
  the member's credit covers it). Verified renewals are attributed to the link (`renewal_tokens.used_at`).
* **Consent and opt-out:** the owner confirms member consent when connecting (required for promotional messages
  and announcements). Replying STOP/UNSUBSCRIBE (or START) is handled from webhooks, and staff can switch messages
  off per member. Opted-out members are skipped, and their queued messages are cancelled.
* **Secrets:** access tokens are encrypted with Fernet (`WHATSAPP_ENC_KEY`), never returned by the API, never
  logged, and erased on disconnect. Meta error text is stored, tokens are not.

## Automations (per gym, all switchable)

| Event | Trigger | Default |
|---|---|---|
| Membership expiring | N days before end (default 7, 3, 1, 0), at the gym's send hour | on |
| Membership expired | N days after end (default 1) | on |
| Membership renewed | renewal recorded / UPI renewal verified / renewal via link | on |
| New member welcome | first membership assigned | off |
| Payment received | payment recorded or UPI verified | off |
| Payment pending | members with dues, every N days | off |
| Payment not verified | gym rejects a UPI claim | off |
| Haven't visited in 14 days | once a month (promotional, needs consent) | off |
| Announcement | manual only, to active members (promotional, needs consent) | — |

Members who already renewed are never reminded.

## One-time platform setup (DUMBBEL.fit team)

1. **Meta app:** at developers.facebook.com, create an app of type *Business* and add the **WhatsApp** product.
2. **Webhook:** go to WhatsApp → Configuration → Webhook.
   * Callback URL: `https://dumbbel-api-424586757598.asia-south1.run.app/v1/webhooks/whatsapp`
   * Verify token: any random string; put the same value in `META_WEBHOOK_VERIFY_TOKEN`.
   * Subscribe to the fields **messages**, **message_template_status_update** and **account_update**.
3. **App secret:** copy it from App settings → Basic into `META_APP_SECRET`. It's used to check webhook signatures.
4. **Optional, "Continue with Facebook" button (Embedded Signup):** create an Embedded Signup configuration under
   Facebook Login for Business, then set `META_APP_ID` and `META_ES_CONFIG_ID`. Add `dumbbel.fit` to the app's allowed
   domains. This needs Meta to approve the app for *Tech Provider* access. Until then, gyms use the manual form
   (WABA ID + phone number ID + system-user token).
5. **Business verification and app review** are Meta's processes. Until they're done, the app can only message the
   test numbers listed in the app.

## Environment variables (add to `env.yaml`)

```yaml
META_APP_SECRET: "…"              # required for webhooks
META_WEBHOOK_VERIFY_TOKEN: "…"    # random string, same as in the Meta webhook form
WHATSAPP_ENC_KEY: "…"             # python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
META_APP_ID: "…"                  # optional (embedded signup)
META_ES_CONFIG_ID: "…"            # optional (embedded signup)
RENEW_URL_BASE: "https://dumbbel.fit/renew"
PUBLIC_BASE_URL: "https://dumbbel-api-424586757598.asia-south1.run.app"
```

`WHATSAPP_ENC_KEY` must stay the same once gyms have connected. If it changes, stored tokens can't be decrypted
and gyms have to reconnect. Without it, a key derived from `JWT_SECRET` is used, which is stable but couples the two.

## Deploy

```bash
gcloud config set project dumbbel-e448b
cd ~/dumbbel-backend && git pull
# edit env.yaml (variables above), then:
gcloud run deploy dumbbel-api --source . --region asia-south1 --allow-unauthenticated \
  --env-vars-file env.yaml --memory 1Gi --timeout 300 \
  --min-instances 1 --max-instances 1 --no-cpu-throttling
```

* **Database:** the new tables (`gym_whatsapp`, `wa_templates`, `wa_messages`, `renewal_tokens`) and columns
  (`gym_members.wa_opted_out_at`, `wa_opt_source`, `upi_payment_requests.renewal_token_id`) are created
  automatically at startup, as with every earlier change. The changes only add things, so they're safe to roll back.
* **Worker and scheduler:** they run inside the API instance. `--min-instances 1 --no-cpu-throttling` keeps them
  alive. If you ever scale to more instances, the outbox is still safe (SKIP LOCKED and the dedupe keys), but set
  `RUN_SCHEDULER=false` on all but one instance to avoid redundant cron work.
* **Check after deploy:**
  * `curl "<API>/v1/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=ok"` should print `ok`.
  * Connect a number from Dashboard → WhatsApp, submit the templates, wait for approval, and send one reminder to
    a test number. Check that it reaches **Delivered** in Message history; that proves webhooks work.

## Monitoring

Cloud Run logs, logger `dumbbel.whatsapp`:
* `whatsapp automation queued N message(s)`: the hourly run.
* `whatsapp webhook: {...}`: what each webhook changed.
* `whatsapp send crashed`: the message is re-queued automatically.

Gym-level health shows on Dashboard → WhatsApp (status, last error, failed count, quality rating).

## Rollback

`gcloud run services update-traffic dumbbel-api --region asia-south1 --to-revisions <previous-revision>=100`.
The new tables stay and older code ignores them. To stop all sending immediately without a rollback, disconnect
from each gym's dashboard, or run `UPDATE gym_whatsapp SET status='disconnected'`.

## Tests

`tests/whatsapp_e2e.py` runs the whole flow against a **mock** Graph API (no real messages). It covers connect
(bad token, number already used by another gym, encryption at rest), template validation and submission, bulk send
and progress, duplicate prevention, signed webhooks (forged signature, out-of-order and cross-gym receipts),
isolation between gyms, STOP opt-out, the automatic run three times in a row, temporary and permanent failures,
manual retry, the renewal link (made-up token, plan from another gym, no renewal before verification, single use,
attribution) and disconnect. It refuses to run unless `DATABASE_URL` points at a database whose name contains
"test". A real end-to-end check needs Meta credentials and an allowed test number; that hasn't been run.
