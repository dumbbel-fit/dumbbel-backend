# WhatsApp integration — architecture, setup and operations

Gyms can send reminders, renewal links and updates in two ways. Each gym picks one in Dashboard → WhatsApp.

| | **Link by QR (default, free)** | **Official WhatsApp Business API** |
|---|---|---|
| How | Gym scans a QR in WhatsApp → Linked devices (like WhatsApp Web) | Meta Business account, approved templates |
| Built on | [Evolution API](https://github.com/EvolutionAPI/evolution-api) 2.3.7 → [Baileys](https://github.com/WhiskeySockets/Baileys) | Meta Cloud API, called directly |
| Cost | Free (one small always-on server for the whole platform) | Meta charges per message |
| Setup per gym | Scan a QR (or type an 8-character code) | WABA ID + phone number ID + system-user token |
| Number | Any normal WhatsApp or WhatsApp Business app number | A number registered on the Business Platform |
| Message approval | None, plain text | Every template is reviewed by Meta |
| Risk | Unofficial: WhatsApp can restrict numbers that send automated messages | None |

**Why Evolution API and not whatsapp-web.js directly:** whatsapp-web.js runs a headless Chrome for every connected
number, roughly 150–300 MB each. Baileys speaks the WhatsApp Web protocol over a WebSocket with no browser.
Evolution API wraps Baileys with what we need: one instance per gym, QR and pairing codes, sessions stored in
PostgreSQL so a restart doesn't unlink anyone, a REST API and webhooks. It's maintained, Apache-2.0 licensed, and
runs as one container.

**Keeping linked numbers safe.** The backend enforces these; they can't be turned off from the dashboard:
* Messages go only to the gym's own members. Promotional messages need the owner's consent confirmation.
* Every member can reply STOP (or use the link on the renewal page) and is skipped from then on.
* Messages are sent one at a time per number, with a random **12–35 s** gap (`WA_WEB_MIN/MAX_GAP_SEC`). The gap is
  stored in the database, so several workers can't burst one number.
* At most **200 messages per number per day** (`WA_WEB_DAILY_LIMIT`); the rest wait until the next morning.
* No automatic messages between **21:00 and 08:00** gym time (`WA_WEB_QUIET_START/END`). Manual sends still go out.
* Each message is personal (name, plan, date, own link), so there are no identical bulk blasts.
* Numbers that aren't on WhatsApp fail straight away with a clear reason instead of being retried.

The owner confirms they understand the risk before linking.

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

### "Your gym is on DUMBBEL.fit" (`gym_live`, on by default)

Sent once to each member, with the member app link (`MEMBER_APP_URL`, default `https://dumbbel.fit/member`).

- **Who:** active members whose membership is current or ended in the last 30 days, with a valid number, not opted out.
- **When it is queued:** when WhatsApp is linked, when an import finishes, straight away when a member is added by hand or given a plan, and every hour as a safety net.
- **Once only:** the dedupe key `live:<member id>` allows one per member, ever, even after re-linking.
- **Pace on a linked number:** one message every 12 to 35 seconds, none between 21:00 and 08:00, and at most 75% of
  the daily limit (`WA_WEB_DAILY_LIMIT`, default 200), so reminders always have room. Reminders are sent first.
  A gym with 400 members therefore takes about three days.
- **Official API:** waits until the `gym_live` template is approved by WhatsApp.

Test: `tests/wa_onboarding_test.py` (test database, fake gateway).

## Set up QR linking (Evolution API) — one server for all gyms

A linked WhatsApp has to stay connected all the time, so Evolution runs on a small always-on VM, not Cloud Run. The
Google Cloud **e2-micro** VM is in the always-free tier in us-central1, us-west1 and us-east1. Google may charge a
little for its public IPv4 address, so check the billing page. Oracle Cloud's Always Free VMs are a fully free
alternative; the same `setup-vm.sh` works there.

```bash
# 1) VM (Cloud Shell)
gcloud config set project dumbbel-e448b
gcloud compute instances create dumbbel-wa --zone us-central1-a --machine-type e2-micro \
  --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB --boot-disk-type pd-standard \
  --tags dumbbel-wa
gcloud compute firewall-rules create dumbbel-wa-web --allow tcp:80,tcp:443 --target-tags dumbbel-wa
gcloud compute instances describe dumbbel-wa --zone us-central1-a --format='get(networkInterfaces[0].accessConfigs[0].natIP)'

# 2) DNS (Hostinger → DNS zone for dumbbel.fit): add an A record   wa  →  <the IP from step 1>

# 3) Install Evolution API + PostgreSQL + HTTPS on the VM
gcloud compute ssh dumbbel-wa --zone us-central1-a -- \
  'curl -fsSL https://raw.githubusercontent.com/dumbbel-fit/dumbbel-backend/main/deploy/evolution/setup-vm.sh | sudo WA_DOMAIN=wa.dumbbel.fit bash'
```

The last command prints `EVOLUTION_API_URL` and `EVOLUTION_API_KEY`. Add both to the backend `env.yaml` and redeploy.
The **QR link** option then appears for every gym.

* Upgrade: `sudo docker compose -f /opt/dumbbel-wa/docker-compose.yml pull && sudo docker compose -f /opt/dumbbel-wa/docker-compose.yml up -d`
  (change the image tag in `docker-compose.yml` first). Linked sessions survive restarts because they're stored
  in PostgreSQL.
* Logs: `sudo docker compose -f /opt/dumbbel-wa/docker-compose.yml logs -f evolution`
* Back up `/opt/dumbbel-wa/.env` somewhere safe. A new API key means updating the backend; the sessions themselves
  live in the `pgdata` volume.
* Evolution's built-in web manager is blocked by Caddy, and every API call needs the key. Only the DUMBBEL backend
  holds it.

## One-time platform setup for the official API (optional)

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
# QR linking (Evolution API)
EVOLUTION_API_URL: "https://wa.dumbbel.fit"
EVOLUTION_API_KEY: "…"            # printed by setup-vm.sh
# optional tuning (defaults shown)
# WA_WEB_DAILY_LIMIT: "200"
# WA_WEB_MIN_GAP_SEC: "12"
# WA_WEB_MAX_GAP_SEC: "35"
# WA_WEB_QUIET_START: "21"
# WA_WEB_QUIET_END: "8"

# Official API (only if you also offer it)
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

## Linked numbers stay linked (drops vs. real unlinks)

A linked WhatsApp drops its connection now and then (phone off Wi-Fi, WhatsApp restarting, a timeout). Evolution
reconnects with the saved session by itself; **no new scan is needed**. So:

* A `close` webhook with any reason except a logout (401 / 402 / 403 / 406) keeps the gym **connected** and sets
  `gym_whatsapp.link_lost_at`. The dashboard shows "Reconnecting", and messages wait instead of being cancelled.
* A send that hits a closed socket is re-queued in 2 minutes (no attempt used), never cancelled.
* `whatsapp.links` (every 2 min, `watch_links()`) asks Evolution for each linked number's state. If it's open the
  gym goes back to plain connected and anything that waited is queued again. If it's closed it asks Evolution to
  reconnect with the saved session. Only a session that no longer exists, or one **still down after 15 minutes**,
  is shown as "needs linking again". If Evolution can't be reached, nothing is changed.
* A gym shown as needing a scan comes back by itself (no scan) if its session reconnects later.
* "Link again" on a connected gym never logs out or deletes its session; it only reconnects it.
* A logout on the phone (Linked devices → Log out) or a WhatsApp ban is still shown straight away. The server log
  line `wa link <gym>: state=close reason=401` says WhatsApp ended the session itself.

## Rollback

`gcloud run services update-traffic dumbbel-api --region asia-south1 --to-revisions <previous-revision>=100`.
The new tables stay and older code ignores them. To stop all sending immediately without a rollback, disconnect
from each gym's dashboard, or run `UPDATE gym_whatsapp SET status='disconnected'`.

## Tests

`tests/whatsapp_web_e2e.py` tests QR linking against a **mock** Evolution server. It covers linking by QR and by
pairing code, re-linking, webhook token checks, sending with spacing, the personal link in the text, delivered/read
receipts with no downgrade, STOP replies, group messages ignored, "not on WhatsApp" failures, unlinking from the phone
and relinking, quiet hours, the daily cap, and disconnecting. `tests/wa_link_drops_e2e.py` covers drops, reconnects, the 15-minute grace, Evolution being
unreachable, and that "Link again" never removes a working session. The QR flow was also checked against a **real**
Evolution API 2.3.7 running locally: it returned a real WhatsApp QR, and Evolution's webhook calls reached the
backend and passed the token check. Actually scanning with a phone and sending a real message hasn't been done.

`tests/whatsapp_e2e.py` runs the whole flow against a **mock** Graph API (no real messages). It covers connect
(bad token, number already used by another gym, encryption at rest), template validation and submission, bulk send
and progress, duplicate prevention, signed webhooks (forged signature, out-of-order and cross-gym receipts),
isolation between gyms, STOP opt-out, the automatic run three times in a row, temporary and permanent failures,
manual retry, the renewal link (made-up token, plan from another gym, no renewal before verification, single use,
attribution) and disconnect. It refuses to run unless `DATABASE_URL` points at a database whose name contains
"test". A real end-to-end check needs Meta credentials and an allowed test number; that hasn't been run.
