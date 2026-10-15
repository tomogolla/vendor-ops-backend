"""Email foundation (Slice 1). No test contacts a real mail server: the console provider uses
Django's in-memory backend under the test runner, and the SMTP relay provider runs against a fake
smtplib.SMTP."""

import smtplib
import threading
from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from catalog.models import MarketWeekend
from config.testing import PASSWORD
from tasks.models import Task
from vendors.models import ActivityEvent, Application, Vendor

from . import sending, stop_conditions
from .models import EmailMessage, EmailRecipientPreference, EmailSettings, EmailTemplate
from .providers import EmailSendError, OutboundEmail, SmtpRelayProvider
from .rendering import MissingVariableError, TemplateSyntaxError, placeholders, render, template_errors
from .services import approve_draft, build_context, cancel_message, queue_email, queue_email_on_commit
from .windows import in_window, next_send_time

Event = ActivityEvent.EventType
Status = EmailMessage.Status
NY = ZoneInfo('America/New_York')
NOON_NY = datetime(2026, 10, 15, 12, 0, tzinfo=NY)  # inside the default 09:00-18:00 window
SEEDED_KEYS = [
    'application_received', 'call_follow_up', 'waitlist_update', 'application_declined', 'market_preparation',
    'market_information_packet', 'post_market_follow_up', 'invoice_sent', 'payment_reminder', 'payment_received',
]

EMAIL_ENV = {
    'EMAIL_PROVIDER': 'console',
    'EMAIL_GLOBAL_MODE': 'enabled',
    'EMAIL_FROM_ADDRESS': 'ops@example.com',
    'EMAIL_REPLY_TO': 'hello@example.com',
    'EMAIL_REDIRECT_TO': '',
}


@override_settings(**EMAIL_ENV)
class EmailTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user('admin', password=PASSWORD, role=User.Role.ADMINISTRATOR, is_staff=True)
        cls.member = User.objects.create_user('member', password=PASSWORD, role=User.Role.TEAM_MEMBER, is_staff=True)
        cls.owner = User.objects.create_user('owner', password=PASSWORD, first_name='Wambui', last_name='Kariuki')
        cls.vendor = Vendor.objects.create(
            brand_name='Moss & <Thread>', first_name='Ana', last_name='Lopez', email='ana@moss.example.com', owner=cls.owner,
        )
        cls.application = Application.objects.create(vendor=cls.vendor, reviewer=cls.admin)
        cls.weekend = MarketWeekend.objects.create(
            label='Oct 10 & 11', start_date=date(2026, 10, 10), end_date=date(2026, 10, 11),
        )

    def setUp(self):
        config = EmailSettings.load()
        config.sending_mode = EmailSettings.SendingMode.ENABLED
        config.save()

    def set_mode(self, key, mode):
        template = EmailTemplate.objects.get(key=key)
        template.mode = mode
        template.save()
        return template

    def queue(self, key='application_received', mode=EmailTemplate.Mode.AUTO, event_key='evt-1', **kwargs):
        self.set_mode(key, mode)
        kwargs.setdefault('vendor', self.vendor)
        kwargs.setdefault('application', self.application)
        return queue_email(key, event_key=event_key, actor=self.admin, **kwargs)

    def make_due(self, message):
        EmailMessage.objects.filter(pk=message.pk).update(scheduled_for=NOON_NY - timedelta(minutes=1))

    def run_worker(self, **kwargs):
        kwargs.setdefault('now', NOON_NY)
        return sending.process_due_emails(**kwargs)

    def events(self, event_type):
        return ActivityEvent.objects.filter(vendor=self.vendor, event_type=event_type)


# --- Rendering and template validation ------------------------------------------------------

