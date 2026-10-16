# Email screens: frontend integration guide (Slice 2)

This guide is for building the email screens in the Svelte frontend against the Slice 2 backend. It
covers three places: **Settings → Emails**, the **Emails tab on an application**, and **Overview
cards**. Every route is under `/api/v1/`, uses the existing session and CSRF authentication, and
returns JSON. The example responses below were captured from the real API on a development database
and shortened (`…`); field names are exact.

Contents: [Shared rules](#shared-rules) · [Settings → Emails](#1-settings--emails) ·
[Application → Emails tab](#2-application--emails-tab) · [Overview cards](#3-overview-cards) ·
[Errors](#errors) · [Status reference](#status-reference)

## Shared rules

- **Never decide permissions in the UI from the role alone.** Every message carries
  `permissions: {can_edit, can_approve, can_cancel}` computed for the signed-in user. Show or enable
  buttons from these flags; the server enforces the same rules.
- Settings and templates: everyone can read, only administrators (`role === "ADMINISTRATOR"` from
  `auth/me/`) can change. Show the edit controls only to administrators; a 403 is the fallback.
- **Render `html_body` safely**: show it in a sandboxed `<iframe sandbox srcdoc=…>` (no
  `allow-scripts`), never with `{@html}` in the page. Template values are HTML-escaped, but edited
  drafts can contain any HTML.
- Lists are paginated: `{count, next, previous, results}`, 50 per page, `?page=2`, `?page_size=`
  up to 500. The templates list is the only unpaginated list (a plain array).
- After any action (edit, approve, cancel, prepare) the response is the **full updated message**;
  replace the item in place rather than refetching the list.

## 1. Settings → Emails

### 1a. Sending status panel: `GET email/settings/`

```json
{
  "sending_mode": "disabled",
  "redirect_to_email": "",
  "sender_name": "The Good Flea",
  "sender_email": "",
  "reply_to_email": "",
  "business_timezone": "America/New_York",
  "send_window_start": "09:00:00",
  "send_window_end": "18:00:00",
  "updated_by": null,
  "updated_at": "2026-10-15T15:04:12.599893Z",
  "effective": {
    "mode": "disabled",
    "sender_name": "The Good Flea",
    "sender_email": "",
    "reply_to_email": "",
    "redirect_to_email": ""
  },
  "environment": {
    "global_mode": "disabled",
    "provider": "console",
    "provider_configured": true,
    "delivers_real_email": false,
    "webhook_configured": null,
    "daily_limit": 90,
    "daily_remaining": 90
  },
  "live_sending_blocked": true,
  "notices": [
    "Sending is disabled: queued emails wait and nothing is sent.",
    "The console provider only prints messages on the server; nothing is delivered."
  ]
}
```

- The top-level fields are what an administrator **chose**. `effective` is what **actually applies**,
  because the server environment can be stricter (`environment.global_mode` is a ceiling) and can
  override the sender addresses. Always show `effective.mode` as "Sending is …", and if it differs
  from `sending_mode`, explain: "You chose Enabled, but the server only allows Redirect."
- Show every string in `notices` as an information banner, in order. They are written for the team.
- `live_sending_blocked: true` means no vendor can receive email right now (disabled or redirect
  mode, or a provider that does not deliver). Use it for a clear "Vendors will not receive email"
  badge.
- `webhook_configured` is `null` unless the provider is Resend (console and SMTP relay have no delivery tracking),
  otherwise `true`/`false`; show a warning for `false` ("Delivery tracking is not set up").
- `daily_remaining` of `null` means no daily limit.
- No credentials are ever returned. Do not build fields for API keys or passwords; they live only in
  the server environment.

**Editing (administrators):** `PATCH email/settings/` with any of `sending_mode` (`disabled`,
`redirect`, `enabled`), `redirect_to_email`, `sender_name`, `sender_email`, `reply_to_email`,
`business_timezone`, `send_window_start`, `send_window_end` (`"HH:MM"`). The response is the same
shape as GET. Validation errors come back per field, e.g. a window that ends before it starts:

```json
{ "send_window_end": ["The send window must end after it starts (same day)."] }
```

Confirm before switching to `enabled` ("Vendors will start receiving queued emails").

### 1b. Templates list: `GET email/templates/` (array)

```json
[
  {
    "key": "call_follow_up",
    "name": "Call follow-up",
    "description": "Sent after a discovery call attempt.",
    "category": "operational",
    "mode": "off",
    "is_active": true,
    "subject_template": "Following up on our call, {vendor_name}",
    "html_template": "<p>Hi {vendor_name},</p><p>We tried to reach you …</p><p>{owner_name}<br>The Good Flea</p>",
    "text_template": "Hi {vendor_name},\n\nWe tried to reach you …\n\n{owner_name}\nThe Good Flea",
    "allowed_variables": ["vendor_name", "owner_name"],
    "available_variables": {
      "vendor_name": "The vendor's brand name.",
      "application_reference": "Short reference for the application, e.g. APP-1A2B3C4D.",
      "market_name": "The market weekend label, e.g. \"Oct 10 & 11\".",
      "market_dates": "The market weekend dates, e.g. \"Oct 10 – 11, 2026\".",
      "booth_name": "The booth (or add-on) a decision is about, e.g. \"8ft x 8ft\".",
      "owner_name": "The team member who looks after the vendor, or \"The Good Flea team\".",
      "payment_link": "A payment link (https). Only available once invoicing exists."
    },
    "updated_by": null,
    "updated_at": "2026-10-15T15:04:10.101262Z"
  }
]
```

- Ten templates. Show `name`, `description`, `category` and a mode badge: **Off** (nothing is
  created), **Draft** (drafts wait for review) and **Auto** (sent automatically in the send window).
- The four Slice 2 templates are `application_received`, `call_follow_up`, `waitlist_update` and
  `application_declined`. The others (invoice, payment, market…) have no trigger yet; label them
  "Not used yet" so nobody expects them to fire.
- `allowed_variables` are the placeholders this template may use; `available_variables` describes
  every placeholder (use it for an "Insert variable" menu and tooltips).

### 1c. Template editor: `GET/PATCH email/templates/{key}/`

Administrators can PATCH `subject_template`, `html_template`, `text_template`, `mode` and
`allowed_variables`. Everything else is read-only. Placeholders are `{name}`; `{{` and `}}` are
literal braces.

Errors are per field and are safe to show as they are:

```json
{ "subject_template": ["Unknown placeholder(s): {secret}."] }
```

Other messages you may see: "Placeholder(s) not allowed for this template: {market_name}. Add them
to the allowed variables first." When changing `mode` to `auto`, confirm first ("Emails will be
sent without review"). Recommend Draft before Auto in the UI copy.

### 1d. Preview: `POST email/templates/{key}/preview/`

Body (all optional):

| Field | Meaning |
| --- | --- |
| `application` | an application id: preview with that application's real data |
| `application_line` | a line of that application (needed for the waitlist booth and market) |
| `market_weekend` | a weekend id, when no line is given |
| `subject_template`, `html_template`, `text_template`, `allowed_variables` | **unsaved** edits to preview before saving |

With no application, sample data is used:

```json
{
  "subject": "We received your application, Sample Vendor Co.",
  "html_body": "<p>Hi Sample Vendor Co.,</p><p>Thanks for applying to The Good Flea. Your reference is <strong>APP-SAMPLE01</strong>. …</p>",
  "text_body": "Hi Sample Vendor Co.,\n\nThanks for applying to The Good Flea. Your reference is APP-SAMPLE01. …",
  "data_source": "sample",
  "missing_variables": []
}
```

With a real application and line (`waitlist_update`):

```json
{
  "subject": "Waitlist update for Oct 10 & 11: 10ft x 10ft",
  "html_body": "<p>Hi Example Ceramics,</p><p>The <strong>10ft x 10ft</strong> booth you asked for at Oct 10 &amp; 11 (Oct 10 – 11, 2026) is full, …</p>",
  "text_body": "Hi Example Ceramics,\n\nThe 10ft x 10ft booth you asked for …",
  "data_source": "application",
  "missing_variables": []
}
```

- Show a "Sample data" or "Using APP-… data" label from `data_source`.
- If `missing_variables` is not empty, the preview shows `[booth_name]` style gaps; show a warning
  such as "Choose a booth to fill in booth_name, market_dates, market_name". An actual email would
  not be created in that state.
- A broken unsaved template answers 400 with per-field errors (same format as 1c). Debounce preview
  calls while typing (e.g. 400 ms).

## 2. Application → Emails tab

### 2a. List: `GET applications/{id}/emails/`

Same item format as `GET email/messages/?application={id}`; newest first.

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 8,
      "template_key": "waitlist_update",
      "template_name": "Waitlist update",
      "status": "draft",
      "status_label": "Draft (awaiting review)",
      "subject": "Waitlist update for Oct 10 & 11: 10ft x 10ft",
      "recipient_name": "Example Ceramics",
      "recipient_email": "hello@example.com",
      "delivered_to_email": "",
      "redirected": false,
      "application": "ea1bbc63-3e45-42dc-a076-ebf0ec894a15",
      "vendor": { "id": "42d09ad4-…", "brand_name": "Example Ceramics" },
      "market_weekend": { "id": "f26cc1ce-…", "label": "Oct 10 & 11", "start_date": "2026-10-10" },
      "application_line": {
        "id": "c4666cd4-…",
        "product_name": "10ft x 10ft",
        "weekend_label": "Oct 10 & 11",
        "status": "WAITLISTED"
      },
      "scheduled_for": null,
      "sent_at": null,
      "delivered_at": null,
      "failure_code": "",
      "failure_detail": "",
      "revision": 0,
      "review_task": {
        "id": "9b5768d7-…",
        "status": "TODO",
        "is_open": true,
        "title": "Review email: Waitlist update to Example Ceramics",
        "owner": null,
        "due_at": "2026-10-16T13:00:00Z",
        "outcome": ""
      },
      "created_by": { "id": "9079f459-…", "username": "admin", "name": "Avery Admin", "role": "ADMINISTRATOR" },
      "created_at": "2026-10-16T12:45:06.411442Z",
      "updated_at": "2026-10-16T12:45:06.422438Z",
      "permissions": { "can_edit": true, "can_approve": true, "can_cancel": true },
      "no_longer_applies": null
    }
  ]
}
```

Row layout suggestion: template name, subject, status badge (`status_label`), the booth and weekend
when `application_line` is set ("10ft x 10ft · Oct 10 & 11"), and the relevant date (`sent_at`,
`scheduled_for` or `created_at`). `review_task.owner` is `null` for the team queue ("Unassigned").
`created_by` is `null` for emails prepared by the system.

### 2b. Detail: `GET email/messages/{id}/`

The list fields plus `html_body`, `text_body`, `sender_name`, `sender_email`, `reply_to_email`,
`provider`, `provider_message_id`, `attempt_count`, `event_key` and `trigger`. `trigger` explains why
the email exists: `{}` (application received), `{call_task_id, attempt, attempted_at}`,
`{line_id, waitlisted_at}`, `{decided_at}` or `{"manual": true}`. Show the text and HTML versions in
two tabs.

### 2c. Edit a draft: `PATCH email/messages/{id}/`

Only when `permissions.can_edit`. Send any of `subject`, `html_body`, `text_body`, plus an optional
`note` ("why I changed it"). The response is the updated detail with `revision` increased by one.

```json
// request
{ "subject": "Thanks for applying, Example Ceramics", "note": "Shorter subject" }
// response (excerpt)
{ "id": 7, "status": "draft", "subject": "Thanks for applying, Example Ceramics", "revision": 1, … }
```

Sending identical content changes nothing (no new revision). Errors: `{"subject": "A subject is
required."}`, `{"html_body": "The HTML body cannot be empty."}`, and once the email is no longer a
draft: `{"status": "Only drafts awaiting review can be edited."}` (refresh the item, since someone
else probably approved it).

### 2d. Revision history: `GET email/messages/{id}/revisions/`

```json
[
  {
    "number": 1,
    "subject": "We received your application, Example Ceramics",
    "html_body": "<p>Hi Example Ceramics,</p>…",
    "text_body": "Hi Example Ceramics,\n\n…",
    "replaced_by": { "id": "9079f459-…", "username": "admin", "name": "Avery Admin", "role": "ADMINISTRATOR" },
    "note": "Shorter subject",
    "created_at": "2026-10-16T12:45:06.951094Z"
  }
]
```

Each entry is the version **before** an edit: revision 1 is the original wording, and the current
content is on the message itself. Show "Edited N times" when `revision > 0` with a link to this list.

### 2e. Approve: `POST email/messages/{id}/approve/` (no body)

Only when `permissions.can_approve`. The response has `status: "queued"`, `scheduled_for` (the next
send-window time), and `review_task` with `status: "COMPLETED"` and `outcome: "Approved"`. Say
"Approved. It will be sent at 9:00 AM" (format `scheduled_for` in the business time zone), and if
`live_sending_blocked` is true add "(sending is currently off or redirected)".

If the email no longer applies, the server answers 400, e.g.
`{"status": "This email no longer applies: The application is no longer declined (it is in review). Cancel it instead."}`.

### 2f. Cancel: `POST email/messages/{id}/cancel/`

Only when `permissions.can_cancel` (drafts and queued emails). Body `{"reason": "Sent by phone
instead"}`; ask for the reason in a small dialog. The response has `status: "cancelled"` and
`failure_detail` set to the reason. Cancelling a draft closes its review task as cancelled.

### 2g. Prepare an email by hand: `POST applications/{id}/emails/`

A "New email" button with a template picker (the four application templates). For
`waitlist_update`, also ask for the booth line (`application_line`, booth lines only).

```json
// request
{ "template_key": "call_follow_up" }
// 201 response: the full message detail, status "draft", trigger {"manual": true}
```

This works whatever the template's mode: the result is always a draft for review, never sent
directly. Refusals (400), shown to the user as they are:

| Response | Show |
| --- | --- |
| `{"template_key": "A draft of this email is already awaiting review.", "message_id": "9"}` | the message, plus "Open draft" linking to message 9 |
| `{"template_key": "This email needs booth_name, market_dates, market_name, which this application cannot provide (choose a booth line)."}` | ask for a booth line |
| `{"template_key": "This email does not apply right now: …"}` | the message |
| `{"recipient": "…"}` | "Fix the vendor's email address first" with a link to the vendor |
| `{"application_line": "That line belongs to another application."}` | field error |

### 2h. UI states for a message

| State | Condition | Show |
| --- | --- | --- |
| Awaiting review | `status == "draft"` | Edit / Approve / Cancel per `permissions`; "Review task: owner or Unassigned" |
| Awaiting review, stale | `status == "draft"` and `no_longer_applies` | warning "No longer applies: {reason}"; hide Approve; offer Cancel. The system cancels it within a minute. |
| Queued | `status == "queued"` | "Scheduled for {scheduled_for}"; Cancel if `can_cancel`; if `no_longer_applies`, "Will not be sent: {reason}" |
| Sending | `status == "sending"` | "Sending…" (no actions) |
| Sent | `status == "sent"` | `sent_at`; if `redirected`, "Redirected to {delivered_to_email} (test mode)" |
| Delivered | `status == "delivered"` | `delivered_at` |
| Problem | `failed`, `bounced`, `complained` | red badge, `failure_detail` |
| Not sent | `skipped`, `cancelled` | grey badge, `failure_detail` (e.g. the stop reason or cancel reason) |
| Empty tab | `count == 0` | "No emails for this application yet." plus the New email button |

`redirected: true` means the email went to the team's test mailbox, not to the vendor; always make
that visible.

## 3. Overview cards

### `GET email/overview/`

```json
{
  "counts": { "awaiting_review": 2, "queued": 0, "sent": 0, "problems": 0, "not_sent": 1 },
  "by_status": { "draft": 2, "cancelled": 1 },
  "filters": {
    "awaiting_review": { "bucket": "awaiting_review" },
    "queued": { "bucket": "queued" },
    "sent": { "bucket": "sent" },
    "problems": { "bucket": "problems" },
    "not_sent": { "bucket": "not_sent" }
  },
  "recent_activity": [
    {
      "id": "e3300e59-…",
      "event_type": "EMAIL_CANCELLED",
      "description": "Email cancelled",
      "vendor": { "id": "42d09ad4-…", "brand_name": "Example Ceramics" },
      "application": "ea1bbc63-3e45-42dc-a076-ebf0ec894a15",
      "email_message_id": 7,
      "actor": "Avery Admin",
      "created_at": "2026-10-16T12:45:07.174480Z"
    }
  ]
}
```

- One card per `counts` key: **Awaiting review**, **Queued**, **Sent**, **Problems**, **Not sent**.
  Each message is counted once (a delivered email counts as Sent only), so the cards add up to the total.
- Clicking a card opens a message list with the matching filter:
  `GET email/messages/?bucket=awaiting_review` (the `filters` object holds the exact query parameters).
- Highlight **Awaiting review** and **Problems** when they are above zero.
- `recent_activity` holds the 10 most recent email events; link each to its application
  (`application`) and, when set, the message (`email_message_id`).
- Combine with `GET email/settings/` to show a "Sending is off / redirected" banner near the cards.

### Message list filters: `GET email/messages/`

| Parameter | Example |
| --- | --- |
| `bucket` | `awaiting_review`, `queued`, `sent`, `problems`, `not_sent` |
| `status` | `draft` or `failed,bounced` |
| `template` | `waitlist_update` |
| `application`, `vendor`, `market_weekend`, `application_line` | ids |
| `search` | subject, recipient or brand name |
| `ordering` | `-created_at` (default), `scheduled_for`, `sent_at` |

## Errors

| Code | Meaning | Show |
| --- | --- | --- |
| 400 | a rule refused the action; body is `{field: message}` or `{field: [messages]}` | the message(s) next to the field, or as a toast for `status` |
| 403 | not allowed (e.g. a team member approving, or a non-admin editing a template) | "You don't have permission to do this." and hide the control |
| 404 | the message or template does not exist | "Not found" |

Review tasks: the task API refuses to complete an `EMAIL_REVIEW` task while its draft is still a
draft (`"Approve or cancel the email draft instead; that closes this task."`). On task screens,
replace "Complete" for these tasks with a link to the application's Emails tab.

## Status reference

| `status` | `status_label` | Bucket |
| --- | --- | --- |
| `draft` | Draft (awaiting review) | awaiting_review |
| `queued` | Queued | queued |
| `sending` | Sending | queued |
| `sent` | Sent | sent |
| `delivered` | Delivered | sent |
| `failed` | Failed | problems |
| `bounced` | Bounced | problems |
| `complained` | Complained (marked as spam) | problems |
| `skipped` | Skipped | not_sent |
| `cancelled` | Cancelled | not_sent |
