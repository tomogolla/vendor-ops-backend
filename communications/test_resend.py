"""Resend provider, delivery webhooks, the daily limit and send_test_email. Nothing here calls
Resend: sends go through Anymail's test backend (or a scripted subclass of it) and webhook calls
are signed locally with a test secret."""

import base64
import json
from datetime import datetime, timedelta
from io import StringIO

from anymail.backends.test import EmailBackend as AnymailTestBackend
from anymail.exceptions import AnymailAPIError
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone
from svix.webhooks import Webhook

from vendors.models import ActivityEvent

from . import sending
from .models import EmailMessage, EmailRecipientPreference, EmailSettings
from .providers import EmailSendError, OutboundEmail, ResendProvider
from .tests import NOON_NY, NY, EmailTestCase

Event = ActivityEvent.EventType
Status = EmailMessage.Status
TEST_BACKEND = 'communications.test_resend.ScriptedAnymailBackend'
SIGNING_SECRET = 'whsec_' + base64.b64encode(b'vendor-ops-test-signing-secret!!').decode()
WEBHOOK_URL = '/api/v1/webhooks/anymail/resend/tracking/'
RESEND_SETTINGS = {
    'EMAIL_PROVIDER': 'resend',
    'EMAIL_RESEND_BACKEND': TEST_BACKEND,
    'ANYMAIL': {'RESEND_API_KEY': 're_test_not_real', 'RESEND_SIGNING_SECRET': SIGNING_SECRET},
}


class ScriptedAnymailBackend(AnymailTestBackend):
    """Anymail's test backend, optionally failing the next sends with scripted API errors."""

    script: list = []

    def post_to_esp(self, payload, message):
        if ScriptedAnymailBackend.script:
            response = ScriptedAnymailBackend.script.pop(0)
            if response is not None:
                message.anymail_test_response = response
        return super().post_to_esp(payload, message)


def api_error(status: int) -> AnymailAPIError:
    return AnymailAPIError(f'HTTP {status}', status_code=status)


@override_settings(**RESEND_SETTINGS)
class ResendTestCase(EmailTestCase):
    def setUp(self):
        super().setUp()
        ScriptedAnymailBackend.script = []

    def queued(self, event_key='evt-1', **kwargs):
        message = self.queue(event_key=event_key, **kwargs).message
        self.make_due(message)
        return message


class ResendProviderTests(ResendTestCase):
    EMAIL = OutboundEmail('Ana', 'ana@example.com', 'The Good Flea', 'ops@example.com', 'hello@example.com',
                          'Subject', '<p>Hi</p>', 'Hi', {'X-Vendor-Ops-Message': '1'})

    def test_sends_through_anymail_and_returns_the_provider_id(self):
        result = ResendProvider().send(self.EMAIL)
        self.assertEqual(result.provider, 'resend')
        self.assertEqual(result.message_id, '0')  # Anymail's test backend id: the outbox position
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['Ana <ana@example.com>'])
        self.assertNotIn('Message-ID', sent.extra_headers, 'Resend assigns the message id')
        self.assertEqual(sent.anymail_test_params['reply_to'][0].addr_spec, 'hello@example.com')

    def test_classifies_api_errors(self):
        cases = [(429, 'rate_limited', True, True), (500, 'provider_unavailable', True, False),
                 (401, 'auth_failed', False, False), (422, 'provider_rejected', False, False)]
        for status, code, transient, retry_later in cases:
            ScriptedAnymailBackend.script = [api_error(status)]
            with self.subTest(status=status), self.assertRaises(EmailSendError) as caught:
                ResendProvider().send(self.EMAIL)
            error = caught.exception
            self.assertEqual((error.code, error.transient, error.retry_later), (code, transient, retry_later))
            self.assertNotIn('re_test_not_real', error.detail)

    def test_worker_sends_with_resend(self):
        message = self.queued()
        summary = self.run_worker()
        message.refresh_from_db()
        self.assertEqual((summary.sent, message.status, message.provider, message.provider_message_id), (1, Status.SENT, 'resend', '0'))