class RenderingTests(EmailTestCase):
    def test_render_escapes_html_only_in_html(self):
        values = {'vendor_name': 'Moss & <Thread>'}
        self.assertEqual(render('Hi {vendor_name}', values, escape_html=False), 'Hi Moss & <Thread>')
        self.assertEqual(render('<p>Hi {vendor_name}</p>', values, escape_html=True), '<p>Hi Moss &amp; &lt;Thread&gt;</p>')
        self.assertEqual(render('{{literal}} {vendor_name}', values, escape_html=False), '{literal} Moss & <Thread>')

    def test_syntax_and_whitelist(self):
        self.assertEqual(placeholders('{vendor_name} {market_name} {vendor_name}'), ['vendor_name', 'market_name'])
        for bad in ('{vendor name}', '{unclosed', 'stray }', '{Vendor_Name}', '{{% if %}'):
            with self.subTest(text=bad), self.assertRaises(TemplateSyntaxError):
                placeholders(bad)
        errors = template_errors({'subject_template': 'Hi {secret_token}', 'text_template': '{market_name}'}, ['vendor_name'])
        self.assertIn('Unknown placeholder', errors['subject_template'])
        self.assertIn('not allowed for this template', errors['text_template'])
        self.assertIn('allowed_variables', template_errors({}, ['password']))

    def test_unknown_placeholders_rejected_on_save(self):
        template = EmailTemplate.objects.get(key='application_received')
        template.subject_template = 'Your {secret} for {vendor_name}'
        with self.assertRaises(ValidationError):
            template.save()
        template.refresh_from_db()
        self.assertNotIn('secret', template.subject_template)
        template.text_template = 'Django tags are not code here: {% load os %}'
        with self.assertRaises(ValidationError):
            template.save()

    def test_missing_variable(self):
        with self.assertRaises(MissingVariableError):
            render('{market_name}', {'vendor_name': 'x'}, escape_html=False)
        # waitlist_update needs a market weekend; without one nothing is created.
        result = self.queue('waitlist_update')
        self.assertEqual(result.outcome, 'rejected')
        self.assertFalse(EmailMessage.objects.exists())
        self.assertEqual(self.events(Event.EMAIL_SKIPPED).get().metadata['reason'], 'render_error')

    def test_context_only_exposes_whitelisted_values(self):
        values = build_context(self.vendor, self.application, self.weekend, {'payment_link': 'https://pay.example.com/x'})
        self.assertEqual(set(values), {'vendor_name', 'owner_name', 'application_reference', 'market_name', 'market_dates', 'payment_link'})
        self.assertEqual(values['market_dates'], 'Oct 10 – 11, 2026')
        self.assertEqual(values['owner_name'], 'Wambui Kariuki')
        self.assertRegex(values['application_reference'], r'^APP-[0-9A-F]{8}$')
        for bad in ({'email': 'x'}, {'vendor_name': 'Spoofed'}, {'payment_link': 'http://insecure.example.com'}):
            with self.subTest(extra=bad), self.assertRaises(ValueError):
                build_context(self.vendor, extra=bad)

    def test_seeded_templates_are_valid_and_off(self):
        templates = EmailTemplate.objects.filter(key__in=SEEDED_KEYS)
        self.assertEqual(sorted(templates.values_list('key', flat=True)), sorted(SEEDED_KEYS))
        for template in templates:
            with self.subTest(key=template.key):
                self.assertEqual(template.mode, EmailTemplate.Mode.OFF)
                template.full_clean()


# --- Queuing ---------------------------------------------------------------------------------

