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
python manage.py createcachetable  # shared cache for throttles (idempotent)
python manage.py seed_dev         # demo users, weekends and vendors, DEBUG only
python manage.py runserver 8000
python manage.py test
```

Apps: `accounts` (users and roles), `communications` (email; see "Email" below), `catalog` (market weekends, booth variants, add-ons),
`vendors` (vendors, applications and their lines, intake events, notes, activity timeline),
`tasks` (tasks, business-hours scheduling, call logging), `imports` (CSV), `public_api` (the public
application form; see "Public application form" below).
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
| `applications/` (GET, POST, PATCH) | all; discounts and reviewer: admins | filters: `stage` (comma-separated), `has_waitlisted_lines`, `call=none\|attempted_not_reached`, `owner=me\|none\|<id>` (vendor owner), `source`, `weekend`, `vendor`, `reviewer`, `campaign` (utm_campaign of the vendor's latest intake event). Items include `requested_weekends` [{id, label}] and `campaign` |
| `applications/summary/` | all | counts per stage and the 10 oldest AWAITING_PAYMENT |
| `applications/{id}/stage/` (POST) | admins | `{stage, reason}` |
| `applications/{id}/lines/` (GET, POST) | all | |
| `applications/{id}/lines-from-requests/` (POST) | all | `{booth_size}`: one REQUESTED line per requested weekend that has no open booth line; returns `created` and `skipped` (with reasons) |
| `applications/{id}/emails/` (GET, POST) | all | the application's emails; POST `{template_key, application_line?}` prepares a draft (see *Application emails*) |
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

The `communications` app gives Vendor Ops a safe, auditable email foundation. Every seeded template
is **Off** and the default configuration sends nothing. Slice 2 (below, *Application emails*) adds
the first four workflow triggers; with their templates Off they still create nothing.

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
  (`vendor_name`, `application_reference`, `market_name`, `market_dates`, `booth_name`,
  `owner_name`, `payment_link`); `{{` and `}}` are literal braces. Database content is never run through Django's
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
- **Stop conditions** (`communications/stop_conditions.py`): per-template checks that say an email no
  longer applies. They run when a message is prepared, when a draft is approved and again just
  before sending. Slice 2 registers the four application checks (see *Application emails*).

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

Each run prints one line: `scanned`, `sent`, `drafted` (drafts awaiting review), `skipped`, `failed`,
`deferred` and `stale_cancelled` (drafts cancelled because they no longer apply). Due messages are claimed with `SELECT … FOR UPDATE SKIP LOCKED` and marked `sending`
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

Drafts are reviewed in the app through the email API (see *Application emails*); Django admin
(Email messages) also has **Approve** and **Cancel** actions for administrators. Message records are
otherwise read-only.

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
3. Set the four application templates to **Draft** and let real drafts accumulate (Slice 2).
4. Review real drafts in the application's Emails tab; edit, approve or cancel.
5. Raise the ceiling to `enabled`, then move selected templates to **Auto** one at a time.

## Application emails (v1.3.2, Slice 2)

Four application workflows now prepare email through the Slice 1 foundation (same queue, worker,
provider and safety controls). **All four templates stay Off after deployment**: an administrator
must move a template to Draft (recommended first) or Auto before anything is created. Preparing an
email never breaks the workflow that triggered it; a failure is logged and the workflow carries on.

### Triggers

| Template | Fires when | Not when | One per (`event_key`) |
| --- | --- | --- | --- |
| `application_received` | `submit_application` creates a **new** application (vendor form, staff, CSV import) | the submission is bundled into an open application; `backfill_applications`; migrations | application |
| `call_follow_up` | a discovery or follow-up call is logged with outcome **No Answer** | any other outcome (Interested, Not Qualified, Callback Requested, Wrong Number…) | call task (repeat No Answers on the same task add nothing) |
| `waitlist_update` | a **booth** line is waitlisted (`waitlist_line`) | add-on lines; the application itself is never "waitlisted" | application line |
| `application_declined` | the application moves to **DECLINED** | WITHDRAWN; declining a single line | decision (`decided_at`), so decline → reopen → decline is two events |

Each message stores the line it is about (`application_line`) and a small `trigger` snapshot (call
task and attempt time, waitlist time, decision time; `{"manual": true}` for manual drafts). The
waitlist email names the booth, market and dates through the new `{booth_name}` variable. The decline
email never includes the internal decline reason. Callback tasks and call notes are unchanged.

### Stop conditions (checked when prepared, when approved and just before sending)

| Template | No longer applies when |
| --- | --- |
| `application_received` | the application is declined or withdrawn |
| `call_follow_up` | the application is closed, or the vendor was reached on a later call (Interested, Not Qualified, Callback Requested) |
| `waitlist_update` | the application is closed, or that line is no longer waitlisted (confirmed, declined, released) |
| `application_declined` | the application is no longer declined, or it was declined again later (the email is for the earlier decision) |

A queued email that no longer applies is **skipped** by the worker (`failure_code=stop_condition`).
A draft that no longer applies cannot be approved, and the worker cancels it within a minute
(reason "No longer applies: …", review task closed as cancelled), counted as `stale_cancelled`.

### Drafts and review

- A draft creates one **Review email** task, assigned to the application's reviewer, else the
  vendor's owner, else unassigned (team queue). Its description points to the application's Emails tab.
- Every signed-in team member can see and **edit** drafts (subject, HTML and text). Each edit keeps
  the previous version as an `EmailDraftRevision` (append-only, numbered) and records an
  `EMAIL_REVISED` activity event. Only drafts can be edited; the recipient and sender cannot be changed.
- **Approve** and **Cancel** are for administrators, the review task's owner and the application's
  reviewer. Approving queues the email for the next send window and completes the task; cancelling
  (drafts or queued emails) cancels the task. Both are recorded in the activity timeline.
- The task API refuses to close a review task while its draft is unresolved, and refuses to create
  `EMAIL_REVIEW` tasks directly.
- A team member can prepare a draft by hand from an application (`POST applications/{id}/emails/`),
  whatever the template mode (the template must be active). It is refused when an identical draft is
  already awaiting review, when the email does not apply, when a booth is required but not chosen, or
  when the recipient address is missing, invalid or suppressed.

### Email API (under `/api/v1/`)

| Endpoint | Who | Notes |
| --- | --- | --- |
| `email/settings/` (GET, PATCH) | read: all; write: admins | stored and **effective** values, environment status, `live_sending_blocked`, `notices`; never credentials |
| `email/templates/` (GET), `email/templates/{key}/` (GET, PATCH) | read: all; write: admins | PATCH `subject_template`, `html_template`, `text_template`, `mode`, `allowed_variables`; invalid placeholders answer 400 per field |
| `email/templates/{key}/preview/` (POST) | all | `{application?, application_line?, market_weekend?}` plus optional unsaved template parts; returns `subject`, `html_body`, `text_body`, `data_source` (`application`/`sample`), `missing_variables` |
| `email/messages/` (GET) | all | paginated; filters `application`, `vendor`, `market_weekend`, `application_line`, `template`, `status` (comma-separated), `bucket`; `search`, `ordering` |
| `email/messages/{id}/` (GET, PATCH) | all | PATCH edits a draft: `{subject?, html_body?, text_body?, note?}` |
| `email/messages/{id}/approve/`, `cancel/` (POST) | admins, task owner, reviewer | cancel takes `{reason}` |
| `email/messages/{id}/revisions/` (GET) | all | previous versions, oldest first |
| `email/overview/` (GET) | all | `counts` per bucket (each message counted once), `by_status`, `recent_activity` |
| `applications/{id}/emails/` (GET, POST) | all | the application's messages (paginated); POST prepares a draft |

Buckets: `awaiting_review` (draft), `queued` (queued, sending), `sent` (sent, delivered), `problems`
(failed, bounced, complained), `not_sent` (skipped, cancelled). Every list and detail item carries
`permissions {can_edit, can_approve, can_cancel}` for the current user and `no_longer_applies`
(the stop-condition reason, or null). Full example responses and UI states are in
[docs/email-frontend-guide.md](docs/email-frontend-guide.md); the manual testing checklist and known
limitations are in [docs/email-slice-2-testing.md](docs/email-slice-2-testing.md).


## Public application form

The vendor-facing page (**https://ops.thegoodflea.com/apply**, also the ads landing page; in the
frontend repository) uses two endpoints under `/api/v1/public/`. They need no login, session or CSRF
token, and both answer **404 while `PUBLIC_APPLICATIONS_ENABLED` is off (the default)**. The page calls
nothing else; the staff API still requires a signed-in team member. (A separate
`apply.thegoodflea.com` host is possible with `deploy/nginx-apply.thegoodflea.com.conf`, but not needed.)

### `GET /api/v1/public/market-weekends/`

OPEN weekends that are not over yet, soonest first. Never prices, capacities, counts or vendor data.
`Cache-Control: public, max-age=60`; throttle `public_weekends` (120/hour per client IP).

```json
{
  "weekends": [
    {"id": "…", "label": "Oct 10 & 11", "start_date": "2026-10-10", "end_date": "2026-10-11",
     "variants": [{"name": "8ft x 8ft", "dimensions": "", "waitlist": true}, {"name": "10ft x 10ft", "dimensions": "", "waitlist": false}],
     "waitlist_only": false}
  ],
  "booth_sizes": [{"name": "8ft x 8ft", "dimensions": ""}, {"name": "10ft x 10ft", "dimensions": ""}]
}
```

`waitlist` is true when a size has no spot left; `waitlist_only` when every size is on the waitlist.
Applying is still allowed: requested lines never use capacity, and staff waitlist them at review.

### `POST /api/v1/public/applications/`

```json
{"first_name": "Ana", "last_name": "Silva", "email": "ana@example.com", "phone": "555 123 4567",
 "brand_name": "Juniper Goods", "instagram": "@juniper.goods", "weekend_ids": ["…"],
 "booth_size": "8ft x 8ft", "consent": true, "consent_text_version": "2026-10",
 "turnstile_token": "…", "website": "",
 "attribution": {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "fall-ads", "utm_content": "",
                 "utm_term": "", "fbclid": "", "gclid": "", "referrer": "", "landing_path": "/"}}
