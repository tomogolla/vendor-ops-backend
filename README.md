# Vendor Ops API

Django + Django REST Framework + PostgreSQL backend for The Good Flea Vendor Ops.
Full setup, architecture and deployment notes live in the frontend repository's README
(`vendor-ops-frontend/README.md`). Quick start:

```sh
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then edit DATABASE_URL and DJANGO_SECRET_KEY
python manage.py migrate
python manage.py seed_dev         # demo users, weekends and vendors, DEBUG only
python manage.py runserver 8000
python manage.py test
```

Apps: `accounts` (users and roles), `communications` (email; see "Email" below), `catalog` (market weekends, booth variants, add-ons),
`vendors` (vendors, applications and their lines, intake events, notes, activity timeline),
`tasks` (tasks, business-hours scheduling, call logging), `imports` (CSV).
Business rules live in services (`vendors/services.py`, `vendors/pipeline.py`,
`catalog/services.py`, `tasks/services.py`, `imports/services.py`); views stay thin. Every state
change writes an append-only `ActivityEvent`.

Money is stored as integer cents (`*_cents`). `settings.CURRENCY` (`USD`) is for display only.

## Sales pipeline (v1.3)

The pipeline stage lives on the **Application** (the order), not on the vendor.

```
NEW -> IN_REVIEW -> AWAITING_PAYMENT -> WON
  \        \
   +--------+--> DECLINED / WITHDRAWN   (provisional; either can be reopened to IN_REVIEW)
```

- Administrators move NEW -> IN_REVIEW, DECLINED or WITHDRAWN; IN_REVIEW -> DECLINED or WITHDRAWN;
  DECLINED/WITHDRAWN -> IN_REVIEW (`POST /applications/{id}/stage/`).
- AWAITING_PAYMENT and WON are reached only through mark-billed and mark-first-payment, and nothing
  leaves them by hand (cancellation and refunds are not designed yet).
- Declining or withdrawing cancels the application's open call and review tasks, cancels its
  requested and waitlisted lines, and releases its confirmed lines.
- Every change updates `stage_entered_at` and records `APPLICATION_STAGE_CHANGED`.

### Entry paths and automation

- Every entry path goes through `submit_application`: `POST /applications/` (+Add vendor, with vendor
  lookup or create), `POST /vendors/` and the CSV import. A new application starts at NEW with a
  discovery call task. If an earlier call reached the vendor (outcome Interested), it starts at
  IN_REVIEW with a review task and no call.
- **Bundling (proposed rule):** if the vendor already has an application at NEW or IN_REVIEW, the
  submission's lines and notes are added to it instead (`APPLICATION_BUNDLED`, no new tasks; the API
  answers 200 with `bundled: true`). Once the open application is AWAITING_PAYMENT or WON, a new
  application is created.
- Review tasks are created when an application enters IN_REVIEW (one open per application).
- Logging any call attempt on the application's call task moves a NEW application to IN_REVIEW
  (a system action, allowed for team members). After the fifth No Answer the call closes, the stage
  is left alone and `call_exhausted` is true.
- CSV rows are all applicants. Matched rows attach a new application (or bundle) under the existing
  vendor. The already-contacted, inactive and do-not-contact flags still suppress the call task.

### Lines

An application has lines: a booth variant, or an add-on that belongs to a booth line of the same weekend.

- Status: REQUESTED -> CONFIRMED | WAITLISTED | DECLINED | CANCELLED; WAITLISTED -> CONFIRMED |
  DECLINED | CANCELLED; CONFIRMED -> CANCELLED ("release hold", reason required). DECLINED and
  CANCELLED are terminal.
- `unit_price_cents` is copied from the product when the line is added and never recalculated.
- Lines can be added while the application is NEW, IN_REVIEW or WON and the weekend is not CLOSED.
  A second open line for the same product on the same application is **rejected**; change the
  quantity of the open line instead.
- Any signed-in user can add lines and edit or delete REQUESTED lines. Administrators confirm,
  waitlist, decline and release.
- An add-on line can be confirmed only when its booth line is CONFIRMED. Closing a booth line
  (decline, release, cancel) closes its add-on lines.
- Release is refused once the application has a first payment ("Refund rules are not implemented yet").

### Capacity

