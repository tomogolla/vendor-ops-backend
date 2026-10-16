"""Slice 2: application email triggers, stop conditions, draft review and the email API.

Triggers run through `transaction.on_commit`, so every workflow call here is wrapped in
captureOnCommitCallbacks(execute=True), exactly as a committed request would run them. Sending
uses Django's in-memory backend; no real email is sent."""

from datetime import date, timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from catalog.models import AddOn, BoothVariant, MarketWeekend
from imports import services as imports_services
from tasks.models import Task
from tasks.services import log_call
from vendors import pipeline
from vendors.models import ActivityEvent, Application, ApplicationLine, Vendor
from vendors.services import submit_application

from .models import EmailDraftRevision, EmailMessage, EmailRecipientPreference, EmailSettings, EmailTemplate
from .services import approve_draft, cancel_message, prepare_draft, revise_draft
from .tests import NOON_NY, EmailTestCase

Event = ActivityEvent.EventType
Status = EmailMessage.Status
LineStatus = ApplicationLine.Status
Stage = Application.Stage
APP_TEMPLATES = ['application_received', 'call_follow_up', 'waitlist_update', 'application_declined']


class ApplicationEmailTestCase(EmailTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.oct17 = MarketWeekend.objects.create(label='Oct 17 & 18', start_date=date(2026, 10, 17), end_date=date(2026, 10, 18))
        cls.small = BoothVariant.objects.create(weekend=cls.weekend, name='8ft x 8ft', price_cents=15000, capacity=5)
        cls.large = BoothVariant.objects.create(weekend=cls.oct17, name='10ft x 10ft', price_cents=22500, capacity=5)
        cls.tent = AddOn.objects.create(weekend=cls.weekend, name='Tent rental', price_cents=4000)

    def setUp(self):
        super().setUp()
        for key in APP_TEMPLATES:
            self.set_mode(key, EmailTemplate.Mode.DRAFT)

    def committed(self, function, *args, **kwargs):
        """Runs a workflow call and the on_commit hooks it registered."""
        with self.captureOnCommitCallbacks(execute=True):
            return function(*args, **kwargs)

    def messages(self, key, **filters):
        return EmailMessage.objects.filter(template_key=key, **filters)

    def submit(self, brand='Juniper', email='ana@juniper.example.com', **kwargs):
        return self.committed(
            submit_application, actor=self.member, vendor_data={'brand_name': brand, 'email': email}, **kwargs,
        )

    def line(self, application, variant=None):
        return self.committed(pipeline.add_line, application, actor=self.member, variant=variant or self.small)

    def api(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.member)
        return client


# --- A. Application received ------------------------------------------------------------------

class ApplicationReceivedTests(ApplicationEmailTestCase):
    def test_new_application_gets_one_draft_and_bundled_submissions_none(self):
        application = self.submit()
        drafts = self.messages('application_received', application=application)
        self.assertEqual(drafts.count(), 1)
        draft = drafts.get()
        self.assertEqual(draft.status, Status.DRAFT)
        self.assertIn('Juniper', draft.subject)
        self.assertIn('APP-', draft.text_body)
        bundled = self.submit()  # same contact: bundled into the open application
        self.assertTrue(bundled.bundled)
        self.assertEqual(self.messages('application_received').count(), 1)

    def test_duplicate_trigger_and_template_off(self):
        from . import application_emails

        application = self.submit()
        self.committed(application_emails.application_submitted, application, self.member)
        self.assertEqual(self.messages('application_received').count(), 1)
        self.set_mode('application_received', EmailTemplate.Mode.OFF)
        self.submit('Other', 'other@example.com')
        self.assertEqual(self.messages('application_received').count(), 1, 'Off creates nothing')

    def test_csv_import_and_backfill(self):
        csv = 'Brand,Email\nFresh Goods,fresh@example.com\n'
        upload = SimpleUploadedFile('rows.csv', csv.encode(), content_type='text/csv')
        self.committed(imports_services.confirm, upload.read(), actor=self.admin, file_name='rows.csv')
        self.assertEqual(self.messages('application_received', vendor__brand_name='Fresh Goods').count(), 1)
        # Backfilled (historical) applications never get an acknowledgment.
        Vendor.objects.create(brand_name='Old Lead', email='old@example.com')
        before = EmailMessage.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            call_command('backfill_applications')
        self.assertEqual(EmailMessage.objects.count(), before)

    def test_missing_invalid_and_suppressed_recipients(self):
        for index, address in enumerate(['', 'not-an-email', 'blocked@example.com']):
            if address == 'blocked@example.com':
                EmailRecipientPreference.objects.create(email=address, suppressed=True, suppression_reason='complaint')
            application = self.submit(f'Brand {index}', address)
            message = self.messages('application_received', application=application).get()
            self.assertEqual(message.status, Status.SKIPPED, address)
            self.assertIn(message.failure_code, ('missing_email', 'invalid_email', 'suppressed'))
        self.assertFalse(Task.objects.filter(task_type=Task.TaskType.EMAIL_REVIEW, vendor__brand_name__startswith='Brand').exists())


# --- B. Call follow-up ----------------------------------------------------------------------------

class CallFollowUpTests(ApplicationEmailTestCase):
    def call_task(self, application):
        return application.tasks.get(task_type=Task.TaskType.DISCOVERY_CALL)

    def log(self, task, outcome, **kwargs):
        task.refresh_from_db()
        return self.committed(log_call, task, self.member, outcome, **kwargs)

    def test_only_no_answer_gets_a_follow_up_once_per_call(self):
        application = self.submit()
        task = self.call_task(application)
        self.log(task, 'No Answer', notes='Rang twice')
        draft = self.messages('call_follow_up').get()
        self.assertEqual((draft.application, draft.trigger['attempt'], draft.trigger['call_task_id']), (application, 1, str(task.pk)))
        self.log(task, 'No Answer')
        self.assertEqual(self.messages('call_follow_up').count(), 1, 'one automatic follow-up per call task')
        task.refresh_from_db()
        self.assertIn('Rang twice', task.notes, 'call notes are kept')

    def test_answered_calls_get_no_follow_up(self):
        for index, outcome in enumerate(['Interested', 'Not Qualified', 'Wrong Number']):
            application = self.submit(f'Answered {index}', f'answered{index}@example.com')
            self.log(self.call_task(application), outcome)
        callback = self.submit('Callback', 'callback@example.com')
        task = self.log(self.call_task(callback), 'Callback Requested', callback_at=NOON_NY + timedelta(days=1))
        self.assertEqual((task.status, task.callback_at), (Task.Status.RESCHEDULED, NOON_NY + timedelta(days=1)))
        self.assertFalse(self.messages('call_follow_up').exists())

    def test_follow_up_goes_stale_once_the_vendor_is_reached(self):
        application = self.submit()
        task = self.call_task(application)
        self.log(task, 'No Answer')
        draft = self.messages('call_follow_up').get()
        self.log(task, 'Interested')
        with self.assertRaisesMessage(ValidationError, 'no longer applies'):
            approve_draft(draft, actor=self.admin)
        summary = self.run_worker()
        draft.refresh_from_db()
        self.assertEqual((summary.stale_cancelled, draft.status), (1, Status.CANCELLED))
        self.assertEqual(draft.review_task.status, Task.Status.CANCELLED)


# --- C. Waitlist update ----------------------------------------------------------------------------

class WaitlistTests(ApplicationEmailTestCase):
    def test_each_booth_line_gets_its_own_waitlist_email(self):
        application = self.submit()
        first, second = self.line(application, self.small), self.line(application, self.large)
        self.committed(pipeline.waitlist_line, first, actor=self.admin)
        self.committed(pipeline.waitlist_line, second, actor=self.admin)
        by_line = {m.application_line_id: m for m in self.messages('waitlist_update')}
        self.assertEqual(set(by_line), {first.pk, second.pk})
        message = by_line[first.pk]
        self.assertEqual(message.market_weekend, self.weekend)
        self.assertIn('8ft x 8ft', message.text_body)
        self.assertIn('Oct 10 – 11, 2026', message.text_body)
        self.assertIn('10ft x 10ft', by_line[second.pk].subject)
        # Retrying the same transition creates nothing new.
        from . import application_emails

        first.refresh_from_db()
        self.committed(application_emails.line_waitlisted, first, self.admin)
        self.assertEqual(self.messages('waitlist_update').count(), 2)
        self.assertEqual(Application.objects.get(pk=application.pk).stage, Stage.NEW, 'the application itself is not waitlisted')

    def test_add_on_waitlist_gets_no_automatic_email(self):
        application = self.submit()
        booth = self.line(application)
        add_on = self.committed(pipeline.add_line, application, actor=self.member, add_on=self.tent, parent_line=booth)
        self.committed(pipeline.waitlist_line, add_on, actor=self.admin)
        self.assertFalse(self.messages('waitlist_update').exists())

    def test_waitlist_email_is_not_sent_once_the_line_is_confirmed(self):
        self.set_mode('waitlist_update', EmailTemplate.Mode.AUTO)
        application = self.submit()
        line = self.line(application)
        self.committed(pipeline.waitlist_line, line, actor=self.admin)
        message = self.messages('waitlist_update').get()
        self.assertEqual(message.status, Status.QUEUED)
        self.make_due(message)
        pipeline.confirm_line(line, actor=self.admin)
        self.run_worker()
        message.refresh_from_db()
        self.assertEqual((message.status, message.failure_code), (Status.SKIPPED, 'stop_condition'))
        self.assertIn('no longer waitlisted', message.failure_detail)
        self.assertEqual(len(mail.outbox), 0)


# --- D. Application declined -------------------------------------------------------------------------

class DeclinedTests(ApplicationEmailTestCase):
    def decline(self, application, reason=''):
        return self.committed(pipeline.change_application_stage, Application.objects.get(pk=application.pk), Stage.DECLINED,
                              self.admin, reason=reason)

    def test_one_draft_per_decline_without_internal_reasons(self):
        application = self.submit()
        self.decline(application, reason='Photos looked mass-produced (internal)')
        draft = self.messages('application_declined').get()
        for body in (draft.subject, draft.text_body, draft.html_body):
            self.assertNotIn('mass-produced', body)
        self.assertEqual(draft.trigger['decided_at'], Application.objects.get(pk=application.pk).decided_at.isoformat())

    def test_withdrawn_and_line_declines_do_not_trigger(self):
        withdrawn = self.submit()
        self.committed(pipeline.change_application_stage, withdrawn, Stage.WITHDRAWN, self.admin)
        other = self.submit('Lines', 'lines@example.com')
        self.committed(pipeline.decline_line, self.line(other), actor=self.admin, reason='Full')
        self.assertFalse(self.messages('application_declined').exists())

    def test_reopen_and_decline_again_is_a_new_decision(self):
        application = self.submit()
        self.decline(application)
        first = self.messages('application_declined').get()
        self.committed(pipeline.change_application_stage, Application.objects.get(pk=application.pk), Stage.IN_REVIEW, self.admin)
        with self.assertRaisesMessage(ValidationError, 'no longer applies'):
            approve_draft(first, actor=self.admin)
        self.decline(application)
        self.assertEqual(self.messages('application_declined').count(), 2)
        self.run_worker()
        first.refresh_from_db()
        self.assertEqual(first.status, Status.CANCELLED, 'the earlier decision\'s draft is stale')
        self.assertEqual(self.messages('application_declined', status=Status.DRAFT).count(), 1)

    def test_received_email_is_stopped_after_a_decline(self):
        self.set_mode('application_received', EmailTemplate.Mode.AUTO)
        application = self.submit()
        message = self.messages('application_received').get()
        self.make_due(message)
        self.decline(application)
        self.run_worker()
        message.refresh_from_db()
        self.assertEqual((message.status, message.failure_code), (Status.SKIPPED, 'stop_condition'))


# --- Drafts: review, revisions, tasks -----------------------------------------------------------------

class DraftReviewTests(ApplicationEmailTestCase):
    def draft(self, **kwargs):
        application = kwargs.pop('application', None) or self.submit()
        return self.messages('application_received', application=application).get()

    def test_review_task_assignment(self):
        reviewer = self.submit()
        Application.objects.filter(pk=reviewer.pk).update(reviewer=self.member)
        self.assertEqual(prepare_draft('call_follow_up', application=Application.objects.get(pk=reviewer.pk),
                                       actor=self.admin).review_task.owner, self.member)
        owned = self.committed(submit_application, actor=self.admin, vendor=Vendor.objects.create(
            brand_name='Owned', email='owned@example.com', owner=self.owner))
        self.assertEqual(self.messages('application_received', application=owned).get().review_task.owner, self.owner)
        self.assertIsNone(self.draft().review_task.owner, 'no reviewer or owner: team queue')

    def test_revisions_keep_every_version(self):
        draft = self.draft()
        original = (draft.subject, draft.text_body)
        revise_draft(draft, actor=self.member, subject='Thanks for applying!', html_body='<p>Edited</p>',
                     text_body='Edited', note='Friendlier')
        revise_draft(draft, actor=self.member, subject='Thanks again', html_body='<p>Edited 2</p>', text_body='Edited 2')
        draft.refresh_from_db()
        self.assertEqual((draft.subject, draft.revision), ('Thanks again', 2))
        revisions = list(EmailDraftRevision.objects.filter(message=draft).order_by('number'))
        self.assertEqual([(r.number, r.subject) for r in revisions], [(1, original[0]), (2, 'Thanks for applying!')])
        self.assertEqual(revisions[0].text_body, original[1])
        self.assertEqual(revisions[0].replaced_by, self.member)
        self.assertEqual(ActivityEvent.objects.filter(event_type=Event.EMAIL_REVISED).count(), 2)
        with self.assertRaises(Exception):
            revisions[0].delete()

    def test_only_drafts_can_be_edited_and_other_fields_stay_immutable(self):
        draft = self.draft()
        approve_draft(draft, actor=self.admin)
        with self.assertRaisesMessage(ValidationError, 'Only drafts'):
            revise_draft(draft, actor=self.admin, subject='x', html_body='x', text_body='x')
        other = self.draft(application=self.submit('Other', 'other@example.com'))
        other.recipient_email = 'someone-else@example.com'
        other._revising = True
        with self.assertRaises(Exception):
            other.save()

    def test_approve_and_cancel_close_the_review_task(self):
        approved = self.draft()
        approve_draft(approved, actor=self.admin)
        approved.refresh_from_db()
        self.assertEqual((approved.status, approved.review_task.status), (Status.QUEUED, Task.Status.COMPLETED))
        cancelled = self.draft(application=self.submit('Other', 'other@example.com'))
        cancel_message(cancelled, actor=self.admin, reason='Not needed')
        cancelled.refresh_from_db()
        self.assertEqual((cancelled.status, cancelled.review_task.status), (Status.CANCELLED, Task.Status.CANCELLED))
        types = set(ActivityEvent.objects.filter(event_type__startswith='EMAIL_').values_list('event_type', flat=True))
        self.assertTrue({Event.EMAIL_DRAFTED, Event.EMAIL_QUEUED, Event.EMAIL_CANCELLED} <= types)

    def test_task_updates_cannot_bypass_the_draft(self):
        draft = self.draft()
        task = draft.review_task
        admin = self.api(self.admin)
        response = admin.patch(f'/api/v1/tasks/{task.pk}/', {'status': 'COMPLETED'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Approve or cancel', str(response.json()))
        self.assertEqual(admin.patch(f'/api/v1/tasks/{task.pk}/', {'notes': 'Looking at it'}, format='json').status_code, 200)
        created = admin.post('/api/v1/tasks/', {'vendor': str(draft.vendor_id), 'title': 'x', 'task_type': 'EMAIL_REVIEW'},
                             format='json')
        self.assertEqual(created.status_code, 400)

    def test_permissions_for_approving(self):
        draft = self.draft()
        with self.assertRaises(PermissionDenied):
            approve_draft(draft, actor=self.member)
        Application.objects.filter(pk=draft.application_id).update(reviewer=self.member)
        draft = EmailMessage.objects.get(pk=draft.pk)
        self.assertEqual(approve_draft(draft, actor=self.member).status, Status.QUEUED, 'the application reviewer may approve')


# --- Sending modes -------------------------------------------------------------------------------------

class SendingModeTests(ApplicationEmailTestCase):
    def approved(self):
        application = self.submit()
        message = self.messages('application_received', application=application).get()
        approve_draft(message, actor=self.admin)
        self.make_due(message)
        return message

    def set_admin_mode(self, mode):
        config = EmailSettings.load()
        config.sending_mode = mode
        config.save()

    def test_disabled_redirect_enabled(self):
        message = self.approved()
        self.set_admin_mode('disabled')
        self.run_worker()
        self.assertEqual((EmailMessage.objects.get(pk=message.pk).status, len(mail.outbox)), (Status.QUEUED, 0))
        self.set_admin_mode('redirect')
        with override_settings(EMAIL_REDIRECT_TO='team@example.com'):
            self.run_worker()
        self.assertEqual(mail.outbox[-1].to, ['Vendor Ops redirect <team@example.com>'])
        other = self.submit('Enabled', 'enabled@example.com')
        enabled = self.messages('application_received', application=other).get()
        approve_draft(enabled, actor=self.admin)
        self.make_due(enabled)
        self.set_admin_mode('enabled')
        self.run_worker()
        self.assertIn('enabled@example.com', mail.outbox[-1].to[0])


# --- API ---------------------------------------------------------------------------------------------

class EmailApiTests(ApplicationEmailTestCase):
    def test_application_emails_list_prepare_edit_approve_cancel(self):
        application = self.submit()
        url = f'/api/v1/applications/{application.pk}/emails/'
        member = self.api(self.member)
        listing = member.get(url).json()
        self.assertEqual(listing['count'], 1)
        item = listing['results'][0]
        self.assertEqual((item['template_key'], item['status'], item['review_task']['status']),
                         ('application_received', 'draft', 'TODO'))
        self.assertEqual(item['permissions'], {'can_edit': True, 'can_approve': False, 'can_cancel': False})

        # Manual draft; a second identical request is refused while the first awaits review.
        created = member.post(url, {'template_key': 'call_follow_up'}, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(member.post(url, {'template_key': 'call_follow_up'}, format='json').status_code, 400)
        missing = member.post(url, {'template_key': 'waitlist_update'}, format='json')
        self.assertEqual(missing.status_code, 400)
        self.assertIn('booth', str(missing.json()))

        message_url = f'/api/v1/email/messages/{item["id"]}/'
        edited = member.patch(message_url, {'subject': 'Thanks for applying', 'note': 'Shorter'}, format='json')
        self.assertEqual((edited.status_code, edited.json()['revision']), (200, 1))
        self.assertEqual(member.get(f'{message_url}revisions/').json()[0]['number'], 1)
        self.assertEqual(member.post(f'{message_url}approve/').status_code, 403)
        approved = self.api(self.admin).post(f'{message_url}approve/')
        self.assertEqual((approved.status_code, approved.json()['status']), (200, 'queued'))
        self.assertEqual(member.patch(message_url, {'subject': 'Too late'}, format='json').status_code, 400)
        cancelled = self.api(self.admin).post(f'{message_url}cancel/', {'reason': 'Duplicate'}, format='json')
        self.assertEqual(cancelled.json()['status'], 'cancelled')

    def test_message_filters_and_overview(self):
        first = self.submit()
        second = self.submit('Other', 'other@example.com')
        approve_draft(self.messages('application_received', application=second).get(), actor=self.admin)
        member = self.api()
        ids = lambda **params: {m['id'] for m in member.get('/api/v1/email/messages/', params).json()['results']}  # noqa: E731
        self.assertEqual(len(ids(status='draft')), 1)
        self.assertEqual(len(ids(application=str(first.pk))), 1)
        self.assertEqual(len(ids(template='application_received')), 2)
        self.assertEqual(len(ids(bucket='queued')), 1)
        message = EmailMessage.objects.get(application=second)
        EmailMessage.objects.filter(pk=message.pk).update(status=Status.DELIVERED)
        overview = member.get('/api/v1/email/overview/').json()
        self.assertEqual(overview['counts']['awaiting_review'], 1)
        self.assertEqual(overview['counts']['sent'], 1, 'delivered is counted once, as sent')
        self.assertEqual(overview['counts']['queued'], 0)
        self.assertTrue(overview['recent_activity'])

    def test_templates_api_validation_and_preview(self):
        admin, member = self.api(self.admin), self.api(self.member)
        templates = member.get('/api/v1/email/templates/').json()
        self.assertEqual(len(templates), 10)
        self.assertTrue(all(t['mode'] in ('off', 'draft') for t in templates))
        url = '/api/v1/email/templates/application_received/'
        self.assertEqual(member.patch(url, {'mode': 'auto'}, format='json').status_code, 403)
        bad = admin.patch(url, {'subject_template': 'Hi {secret_token}'}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('Unknown placeholder', str(bad.json()))
        self.assertEqual(admin.patch(url, {'mode': 'off'}, format='json').json()['mode'], 'off')

        sample = member.post(f'{url}preview/', {}, format='json').json()
        self.assertEqual((sample['data_source'], sample['missing_variables']), ('sample', []))
        self.assertIn('Sample Vendor Co.', sample['subject'])
        application = self.submit()
        line = self.line(application, self.large)
        real = member.post('/api/v1/email/templates/waitlist_update/preview/',
                           {'application': str(application.pk), 'application_line': str(line.pk)}, format='json').json()
        self.assertIn('10ft x 10ft', real['subject'])
        no_line = member.post('/api/v1/email/templates/waitlist_update/preview/',
                              {'application': str(application.pk)}, format='json').json()
        self.assertEqual(no_line['missing_variables'], ['booth_name', 'market_dates', 'market_name'])
        unsaved = member.post(f'{url}preview/', {'subject_template': 'Hello {nope}'}, format='json')
        self.assertEqual(unsaved.status_code, 400)

    def test_settings_api(self):
        member, admin = self.api(self.member), self.api(self.admin)
        with override_settings(EMAIL_GLOBAL_MODE='redirect', ANYMAIL={'RESEND_API_KEY': 're_secret_key', 'RESEND_SIGNING_SECRET': 'whsec_x'}):
            body = member.get('/api/v1/email/settings/').json()
            self.assertEqual((body['sending_mode'], body['effective']['mode']), ('enabled', 'redirect'))
            self.assertTrue(body['live_sending_blocked'])
            self.assertIn('EMAIL_GLOBAL_MODE=redirect', ' '.join(body['notices']))
            self.assertNotIn('re_secret_key', str(body))
            self.assertNotIn('whsec_x', str(body))
        self.assertEqual(member.patch('/api/v1/email/settings/', {'sending_mode': 'disabled'}, format='json').status_code, 403)
        bad = admin.patch('/api/v1/email/settings/', {'send_window_start': '19:00'}, format='json')
        self.assertEqual(bad.status_code, 400)
        ok = admin.patch('/api/v1/email/settings/', {'sending_mode': 'disabled'}, format='json')
        self.assertEqual((ok.status_code, ok.json()['sending_mode']), (200, 'disabled'))


class MigrationDefaultsTests(EmailTestCase):
    def test_application_templates_stay_off_and_waitlist_names_the_booth(self):
        for key in APP_TEMPLATES:
            self.assertEqual(EmailTemplate.objects.get(key=key).mode, EmailTemplate.Mode.OFF, key)
        waitlist = EmailTemplate.objects.get(key='waitlist_update')
        self.assertIn('booth_name', waitlist.allowed_variables)
        self.assertIn('{booth_name}', waitlist.text_template)
        waitlist.full_clean()

    def test_customised_waitlist_copy_is_not_overwritten(self):
        import importlib

        from django.apps import apps as global_apps

        migration = importlib.import_module('communications.migrations.0005_waitlist_booth_name')
        template = EmailTemplate.objects.get(key='waitlist_update')
        # The team's own wording on top of the original (pre-booth) template.
        seeded_variables = [name for name in template.allowed_variables if name != 'booth_name']
        EmailTemplate.objects.filter(pk=template.pk).update(
            subject_template=migration.SEED['subject_template'], html_template=migration.SEED['html_template'],
            text_template='Our own words for {vendor_name}.', allowed_variables=seeded_variables,
        )
        migration.forwards(global_apps, None)
        template.refresh_from_db()
        self.assertEqual(template.text_template, 'Our own words for {vendor_name}.')
        self.assertEqual(template.subject_template, migration.SEED['subject_template'])
        self.assertIn('booth_name', template.allowed_variables)
        self.assertEqual(template.mode, EmailTemplate.Mode.OFF)
        # Untouched seeded copy is upgraded to name the booth.
        EmailTemplate.objects.filter(pk=template.pk).update(text_template=migration.SEED['text_template'],
                                                            allowed_variables=seeded_variables)
        migration.forwards(global_apps, None)
        template.refresh_from_db()
        self.assertEqual(template.text_template, migration.NEW_COPY['text_template'])