```

**Validation** (400 with per-field errors, only about the applicant's own input): first name, last
name and brand name required; a valid email; phone with 7 to 15 digits after normalising; Instagram as
`@handle` or an instagram.com link, normalised to letters, digits, dots and underscores (1 to 30);
1 to 12 distinct weekends, all open; `booth_size` is `not_sure` or a size offered on at least one
chosen weekend; `consent` true. Control characters are stripped and lengths capped.

**What it does**: exactly what a staff-entered application does, through `submit_application`, as
the inactive "Website form" system user, with source `Website form`:

- a chosen size becomes one REQUESTED line per chosen weekend that offers it (a weekend without that
  size is a requested weekend only); `not_sure` creates no lines;
- `requested_weekends` always holds every chosen weekend;
- a new application starts at New with a call task and the `application_received` email trigger
  (subject to the template's mode, Off by default); a vendor with an open application is
  **bundled** into it (booths it already has are skipped, not refused);
- attribution, a truncated user agent, the consent (version and time) and the details the applicant
  typed go in the intake event's metadata. **The IP address is never stored.**
- **Several matching vendors** (e.g. the email matches one and the phone another): never dropped and
  never revealed. It is attached to the email match (else phone, else Instagram), with a staff note
  ("Phone also matches <brand>") and a team-queue follow-up task "Possible duplicate vendor: review
  web application".

**The same answer for everyone**: every accepted submission (new vendor, existing vendor, bundled,
ambiguous) and a filled honeypot get `200 {"status": "received"}`, with no ids or flags. A filled
`website` (honeypot) field creates nothing. Everything is one transaction.

### Spam and abuse protection

| Protection | Behaviour |
| --- | --- |
| Cloudflare Turnstile | Verified server-side (siteverify) after validation and before anything is created. The token must be a success **for an expected hostname** (`TURNSTILE_EXPECTED_HOSTNAMES`, default `ops.thegoodflea.com`) **and the expected action** (`TURNSTILE_EXPECTED_ACTION`, default `apply`). Missing, invalid, expired, reused or mismatched tokens answer 400; an unreachable Turnstile or a bad secret 503. Fails closed: nothing is created. The client IP is sent as `remoteip` only when it is a valid address. |
| No bypass | Without `TURNSTILE_SECRET_KEY` every submission is refused (503), in development too. `manage.py check` errors when the form is on in production without the secret (`public_api.E001`), the expected hostnames (`E002`) or the expected action (`E003`), and warns for a Cloudflare test secret (`W001`). |
| Per-IP throttle | `public_apply` 10/hour (mobile networks share addresses; do not go lower). |
| Per-email limit | 3 accepted submissions per address per day (429); the cache key is a keyed hash, never the address. Refused or invalid attempts do not count. |
| Daily cap | `PUBLIC_APPLICATIONS_DAILY_CAP` (200) website applications created per day in the business time zone; above it 503 and a warning in the log. Bundled submissions create no application and are not counted. |
| Kill switch | `PUBLIC_APPLICATIONS_ENABLED=false` (default): both endpoints 404. |
| Logs | Never request bodies, tokens or personal details; the application id only. |

**Client IP and the cache.** gunicorn listens on a unix socket behind nginx, which appends the real
client address to `X-Forwarded-For`. `REST_FRAMEWORK['NUM_PROXIES'] = 1` makes throttles use that
last entry, so a client-sent prefix cannot change who is counted (tested). Throttle counters live in
a shared database cache table (`django_cache`) rather than per-process memory, so every gunicorn
worker counts together; `manage.py createcachetable` creates it (the deploy script runs it after
`migrate`). Tests use an in-memory cache.

### Testing locally with Turnstile test keys

Cloudflare's documented test keys work without an account: site key `1x00000000000000000000AA`
(always passes) or `2x00000000000000000000AB` (always fails) in the page, and secret
`1x0000000000000000000000000000000AA` (always passes), `2x0000000000000000000000000000000AA` (always
fails) or `3x0000000000000000000000000000000AA` (token already spent) here. The test site keys
produce the token `XXXX.DUMMY.TOKEN.XXXX`, which only test secrets accept. Test-key responses carry
placeholder hostnames and actions, so with `DJANGO_DEBUG=true` **and a test secret** the hostname and
action checks are skipped; a real secret is always checked in full, and production never relaxes them.

```sh
# .env: PUBLIC_APPLICATIONS_ENABLED=true, TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA
curl -s localhost:8000/api/v1/public/market-weekends/
curl -s -X POST localhost:8000/api/v1/public/applications/ -H 'Content-Type: application/json' \
  -d '{"first_name":"Ana","last_name":"Silva","email":"ana@example.com","phone":"5551234567","brand_name":"Juniper","instagram":"juniper","weekend_ids":["<id>"],"booth_size":"not_sure","consent":true,"turnstile_token":"XXXX.DUMMY.TOKEN.XXXX"}'
```

Automated tests use only these test keys, mock siteverify and never call Cloudflare.

### Staff side

Web applications appear on the board with source `Website form`, `requested_weekends` and
`campaign`. `POST /applications/{id}/lines-from-requests/ {"booth_size": "8ft x 8ft"}` (any team
member) requests that size on every requested weekend that has no open booth line and says which
weekends it skipped and why.