Only CONFIRMED lines consume capacity, and they hold it until released.
`remaining = capacity - sum(quantity of CONFIRMED lines)`; add-ons with no capacity are unlimited.
Confirming locks the variant or add-on row (`select_for_update`) and refuses with the remaining
number when the line does not fit (no override; waitlist instead). Capacity cannot be lowered below
what is confirmed, and products with lines cannot be deleted. The waitlist is ordered by
`waitlisted_at`; weekend detail lists the oldest waitlisted lines of products with room as
**suggestions** (nothing is promoted automatically).

### Totals and discounts

Each application reports `requested_total_cents` (requested + waitlisted + confirmed),
`confirmed_subtotal_cents`, `discount_cents` and `confirmed_total_cents`. A PERCENT discount (0-100)
applies to the confirmed subtotal, rounded half-up to the cent; a FIXED discount (whole cents) is
capped at the subtotal. Administrators set discounts with PATCH `discount_type`, `discount_value`,
`discount_label`.

### Billing placeholders (`TODO(invoicing)`)

- `POST /applications/{id}/mark-billed/` needs IN_REVIEW and at least one CONFIRMED line; sets
  `billed_at` and moves to AWAITING_PAYMENT.
- `POST /applications/{id}/mark-first-payment/` (optional `note`) needs AWAITING_PAYMENT; sets
  `first_paid_at` and moves to WON.

### Vendor label

`Vendor.label` (also filterable: `GET /vendors/?label=active,prospect`): `suspended` if the journey
stage is SUSPENDED, `inactive` if not active, `active` if any application is WON, otherwise `prospect`.

## Endpoints (under `/api/v1/`)

| Endpoint | Who | Notes |
|---|---|---|
| `market-weekends/` (GET, POST, PATCH, DELETE) | read: all; write: admins | detail includes per-product capacity, confirmed, remaining, waitlisted and promotion suggestions |
| `market-weekends/{id}/variants/`, `.../variants/{id}/` | read: all; write: admins | |
| `market-weekends/{id}/add-ons/`, `.../add-ons/{id}/` | read: all; write: admins | |
| `applications/` (GET, POST, PATCH) | all; discounts and reviewer: admins | filters: `stage` (comma-separated), `has_waitlisted_lines`, `call=none\|attempted_not_reached`, `owner=me\|none\|<id>` (vendor owner), `source`, `weekend`, `vendor`, `reviewer` |
| `applications/summary/` | all | counts per stage and the 10 oldest AWAITING_PAYMENT |
| `applications/{id}/stage/` (POST) | admins | `{stage, reason}` |
| `applications/{id}/lines/` (GET, POST) | all | |
| `applications/{id}/mark-billed/`, `mark-first-payment/` (POST) | admins | placeholders |
| `application-lines/{id}/` (GET, PATCH, DELETE) | all | PATCH (quantity) and DELETE only while REQUESTED |
| `application-lines/{id}/confirm/`, `waitlist/`, `decline/`, `release/` (POST) | admins | `release` needs `reason` |

Management commands: `seed_dev` (DEBUG only) and `backfill_applications [--dry-run]`, which gives
every pre-v1.3 lead without an application one at NEW (or DECLINED, "Not qualified (migrated)",
for leads marked not qualified). It is idempotent and is not run by any migration; run it by hand.

## Deprecated (kept until the frontend update)

Migrations in this change are expand-only, so these remain and are kept working:

- `Application.status` (and its `status` filter): the column is kept in step with `stage`
  (`NEW` -> SUBMITTED, `IN_REVIEW` -> IN_REVIEW, `AWAITING_PAYMENT`/`WON` -> APPROVED,
  `DECLINED` -> DECLINED, `WITHDRAWN` -> WITHDRAWN) and the API derives it from `stage`.
  `legacy_status` keeps each application's status from before the migration.
- `Vendor.display_status` (`status` in the API and the `status` filter), `journey_stage`,
  `qualification_status` and `application_status_summary`. `waitlisted` no longer exists as a vendor
  status; journey becomes APPROVED when an application is WON.
- `POST /vendors/{id}/status/` supports applied -> in_review, decline, reopen, suspend and reinstate.
  `approved` (other than reinstating a suspended vendor) and `waitlisted` answer 400 with
  `code: use_line_decisions`, pointing to the line decision endpoints.
