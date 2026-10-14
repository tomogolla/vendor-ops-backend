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

Apps: `accounts` (users and roles), `catalog` (market weekends, booth variants, add-ons),
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