class RateLimitTests(ResendTestCase):
    def test_429_releases_the_batch_without_using_attempts(self):
        first, second = self.queued(), self.queued(event_key='evt-2')
        ScriptedAnymailBackend.script = [api_error(429)]
        summary = self.run_worker()
        self.assertEqual((summary.sent, summary.failed, summary.deferred), (0, 0, 2))
        self.assertIn('rate limit', summary.note)
        for message in (first, second):
            message.refresh_from_db()
            self.assertEqual((message.status, message.attempt_count), (Status.QUEUED, 0))
            self.assertEqual(message.scheduled_for, NOON_NY + timedelta(minutes=1))
        self.assertFalse(self.events(Event.EMAIL_FAILED).exists())
        # Repeated rate limits never exhaust the retries.
        for _ in range(5):
            ScriptedAnymailBackend.script = [api_error(429)]
            self.make_due(first)
            self.make_due(second)
            self.run_worker()
        first.refresh_from_db()
        self.assertEqual((first.status, first.attempt_count), (Status.QUEUED, 0))
        self.make_due(first)
        self.make_due(second)
        self.assertEqual(self.run_worker().sent, 2)


class DailyLimitTests(ResendTestCase):
    def test_limit_leaves_the_rest_queued_and_says_so(self):
        messages = [self.queued(event_key=f'e{index}') for index in range(4)]
        with override_settings(EMAIL_DAILY_LIMIT=3):
            summary = self.run_worker()
            self.assertEqual((summary.sent, summary.deferred, summary.failed, summary.skipped), (3, 1, 0, 0))
            self.assertEqual(
                EmailMessage.objects.filter(status=Status.SENT, sent_at=NOON_NY).count(),
                3,
                'sent messages use the worker clock used by the daily-limit calculation',
            )
            self.assertIn('daily limit of 3 reached', summary.note)
            self.assertIn('1 left queued', summary.note)
            again = self.run_worker()
            self.assertEqual((again.sent, again.deferred), (0, 1))
            self.assertIn('daily limit of 3 reached', again.note)
            # Still queued, not expired early, with no attempt used.
            waiting = EmailMessage.objects.get(status=Status.QUEUED)
            self.assertEqual((waiting.attempt_count, waiting.failure_code), (0, ''))
            # The next local day (midnight New York, not UTC) has a fresh allowance.
            just_before_midnight = datetime(2026, 10, 15, 23, 59, tzinfo=NY)
            self.assertEqual(sending.daily_remaining(just_before_midnight, EmailSettings.load()), 0)
            next_morning = datetime(2026, 10, 16, 10, 0, tzinfo=NY)
            self.assertEqual(self.run_worker(now=next_morning).sent, 1)
        self.assertEqual(len(messages), 4)

    def test_counts_since_local_midnight_in_the_business_time_zone(self):
        message = self.queued()
        # 03:30 UTC on the 16th is still the 15th in New York.
        EmailMessage.objects.filter(pk=message.pk).update(status=Status.SENT, sent_at=datetime(2026, 10, 16, 3, 30, tzinfo=timezone.UTC))
        config = EmailSettings.load()
        with override_settings(EMAIL_DAILY_LIMIT=1):
            self.assertEqual(sending.daily_remaining(datetime(2026, 10, 15, 23, 0, tzinfo=NY), config), 0)
            self.assertEqual(sending.daily_remaining(datetime(2026, 10, 16, 0, 30, tzinfo=NY), config), 1)
        with override_settings(EMAIL_DAILY_LIMIT=0):
            self.assertIsNone(sending.daily_remaining(NOON_NY, config), '0 means no limit')

    def test_dry_run_respects_the_limit(self):
        for index in range(3):
            self.queued(event_key=f'e{index}')
        with override_settings(EMAIL_DAILY_LIMIT=2):
            summary = self.run_worker(dry_run=True)
        self.assertEqual(summary.sent, 2)
        self.assertIn('1 left queued', summary.note)
        self.assertEqual(EmailMessage.objects.filter(status=Status.QUEUED).count(), 3)


def signed_post(client, payload: dict, *, secret=SIGNING_SECRET, tamper=False):
    body = json.dumps(payload)
    msg_id = f'msg_{payload["data"]["email_id"]}_{payload["type"]}'
    timestamp = datetime.now(tz=timezone.UTC)
    signature = Webhook(secret).sign(msg_id, timestamp, body)
    if tamper:
        body = body.replace('email.', 'email.x')
    return client.post(
        WEBHOOK_URL, body, content_type='application/json',
        HTTP_SVIX_ID=msg_id, HTTP_SVIX_TIMESTAMP=str(int(timestamp.timestamp())), HTTP_SVIX_SIGNATURE=signature,
    )