- PATCH `/applications/{id}/` with `status` maps IN_REVIEW, DECLINED and WITHDRAWN onto stages and
  answers 400 for APPROVED and WAITLISTED.
- `APPLICATION_STATUS_CHANGED` events remain in history; new changes record `APPLICATION_STAGE_CHANGED`.

## Email (v1.3.1, Slice 1: foundation)

The `communications` app gives Vendor Ops a safe, auditable email foundation. In this slice **nothing
triggers email yet** (no application, call, invoice or market workflow queues messages), every seeded
template is **Off**, and the default configuration sends nothing.

### How it works

```
workflow service ──(transaction.on_commit)──▶ queue_email() ──▶ EmailMessage (snapshot)
                                                                   │  queued / draft / skipped
systemd timer (every minute) ──▶ manage.py send_due_emails ──▶ provider ──▶ Resend API or Google SMTP relay
Resend ──(signed webhook)──▶ /api/v1/webhooks/anymail/resend/tracking/ ──▶ delivered / bounced / complained
```

- **Django owns everything**: templates, queued messages, delivery history and send decisions.
  Email is never sent inside a web request: services call
  `communications.services.queue_email_on_commit(template_key, vendor=..., application=...,
  market_weekend=..., event_key=..., actor=...)`, which runs only after the transaction commits.
- **Templates** (`EmailTemplate`) have a mode: **Off** (nothing is created), **Draft** (a draft
  message plus one *Review email* task for the application's reviewer or the vendor's owner) or
  **Auto** (queued for the next send window). The only syntax is `{variable}` from a fixed whitelist
  (`vendor_name`, `application_reference`, `market_name`, `market_dates`, `owner_name`,
  `payment_link`); `{{` and `}}` are literal braces. Database content is never run through Django's
  template engine, and unknown or undeclared placeholders are rejected when a template is saved.
  Values are HTML-escaped in HTML bodies.
- **Messages** (`EmailMessage`) snapshot the rendered subject, bodies, recipient, sender and
  reply-to when they are queued; editing a template never changes history, and the content fields
  cannot be changed or deleted. One message per template + application (or vendor) + `event_key`,
  enforced by a unique `dedupe_key`.
- **Recipients**: the vendor's contact email (never a payer). Missing, invalid or suppressed
  addresses produce a `skipped` message with a reason. `EmailRecipientPreference` holds suppression
  (unsubscribe, hard bounce, complaint, invalid address, manual), marketing consent (required for
  promotional templates) and whether transactional email is permitted; it is ready for the public
  unsubscribe page in a later slice.
- **Activity**: queued, drafted, sent, failed, skipped, cancelled, bounced and complained outcomes are
  written to the vendor timeline (`EMAIL_*` events; no bodies or addresses in the metadata).
- **Delivery tracking** (Resend only): the signed webhook moves a sent message to `delivered`,
  `bounced` or `complained`. Hard bounces and complaints suppress the address in
  `EmailRecipientPreference` (never for redirected messages, whose bounce concerns the team mailbox).
- **Stop conditions** (`communications/stop_conditions.py`): checks re-run just before sending, e.g.
  a later "application is still unpaid" check for payment reminders. Slice 1 registers none.

### Sending modes and settings

The effective sending mode is the **stricter** of `EMAIL_GLOBAL_MODE` (environment) and the admin
*Email settings* (Django admin → Email communications → Email settings; administrators only):

| Mode | What the worker does |
| --- | --- |
| `disabled` | Sends nothing; due messages wait (and expire after 72 hours). |
| `redirect` | Sends every message to the redirect mailbox (`EMAIL_REDIRECT_TO` or the admin setting) with the original recipient in the subject and body. Vendors receive nothing. |
| `enabled` | Sends to vendors. |

`EMAIL_FROM_ADDRESS`, `EMAIL_REPLY_TO` and `EMAIL_REDIRECT_TO` win over the admin values when set.
The send window (`EMAIL_SEND_WINDOW_START`/`END`, admin-editable) is applied in the configured
business time zone (`EMAIL_TIMEZONE`), never the server's.