class QueueTests(EmailTestCase):
    def test_off_creates_nothing(self):
        result = self.queue(mode=EmailTemplate.Mode.OFF)
        self.assertEqual(result.outcome, 'off')
        self.assertFalse(EmailMessage.objects.exists())
        self.assertFalse(ActivityEvent.objects.filter(event_type__startswith='EMAIL_').exists())

    def test_auto_queues_a_snapshot_for_the_next_window(self):
        with mock.patch('communications.services.timezone.now', return_value=datetime(2026, 10, 15, 20, 30, tzinfo=NY)):
            result = self.queue()
        message = result.message
        self.assertEqual((result.outcome, message.status), ('queued', Status.QUEUED))
        self.assertEqual(message.scheduled_for, datetime(2026, 10, 16, 9, 0, tzinfo=NY))
        self.assertEqual(
            (message.recipient_email, message.recipient_name, message.sender_email, message.reply_to_email),
            ('ana@moss.example.com', 'Ana Lopez', 'ops@example.com', 'hello@example.com'),
        )
        self.assertIn('Moss &amp; &lt;Thread&gt;', message.html_body)
        self.assertIn('Moss & <Thread>', message.text_body)
        event = self.events(Event.EMAIL_QUEUED).get()
        self.assertEqual(event.metadata['email_message_id'], message.pk)
        self.assertNotIn('ana@moss.example.com', str(event.metadata))

    def test_snapshot_survives_template_edits_and_cannot_be_changed(self):
        message = self.queue().message
        original = message.subject
        template = EmailTemplate.objects.get(key='application_received')
        template.subject_template = 'Completely new subject for {vendor_name}'
        template.save()
        message.refresh_from_db()
        self.assertEqual(message.subject, original)
        message.subject = 'Tampered'
        with self.assertRaises(ValidationError):
            message.save()
        with self.assertRaises(ValidationError):
            message.delete()

    def test_draft_creates_exactly_one_review_task(self):
        result = self.queue(mode=EmailTemplate.Mode.DRAFT)
        self.assertEqual((result.outcome, result.message.status, result.message.scheduled_for), ('draft', Status.DRAFT, None))
        task = Task.objects.get(task_type=Task.TaskType.EMAIL_REVIEW)
        self.assertEqual((task.application, task.owner, result.message.review_task), (self.application, self.admin, task))
        self.assertEqual(self.queue(mode=EmailTemplate.Mode.DRAFT).outcome, 'duplicate')
        self.assertEqual(Task.objects.filter(task_type=Task.TaskType.EMAIL_REVIEW).count(), 1)
        self.assertTrue(self.events(Event.EMAIL_DRAFTED).exists())

    def test_draft_without_application_goes_to_the_vendor_owner(self):
        self.queue('call_follow_up', mode=EmailTemplate.Mode.DRAFT, application=None)
        self.assertEqual(Task.objects.get(task_type=Task.TaskType.EMAIL_REVIEW).owner, self.owner)

    def test_dedupe_per_template_subject_and_event(self):
        first = self.queue().message
        again = self.queue()
        self.assertEqual((again.outcome, again.message), ('duplicate', first))
        self.assertEqual(self.queue(event_key='evt-2').outcome, 'queued')
        self.assertEqual(self.queue('call_follow_up').outcome, 'queued')
        self.assertEqual(EmailMessage.objects.count(), 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailMessage.objects.create(subject='x', html_body='x', text_body='x', status=Status.SKIPPED,
                                        event_key='evt-1', dedupe_key=first.dedupe_key)

    def test_missing_invalid_and_suppressed_recipients_are_skipped(self):
        cases = [('', 'missing_email'), ('not-an-email', 'invalid_email'), ('blocked@example.com', 'suppressed')]
        EmailRecipientPreference.objects.create(
            email='Blocked@Example.com ', suppressed=True,
            suppression_reason=EmailRecipientPreference.SuppressionReason.HARD_BOUNCE,
        )
        for index, (address, code) in enumerate(cases):
            with self.subTest(address=address):
                vendor = Vendor.objects.create(brand_name=f'V{index}', email=address, instagram=f'@v{index}')
                result = self.queue('call_follow_up', vendor=vendor, application=None)
                self.assertEqual((result.outcome, result.message.failure_code), ('skipped', code))
                self.assertTrue(ActivityEvent.objects.filter(vendor=vendor, event_type=Event.EMAIL_SKIPPED).exists())

    def test_promotional_email_needs_marketing_consent(self):
        template = EmailTemplate.objects.create(
            key='spring_announcement', name='Spring announcement', category=EmailTemplate.Category.PROMOTIONAL,
            mode=EmailTemplate.Mode.AUTO, subject_template='News for {vendor_name}', html_template='<p>Hi</p>',
            text_template='Hi', allowed_variables=['vendor_name'],
        )
        self.assertEqual(queue_email(template.key, vendor=self.vendor, event_key='a').message.failure_code, 'no_marketing_consent')
        EmailRecipientPreference.objects.create(email=self.vendor.email, marketing_consent='granted')
        self.assertEqual(queue_email(template.key, vendor=self.vendor, event_key='b').outcome, 'queued')

    def test_queue_on_commit_waits_for_the_transaction(self):
        self.set_mode('application_received', EmailTemplate.Mode.AUTO)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                queue_email_on_commit('application_received', vendor=self.vendor, application=self.application, event_key='c1')
                self.assertFalse(EmailMessage.objects.exists())
        self.assertEqual(len(callbacks), 1)
        self.assertTrue(EmailMessage.objects.exists())

    def test_approve_and_cancel_are_administrator_actions(self):
        draft = self.queue(mode=EmailTemplate.Mode.DRAFT).message
        with self.assertRaises(PermissionDenied):
            approve_draft(draft, actor=self.member)
        approved = approve_draft(draft, actor=self.admin)
        self.assertEqual(approved.status, Status.QUEUED)
        self.assertEqual(approved.review_task.status, Task.Status.COMPLETED)
        cancelled = cancel_message(approved, actor=self.admin, reason='Wrong weekend')
        self.assertEqual((cancelled.status, cancelled.failure_detail), (Status.CANCELLED, 'Wrong weekend'))
        self.assertTrue(self.events(Event.EMAIL_CANCELLED).exists())


class WindowTests(TestCase):
    def test_next_send_time_uses_the_business_time_zone(self):
        start, end = time(9), time(18)
        utc = ZoneInfo('UTC')
        # 12:00 UTC is 08:00 in New York: before the window, so today's 09:00 New York.
        moment = datetime(2026, 10, 15, 12, 0, tzinfo=utc)
        self.assertEqual(next_send_time(moment, 'America/New_York', start, end), datetime(2026, 10, 15, 9, 0, tzinfo=NY))
        inside = datetime(2026, 10, 15, 14, 0, tzinfo=NY)
        self.assertEqual(next_send_time(inside, 'America/New_York', start, end), inside)
        late = datetime(2026, 10, 15, 18, 0, tzinfo=NY)
        self.assertEqual(next_send_time(late, 'America/New_York', start, end), datetime(2026, 10, 16, 9, 0, tzinfo=NY))
        self.assertFalse(in_window(late, 'America/New_York', start, end))
        # Across the November DST change the opening stays 09:00 local.
        self.assertEqual(
            next_send_time(datetime(2026, 10, 31, 20, 0, tzinfo=NY), 'America/New_York', start, end).utcoffset(),
            timedelta(hours=-5),
        )


# --- Worker ----------------------------------------------------------------------------------

class WorkerTests(EmailTestCase):
    def queued(self, **kwargs):
        message = self.queue(**kwargs).message
        self.make_due(message)
        return message

    def test_sends_due_messages_once(self):
        message = self.queued()
        summary = self.run_worker()
        self.assertEqual((summary.scanned, summary.sent, summary.failed), (1, 1, 0))
        message.refresh_from_db()
        self.assertEqual(message.status, Status.SENT)
        self.assertTrue(message.provider_message_id.startswith('<'))
        self.assertEqual((message.provider, message.delivered_to_email, message.redirected), ('console', 'ana@moss.example.com', False))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['Ana Lopez <ana@moss.example.com>'])
        self.assertEqual(mail.outbox[0].reply_to, ['hello@example.com'])
        self.assertTrue(self.events(Event.EMAIL_SENT).exists())
        self.assertEqual(self.run_worker().sent, 0, 'running again sends nothing')
        self.assertEqual(len(mail.outbox), 1)

    def test_disabled_mode_and_environment_ceiling(self):
        self.queued()
        config = EmailSettings.load()
        config.sending_mode = EmailSettings.SendingMode.DISABLED
        config.save()
        self.assertEqual((self.run_worker().deferred, len(mail.outbox)), (1, 0))
        config.sending_mode = EmailSettings.SendingMode.ENABLED
        config.save()
        with override_settings(EMAIL_GLOBAL_MODE='disabled'):
            self.assertEqual(EmailSettings.load().effective_mode, 'disabled')
            summary = self.run_worker()
        self.assertEqual((summary.sent, summary.note, len(mail.outbox)), (0, 'sending is disabled', 0))
        self.assertEqual(EmailMessage.objects.get().status, Status.QUEUED)

    def test_redirect_mode_never_sends_to_the_vendor(self):
        message = self.queued()
        config = EmailSettings.load()
        config.sending_mode = EmailSettings.SendingMode.REDIRECT
        config.save()
        self.assertEqual(self.run_worker().deferred, 1, 'no redirect address: nothing goes out')
        self.assertEqual(len(mail.outbox), 0)
        with override_settings(EMAIL_REDIRECT_TO='team@example.com'):
            self.run_worker()
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['Vendor Ops redirect <team@example.com>'])
        self.assertNotIn('ana@moss.example.com', ' '.join(sent.to + sent.cc + sent.bcc))
        self.assertTrue(sent.subject.startswith('[Redirected from ana@moss.example.com]'))
        message.refresh_from_db()
        self.assertEqual((message.redirected, message.delivered_to_email), (True, 'team@example.com'))
        self.assertEqual(message.recipient_email, 'ana@moss.example.com', 'the snapshot keeps the real recipient')

    def test_outside_the_send_window_defers(self):
        self.queued()
        summary = self.run_worker(now=datetime(2026, 10, 15, 22, 0, tzinfo=NY))
        self.assertEqual((summary.deferred, summary.sent, summary.note), (1, 0, 'outside the send window'))

    def test_dry_run_changes_nothing(self):
        message = self.queued()
        summary = self.run_worker(dry_run=True)
        self.assertEqual((summary.dry_run, summary.scanned, summary.sent), (True, 1, 1))
        message.refresh_from_db()
        self.assertEqual((message.status, message.attempt_count), (Status.QUEUED, 0))
        self.assertEqual(len(mail.outbox), 0)
        with self.assertLogs('communications.sending', level='INFO') as logs:
            call_command('send_due_emails', '--dry-run')
        self.assertIn('[dry run] send_due_emails:', logs.output[0])
        with self.assertRaises(CommandError):
            call_command('send_due_emails', '--limit', '0')

    def test_rechecks_suppression_and_stop_conditions_before_sending(self):
        suppressed = self.queued()
        EmailRecipientPreference.objects.create(email='ana@moss.example.com', suppressed=True, suppression_reason='unsubscribe')
        self.run_worker()
        suppressed.refresh_from_db()
        self.assertEqual((suppressed.status, suppressed.failure_code), (Status.SKIPPED, 'suppressed'))

        other = Vendor.objects.create(brand_name='Other', email='other@example.com')
        stopped = self.queued(key='call_follow_up', vendor=other, application=None, event_key='s1')
        broken = self.queued(
            key='post_market_follow_up', vendor=other, application=None, market_weekend=self.weekend, event_key='s2',
        )

        def already_handled(message):
            return 'Vendor already replied' if message.event_key == 's1' else None

        def crashes(message):
            raise RuntimeError('boom')

        with stop_conditions.registered('call_follow_up', already_handled), \
                stop_conditions.registered('post_market_follow_up', crashes), \
                self.assertLogs('communications.stop_conditions', level='ERROR'):
            summary = self.run_worker()
        self.assertEqual((summary.skipped, summary.sent), (2, 0))
        stopped.refresh_from_db()
        broken.refresh_from_db()
        self.assertEqual((stopped.failure_code, stopped.failure_detail), ('stop_condition', 'already_handled: Vendor already replied'))
        self.assertEqual(broken.failure_code, 'stop_condition')
        self.assertEqual(len(mail.outbox), 0)

    def test_expired_and_missing_sender_are_skipped(self):
        old = self.queued()
        EmailMessage.objects.filter(pk=old.pk).update(scheduled_for=NOON_NY - timedelta(days=4))
        with override_settings(EMAIL_FROM_ADDRESS=''):
            no_sender = self.queued(event_key='evt-2')
        self.run_worker()
        old.refresh_from_db()
        no_sender.refresh_from_db()
        self.assertEqual((old.status, old.failure_code), (Status.SKIPPED, 'expired'))
        self.assertEqual(no_sender.failure_code, 'missing_sender')

    def test_transient_failures_retry_then_fail(self):
        message = self.queued()

        class Down:
            name = 'down'

            def send(self, email):
                raise EmailSendError('connection_error', 'Could not reach the mail relay (OSError).', transient=True)

        summary = self.run_worker(provider=Down())
        message.refresh_from_db()
        self.assertEqual((summary.deferred, message.status, message.attempt_count), (1, Status.QUEUED, 1))
        self.assertGreater(message.scheduled_for, NOON_NY)
        for _ in range(2):
            self.make_due(message)
            self.run_worker(provider=Down())
        message.refresh_from_db()
        self.assertEqual((message.status, message.attempt_count, message.failure_code), (Status.FAILED, 3, 'connection_error'))
        self.assertEqual(self.events(Event.EMAIL_FAILED).count(), 1)

    def test_permanent_and_unexpected_failures(self):
        refused = self.queued()
        weird = self.queued(event_key='evt-2')

        class Picky:
            def send(self, email):
                if email.headers['X-Vendor-Ops-Message'] == str(refused.pk):
                    raise EmailSendError('recipient_refused', 'Recipient refused (550).', transient=False)
                raise KeyError('unexpected')

        with self.assertLogs('communications.sending', level='ERROR'):
            summary = self.run_worker(provider=Picky())
        refused.refresh_from_db()
        weird.refresh_from_db()
        self.assertEqual((refused.status, refused.failure_detail), (Status.FAILED, 'Recipient refused (550).'))
        self.assertEqual((weird.status, weird.failure_code), (Status.QUEUED, 'provider_error'))
        self.assertEqual((summary.failed, summary.deferred), (1, 1))

    def test_claimed_and_stuck_messages(self):
        message = self.queued()
        summary = sending.Summary()
        claimed = sending.claim(NOON_NY, 10, EmailSettings.load(), summary)
        self.assertEqual([m.pk for m in claimed], [message.pk])
        self.assertEqual(sending.claim(NOON_NY, 10, EmailSettings.load(), sending.Summary()), [], 'a claimed message is not claimed twice')
        message.refresh_from_db()
        self.assertEqual(message.status, Status.SENDING)
        later = NOON_NY + timedelta(minutes=20)
        self.run_worker(now=later)
        message.refresh_from_db()
        self.assertEqual((message.status, message.failure_code), (Status.FAILED, 'stuck_sending'))
        self.assertEqual(len(mail.outbox), 0, 'a stuck message is never re-sent')

    def test_limit_bounds_the_batch(self):
        for index in range(3):
            self.queued(event_key=f'e{index}')
        self.assertEqual(self.run_worker(limit=2).sent, 2)
        self.assertEqual(self.run_worker(limit=2).sent, 1)