def event(kind: str, email_id: str, bounce: dict | None = None) -> dict:
    data = {'email_id': email_id, 'to': ['ana@moss.example.com'], 'created_at': '2026-10-15T16:00:00.000Z'}
    if bounce:
        data['bounce'] = bounce
    return {'type': kind, 'created_at': '2026-10-15T16:00:05.000Z', 'data': data}


HARD_BOUNCE = {'type': 'Permanent', 'subType': 'General',
               'message': "The recipient's email provider sent a hard bounce message, and there's nothing you can do."}
SOFT_BOUNCE = {'type': 'Transient', 'subType': 'MailboxFull',
               'message': "The recipient's email provider sent a bounce message because the recipient's inbox was full."}


class WebhookTests(ResendTestCase):
    def sent_message(self, email_id='em_123', event_key='evt-1', **fields):
        message = self.queued(event_key=event_key)
        EmailMessage.objects.filter(pk=message.pk).update(
            status=Status.SENT, provider='resend', provider_message_id=email_id, sent_at=NOON_NY,
            delivered_to_email='ana@moss.example.com', **fields,
        )
        return message

    def test_rejects_bad_or_missing_signatures(self):
        message = self.sent_message()
        self.assertEqual(signed_post(self.client, event('email.delivered', 'em_123'), tamper=True).status_code, 400)
        wrong = 'whsec_' + base64.b64encode(b'another-secret-another-secret!!!').decode()
        self.assertEqual(signed_post(self.client, event('email.delivered', 'em_123'), secret=wrong).status_code, 400)
        unsigned = self.client.post(WEBHOOK_URL, json.dumps(event('email.delivered', 'em_123')), content_type='application/json')
        self.assertEqual(unsigned.status_code, 400)
        with override_settings(ANYMAIL={'RESEND_API_KEY': 'x', 'RESEND_SIGNING_SECRET': None}):
            self.assertEqual(signed_post(self.client, event('email.delivered', 'em_123')).status_code, 400,
                             'no configured secret: every call is refused')
        message.refresh_from_db()
        self.assertEqual(message.status, Status.SENT)

    def test_delivered(self):
        message = self.sent_message()
        self.assertEqual(signed_post(self.client, event('email.delivered', 'em_123')).status_code, 200)
        message.refresh_from_db()
        self.assertEqual(message.status, Status.DELIVERED)
        self.assertIsNotNone(message.delivered_at)
        self.assertEqual(signed_post(self.client, event('email.delivered', 'em_unknown')).status_code, 200, 'unknown ids are ignored')

    def test_hard_bounce_suppresses_and_soft_bounce_does_not(self):
        hard = self.sent_message()
        signed_post(self.client, event('email.bounced', 'em_123', HARD_BOUNCE))
        hard.refresh_from_db()
        self.assertEqual((hard.status, hard.failure_code), (Status.BOUNCED, 'hard_bounce'))
        preference = EmailRecipientPreference.objects.get(email='ana@moss.example.com')
        self.assertEqual((preference.suppressed, preference.suppression_reason, preference.vendor), (True, 'hard_bounce', self.vendor))
        self.assertTrue(self.events(Event.EMAIL_BOUNCED).exists())
        # A later delivered event does not undo the bounce.
        signed_post(self.client, event('email.delivered', 'em_123'))
        hard.refresh_from_db()
        self.assertEqual(hard.status, Status.BOUNCED)

        EmailRecipientPreference.objects.all().delete()
        soft = self.sent_message('em_456', event_key='evt-2')
        signed_post(self.client, event('email.bounced', 'em_456', SOFT_BOUNCE))
        soft.refresh_from_db()
        self.assertEqual((soft.status, soft.failure_code), (Status.BOUNCED, 'soft_bounce'))
        self.assertFalse(EmailRecipientPreference.objects.exists())

    def test_complaint_suppresses(self):
        message = self.sent_message()
        signed_post(self.client, event('email.delivered', 'em_123'))
        signed_post(self.client, event('email.complained', 'em_123'))
        message.refresh_from_db()
        self.assertEqual(message.status, Status.COMPLAINED)
        preference = EmailRecipientPreference.objects.get(email='ana@moss.example.com')
        self.assertEqual((preference.suppressed, preference.suppression_reason), (True, 'complaint'))
        self.assertTrue(self.events(Event.EMAIL_COMPLAINED).exists())

    def test_redirected_messages_never_suppress_the_vendor(self):
        message = self.sent_message(redirected=True)
        EmailMessage.objects.filter(pk=message.pk).update(delivered_to_email='team@example.com')
        with self.assertLogs('communications.webhooks', level='WARNING'):
            signed_post(self.client, event('email.bounced', 'em_123', HARD_BOUNCE))
        message.refresh_from_db()
        self.assertEqual(message.status, Status.BOUNCED)
        self.assertFalse(EmailRecipientPreference.objects.exists())