| Variable | Default | Notes |
| --- | --- | --- |
| `EMAIL_PROVIDER` | `console` | `console` uses Django's console backend (in-memory under tests); `smtp_relay` is the Google Workspace SMTP relay; `resend` is Resend's API through django-anymail. |
| `EMAIL_GLOBAL_MODE` | `disabled` | `disabled`, `redirect` or `enabled`; a ceiling for the admin setting. |
| `EMAIL_FROM_NAME`, `EMAIL_FROM_ADDRESS`, `EMAIL_REPLY_TO` | `The Good Flea`, empty, empty | The from address is required for `smtp_relay` and `resend` (system check). |
| `EMAIL_REDIRECT_TO` | empty | Team mailbox for redirect mode. |
| `EMAIL_TIMEZONE` | `America/New_York` | |
| `EMAIL_SEND_WINDOW_START`, `EMAIL_SEND_WINDOW_END` | `09:00`, `18:00` | Same-day window. |
| `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `EMAIL_SMTP_USE_TLS` | `smtp-relay.gmail.com`, `587`, `true` | STARTTLS. |
| `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD` | empty | Optional: leave empty when the relay trusts the droplet's IP. Never commit them. |
| `EMAIL_RESEND_API_KEY` | empty | Required for `resend` (system check). Server environment only; never commit it. |
| `EMAIL_RESEND_WEBHOOK_SECRET` | empty | Resend webhook signing secret (`whsec_…`). Without it the webhook refuses every call (system check warning). Never commit it. |
| `EMAIL_DAILY_LIMIT` | `90` | Messages sent per day, counted since local midnight in `EMAIL_TIMEZONE`; `0` = no limit. |

### The worker: `send_due_emails`

```sh
python manage.py send_due_emails --dry-run      # what would happen; changes and sends nothing
python manage.py send_due_emails --limit 50     # what the timer runs (limit 1-500)
```

Each run prints one line: `scanned`, `sent`, `drafted` (drafts awaiting review), `skipped`, `failed`
and `deferred`. Due messages are claimed with `SELECT … FOR UPDATE SKIP LOCKED` and marked `sending`
before the provider is called, so concurrent runs never send the same message. Just before sending,
the worker re-checks expiry, the sender, the recipient, suppression and stop conditions. Retries are
conservative: transient failures (connection problems, 4xx replies) are retried after 5 and 30
minutes, three attempts in all, then marked `failed`; permanent failures (5xx, refused recipient,
authentication) fail at once; a message stuck in `sending` for 15 minutes is marked `failed` and is
never re-sent automatically, because it may already have been delivered.

**Daily limit**: `EMAIL_DAILY_LIMIT` (default 90; `0` = no limit) caps messages sent since local
midnight in the business time zone (redirected and test messages count too). Once it is reached the
remaining messages stay queued, untouched (no attempt used, not failed), and the summary says
`daily limit of 90 reached …; N left queued`. **Rate limits**: an HTTP 429 from Resend puts that
message and the rest of the batch back in the queue for a minute without using up a retry attempt.

**Test email**: `python manage.py send_test_email you@example.com` sends one message through the same
path (configured provider, sending mode, redirect mailbox, daily limit; not the send window, no
retries) and prints the result. In disabled mode nothing is sent; in redirect mode it goes to the
redirect mailbox. It exits non-zero when nothing was sent, and the message is recorded with template
key `test_email`.

Drafts are reviewed in Django admin (Email messages): administrators use the **Approve** and
**Cancel** actions; message records are otherwise read-only.

### Local, safe testing

The defaults (`EMAIL_PROVIDER=console`, `EMAIL_GLOBAL_MODE=disabled`) never reach a mail server. To
watch messages locally, set `EMAIL_GLOBAL_MODE=redirect` and `EMAIL_REDIRECT_TO=you@example.com`,
move one template to Auto in the admin, trigger it from a shell with `queue_email(...)`, and run
`send_due_emails`: the message is printed to the console. Tests use Django's in-memory backend, a
fake SMTP server and Anymail's test backend; they never contact a real mail server or Resend.

### Production: systemd timer

`deploy/deploy-backend.sh` installs or updates `vendor-ops-send-emails.service` and
`vendor-ops-send-emails.timer` on every deploy, pauses the timer during migrations and enables it
afterwards. The units use the same user, virtualenv and `/etc/vendor-ops/backend.env` as the API.
With the default environment the timer runs but sends nothing.

```sh
sudo systemctl enable --now vendor-ops-send-emails.timer     # done by the deploy script
sudo systemctl list-timers vendor-ops-send-emails.timer      # next and last run
sudo systemctl status vendor-ops-send-emails.service --no-pager
sudo journalctl -u vendor-ops-send-emails.service -n 50 --no-pager
sudo -u vendorops /opt/vendor-ops/venv/bin/python /opt/vendor-ops/backend-current/manage.py send_due_emails --dry-run
sudo -u vendorops /opt/vendor-ops/venv/bin/python /opt/vendor-ops/backend-current/manage.py send_test_email you@thegoodflea.com
sudo systemctl stop vendor-ops-send-emails.timer             # emergency stop (or set EMAIL_GLOBAL_MODE=disabled)
```

### Resend: setup (outside the application)

1. **Domain**: in the Resend dashboard (Domains), add `thegoodflea.com` (or a sending subdomain) and
   publish the DNS records Resend shows (SPF/MX on its bounce subdomain and the DKIM TXT record) in
   Shopify DNS; wait until Resend marks the domain verified. Publish DMARC at `p=none` as below.
2. **API key**: create a key with sending access and put it in `/etc/vendor-ops/backend.env` as
   `EMAIL_RESEND_API_KEY`; set `EMAIL_PROVIDER=resend` and `EMAIL_FROM_ADDRESS` (an address on the
   verified domain). `manage.py check` refuses `resend` without the key or the from address.
3. **Webhook**: in Resend (Webhooks), add the endpoint
   `https://ops.thegoodflea.com/api/v1/webhooks/anymail/resend/tracking/` with the events
   `email.delivered`, `email.bounced` and `email.complained` (others are accepted and ignored), and
   copy its signing secret (`whsec_…`) to `EMAIL_RESEND_WEBHOOK_SECRET`. nginx already proxies
   `/api/`. Calls with a bad or missing signature are answered 400; without a configured secret every
   call is refused.
