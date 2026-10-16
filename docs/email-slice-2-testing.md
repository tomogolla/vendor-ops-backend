# Application emails (Slice 2): manual testing checklist and known limitations

## Before you start (safe set-up)

Use a development or staging server, never production with `EMAIL_GLOBAL_MODE=enabled`.

1. Server environment: `EMAIL_GLOBAL_MODE=redirect` and `EMAIL_REDIRECT_TO=<a team mailbox>`, or keep
   the default `disabled` with `EMAIL_PROVIDER=console` to only print messages.
2. `python manage.py migrate`, then confirm in Django admin (Email templates) that all ten
   templates are **Off**.
3. As an administrator, set `application_received`, `call_follow_up`, `waitlist_update` and
   `application_declined` to **Draft** (`PATCH /api/v1/email/templates/{key}/ {"mode": "draft"}` or
   Django admin).
4. Use test vendors with addresses you control (e.g. `you+test1@thegoodflea.com`).

## Checklist

**Triggers**

- [ ] Submit a new application through the public form: one "Application received" draft appears
      on its Emails tab and one *Review email* task appears (assigned to the reviewer, else the
      vendor owner, else unassigned).
- [ ] Submit again with the same contact email (bundled into the open application): no second draft.
- [ ] Import a CSV with one new vendor: one draft for that vendor.
- [ ] Log a discovery call with **No Answer**: one "Call follow-up" draft. Log No Answer again on the
      same call task: still one. Callback Requested, Interested and Wrong Number create none, and the
      callback task and call notes look as before.
- [ ] Add two booth lines on different weekends and waitlist both: two "Waitlist update" drafts, each
      naming its own booth, market and dates. Waitlist an add-on: no draft.
- [ ] Decline the application with an internal reason: one "Application declined" draft, and the
      reason does not appear in it. Withdraw another application, and decline a single line on a
      third: no decline drafts.

**Review**

- [ ] As a team member: edit a draft's subject and text, then open its revision history (the
      original wording is listed as revision 1). Approve and Cancel are not offered.
- [ ] As the application's reviewer or an administrator: approve a draft. It becomes *Queued* for
      the next send window and its review task is completed.
- [ ] Cancel another draft with a reason: it becomes *Cancelled* and its task is cancelled.
- [ ] Try to complete a review task from the task screen while its draft is pending: refused.
- [ ] Prepare an email by hand from an application (`POST /api/v1/applications/{id}/emails/`), then
      try again: the second attempt is refused with a link to the first draft.

**Stop conditions**

- [ ] Waitlist a line (draft appears), then confirm the line. Within a minute the draft is cancelled
      as "No longer applies"; approving it in the meantime is refused.
- [ ] Decline an application, reopen it, decline it again: the first decline draft is cancelled,
      a new one appears.
- [ ] Log No Answer, then log Interested: the follow-up draft is cancelled.

**Sending (redirect mode only)**

- [ ] Approve a draft during the send window and wait for the worker (or run
      `python manage.py send_due_emails`): the email arrives in the redirect mailbox with the
      original recipient shown, never at the vendor.
- [ ] Settings → Emails (`GET /api/v1/email/settings/`) shows the effective mode as redirect and
      contains no keys or passwords.
- [ ] `GET /api/v1/email/overview/` counts match what you see; a delivered email is counted once.

**Afterwards**

- [ ] Set the four templates back to **Off** (or leave them on Draft if the team is ready to review).

## Known limitations

- **Templates are Off.** Nothing happens until an administrator switches a template to Draft or Auto,
  and switching it on does not create emails for events that happened earlier.
- **Only "No Answer" sends an automatic follow-up**, at most one per call task. Other outcomes need
  a manually prepared email. A new follow-up call task can produce another follow-up.
- **"Reached" is vendor-wide**: a later Interested, Not Qualified or Callback Requested outcome on any
  of the vendor's call tasks stops a pending follow-up.
- **Add-on waitlists** do not trigger an email; there is no automatic email when a waitlisted line is
  later confirmed or released (confirmation emails belong to a later slice).
- **Edited draft HTML is not filtered.** Team members are trusted to edit drafts; the frontend must
  show HTML in a sandboxed frame.
- **Stale drafts are cleaned up by the worker** (every minute when the timer runs). If the timer is
  stopped, stale drafts stay visible but cannot be approved.
- **Approvals wait for the send window.** A draft approved in the evening is sent the next morning;
  the daily limit still applies.
- **One pending manual draft** per template, application and line.
- **Overview counts are all-time**; there is no date range yet.
- **No frontend yet**: the screens are described in [email-frontend-guide.md](email-frontend-guide.md).
  Until they exist, drafts can be reviewed in Django admin (Email messages → Approve / Cancel).