class SendTestEmailCommandTests(ResendTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command('send_test_email', *args, stdout=out)
        return out.getvalue()

    def set_admin_mode(self, mode):
        config = EmailSettings.load()
        config.sending_mode = mode
        config.save()

    def test_sends_through_the_configured_provider(self):
        output = self.run_command('check@example.com')
        self.assertIn('Sent to check@example.com via resend', output)
        message = EmailMessage.objects.get(template_key='test_email')
        self.assertEqual((message.status, message.recipient_email, message.provider), (Status.SENT, 'check@example.com', 'resend'))
        self.assertEqual(mail.outbox[0].to, ['check@example.com'])

    def test_disabled_mode_sends_nothing(self):
        self.set_admin_mode(EmailSettings.SendingMode.DISABLED)
        with self.assertRaisesMessage(CommandError, 'Sending is disabled'):
            self.run_command('check@example.com')
        with override_settings(EMAIL_GLOBAL_MODE='disabled'):
            self.set_admin_mode(EmailSettings.SendingMode.ENABLED)
            with self.assertRaisesMessage(CommandError, 'Sending is disabled'):
                self.run_command('check@example.com')
        self.assertEqual((len(mail.outbox), EmailMessage.objects.count()), (0, 0))

    def test_redirect_mode_goes_to_the_redirect_mailbox(self):
        self.set_admin_mode(EmailSettings.SendingMode.REDIRECT)
        with self.assertRaisesMessage(CommandError, 'no redirect address'):
            self.run_command('check@example.com')
        with override_settings(EMAIL_REDIRECT_TO='team@example.com'):
            output = self.run_command('check@example.com')
        self.assertIn('the redirect mailbox team@example.com instead of check@example.com', output)
        self.assertEqual(mail.outbox[0].to, ['Vendor Ops redirect <team@example.com>'])

    def test_failures_and_bad_input_are_clear(self):
        with self.assertRaisesMessage(CommandError, 'not a valid email address'):
            self.run_command('not-an-email')
        ScriptedAnymailBackend.script = [api_error(429)]
        with self.assertRaisesMessage(CommandError, 'Not sent: rate_limited'):
            self.run_command('check@example.com')
        message = EmailMessage.objects.get(template_key='test_email')
        self.assertEqual(message.status, Status.FAILED, 'a test email is never retried')
        with override_settings(EMAIL_DAILY_LIMIT=1):
            self.run_command('first@example.com')
            with self.assertRaisesMessage(CommandError, 'daily limit of 1 is reached'):
                self.run_command('second@example.com')


@override_settings(**RESEND_SETTINGS)
class ResendChecksTests(EmailTestCase):
    def test_resend_requires_api_key_and_sender(self):
        from .checks import email_settings_check

        ids = lambda: {problem.id for problem in email_settings_check(None)}  # noqa: E731
        self.assertNotIn('communications.E006', ids())
        with override_settings(ANYMAIL={'RESEND_API_KEY': '', 'RESEND_SIGNING_SECRET': None}, EMAIL_FROM_ADDRESS=''):
            self.assertTrue({'communications.E006', 'communications.E007', 'communications.W003'} <= ids())
        with override_settings(EMAIL_DAILY_LIMIT=-1):
            self.assertIn('communications.E008', ids())