4. Restart the API (`sudo systemctl restart vendor-ops-backend`), then run `send_test_email` in
   redirect mode before anything else.

The `smtp_relay` provider stays available; switch with `EMAIL_PROVIDER`.

### Google Workspace SMTP relay: prerequisites (outside the application)

Production email cannot be delivered until all of these are done and verified; the application
does not change any of them.

1. **Workspace admin** (Apps → Google Workspace → Gmail → Routing → SMTP relay service): add a relay
   rule that allows the DigitalOcean droplet's static IP address, requires TLS, and allows only
   addresses in your domains. Decide whether the relay also requires SMTP authentication; if it does,
   set `EMAIL_SMTP_USERNAME`/`EMAIL_SMTP_PASSWORD` for a Workspace account in
   `/etc/vendor-ops/backend.env`.
2. **DNS for thegoodflea.com** (managed in Shopify, so these are Shopify DNS changes):
   - SPF: the TXT record must include Google, e.g. `v=spf1 include:_spf.google.com ~all` (merge with
     any existing SPF; there can only be one SPF record).
   - DKIM: generate the key in the Workspace admin (Gmail → Authenticate email) and publish the
     `google._domainkey` TXT record, then start authentication.
   - DMARC: publish `_dmarc.thegoodflea.com` starting at `p=none` with a reporting address, e.g.
     `v=DMARC1; p=none; rua=mailto:dmarc-reports@thegoodflea.com`; tighten later.
3. Set `EMAIL_PROVIDER=smtp_relay`, `EMAIL_FROM_ADDRESS` (an address in the domain),
   `EMAIL_REPLY_TO` and `EMAIL_REDIRECT_TO` in the server environment, then restart the API.

### Staged rollout

1. Deploy with `EMAIL_GLOBAL_MODE=redirect` and `EMAIL_REDIRECT_TO` set to a team mailbox (admin
   setting `redirect` too). Templates are still Off.
2. Turn one template to Auto or Draft only to exercise it, and inspect the redirected messages in
   the team mailbox (headers, SPF/DKIM pass, layout, copy).
3. Set templates to **Draft** and let real drafts accumulate once triggers exist (Slice 2).
4. Review real drafts in the admin; approve or cancel.
5. Raise the ceiling to `enabled`, then move selected templates to **Auto** one at a time.