@override_settings(**EMAIL_ENV)
class ConcurrentClaimTests(TransactionTestCase):
    """Two workers: rows locked by one are skipped by the other (SELECT ... FOR UPDATE SKIP LOCKED)."""

    def test_locked_rows_are_skipped(self):
        vendor = Vendor.objects.create(brand_name='Lock', email='lock@example.com')
        message = EmailMessage.objects.create(
            vendor=vendor, subject='s', html_body='h', text_body='t', status=Status.QUEUED,
            scheduled_for=NOON_NY - timedelta(minutes=1), sender_email='ops@example.com',
            recipient_email='lock@example.com', event_key='lock', dedupe_key='lock',
        )
        config = EmailSettings.load()
        result: dict = {}
        locked = threading.Event()
        release = threading.Event()

        def hold_lock():
            try:
                with transaction.atomic():
                    list(EmailMessage.objects.select_for_update().filter(pk=message.pk))
                    locked.set()
                    release.wait(10)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        locked.wait(10)
        try:
            result['claimed'] = sending.claim(NOON_NY, 10, config, sending.Summary())
        finally:
            release.set()
            holder.join()
        self.assertEqual(result['claimed'], [])
        self.assertEqual(len(sending.claim(NOON_NY, 10, config, sending.Summary())), 1)


# --- SMTP relay provider (fake smtplib) ------------------------------------------------------

class FakeSMTP:
    instances: list['FakeSMTP'] = []
    behaviour = 'ok'

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.sent, self.logged_in, self.tls = host, port, [], None, False
        FakeSMTP.instances.append(self)
        if FakeSMTP.behaviour == 'unreachable':
            raise ConnectionRefusedError('refused')

    def ehlo(self):
        return 250, b'ok'

    def starttls(self, **kwargs):
        self.tls = True

    def login(self, username, password):
        if FakeSMTP.behaviour == 'auth':
            raise smtplib.SMTPAuthenticationError(535, b'5.7.8 Username and Password not accepted')
        self.logged_in = username

    def sendmail(self, from_addr, to_addrs, msg, **kwargs):
        if FakeSMTP.behaviour == 'refused':
            raise smtplib.SMTPRecipientsRefused({to_addrs[0]: (550, b'5.1.1 No such user')})
        self.sent.append((from_addr, to_addrs, msg))
        return {}

    def quit(self):
        pass

    def close(self):
        pass


class SmtpRelayProviderTests(TestCase):
    CONFIG = {'host': 'smtp-relay.gmail.com', 'port': 587, 'use_tls': True, 'username': 'relay@example.com',
              'password': 'not-a-real-password', 'timeout': 5}
    EMAIL = OutboundEmail('Ana', 'ana@example.com', 'The Good Flea', 'ops@example.com', 'hello@example.com',
                          'Subject', '<p>Hi</p>', 'Hi', {})

    def setUp(self):
        FakeSMTP.instances = []
        FakeSMTP.behaviour = 'ok'
        patcher = mock.patch('django.core.mail.backends.smtp.smtplib.SMTP', FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_over_tls_with_a_message_id(self):
        result = SmtpRelayProvider(self.CONFIG).send(self.EMAIL)
        smtp = FakeSMTP.instances[0]
        self.assertEqual((smtp.host, smtp.port, smtp.tls, smtp.logged_in), ('smtp-relay.gmail.com', 587, True, 'relay@example.com'))
        self.assertEqual(smtp.sent[0][1], ['ana@example.com'])
        self.assertTrue(result.message_id.endswith('@example.com>'))
        self.assertIn(result.message_id.encode(), smtp.sent[0][2])

    def test_failures_are_classified_without_leaking_credentials(self):
        cases = [('refused', 'recipient_refused', False), ('auth', 'auth_failed', False), ('unreachable', 'connection_error', True)]
        for behaviour, code, transient in cases:
            FakeSMTP.behaviour = behaviour
            with self.subTest(behaviour=behaviour), self.assertRaises(EmailSendError) as caught:
                SmtpRelayProvider(self.CONFIG).send(self.EMAIL)
            self.assertEqual((caught.exception.code, caught.exception.transient), (code, transient))
            self.assertNotIn('not-a-real-password', caught.exception.detail)


# --- Administration permissions ---------------------------------------------------------------

class AdminPermissionTests(EmailTestCase):
    def client_for(self, user):
        self.client.force_login(user)
        return self.client

    def test_only_administrators_change_settings(self):
        url = f'/admin/communications/emailsettings/{EmailSettings.load().pk}/change/'
        data = {'sending_mode': 'redirect', 'redirect_to_email': 'team@example.com', 'sender_name': 'TGF', 'sender_email': '',
                'reply_to_email': '', 'business_timezone': 'America/New_York', 'send_window_start': '09:00',
                'send_window_end': '18:00'}
        member = self.client_for(self.member)
        self.assertEqual(member.get(url).status_code, 200, 'staff can look')
        self.assertEqual(member.post(url, data).status_code, 403)
        self.assertEqual(EmailSettings.load().sending_mode, 'enabled')
        admin = self.client_for(self.admin)
        self.assertEqual(admin.post(url, data).status_code, 302)
        self.assertEqual(EmailSettings.load().sending_mode, 'redirect')
        self.assertEqual(EmailSettings.load().updated_by, self.admin)
        bad = {**data, 'send_window_start': '19:00'}
        self.assertEqual(admin.post(url, bad).status_code, 200, 'invalid window is refused with form errors')

    def test_non_staff_cannot_open_the_admin(self):
        outsider = User.objects.create_user('outsider', password=PASSWORD, role=User.Role.ADMINISTRATOR)
        self.assertEqual(self.client_for(outsider).get('/admin/communications/emailtemplate/').status_code, 302)

    def test_template_edits_are_validated_and_administrator_only(self):
        template = EmailTemplate.objects.get(key='call_follow_up')
        url = f'/admin/communications/emailtemplate/{template.pk}/change/'
        data = {'name': template.name, 'description': '', 'category': template.category, 'mode': 'off', 'is_active': 'on',
                'allowed_variables': '["vendor_name"]', 'subject_template': 'Hi {vendor_name} {secret}',
                'html_template': '<p>x</p>', 'text_template': 'x'}
        self.assertEqual(self.client_for(self.member).post(url, data).status_code, 403)
        response = self.client_for(self.admin).post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Unknown placeholder')
        template.refresh_from_db()
        self.assertNotIn('secret', template.subject_template)

    def test_messages_are_read_only_with_administrator_actions(self):
        draft = self.queue(mode=EmailTemplate.Mode.DRAFT).message
        member = self.client_for(self.member)
        self.assertEqual(member.get(f'/admin/communications/emailmessage/{draft.pk}/change/').status_code, 200)
        self.assertEqual(member.get('/admin/communications/emailmessage/add/').status_code, 403)
        member.post('/admin/communications/emailmessage/', {'action': 'approve_drafts', '_selected_action': [draft.pk]})
        draft.refresh_from_db()
        self.assertEqual(draft.status, Status.DRAFT, 'team members cannot approve')
        self.client_for(self.admin).post('/admin/communications/emailmessage/', {'action': 'approve_drafts', '_selected_action': [draft.pk]})
        draft.refresh_from_db()
        self.assertEqual(draft.status, Status.QUEUED)
