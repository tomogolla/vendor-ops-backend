"""Step 3: POST /api/v1/public/applications/."""

from datetime import date, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from catalog.models import BoothVariant, MarketWeekend
from communications.models import EmailMessage, EmailTemplate
from config.testing import ApiTestCase
from tasks.models import Task
from tasks.services import DUPLICATE_REVIEW_TITLE
from vendors.models import ActivityEvent, Application, ApplicationLine, InternalNote, LeadIntakeEvent, Vendor

URL = '/api/v1/public/applications/'
# Cloudflare's documented always-pass test secret. Turnstile is never contacted from tests: siteverify
# is mocked with a successful answer for the production hostname and the 'apply' action.
TEST_SECRET = '1x0000000000000000000000000000000AA'


def siteverify(success=True, codes=(), hostname='ops.thegoodflea.com', action='apply'):
    response = mock.Mock(status_code=200)
    response.raise_for_status.return_value = None
    body = {'success': success, 'error-codes': list(codes)}
    if success:
        body.update({'hostname': hostname, 'action': action, 'challenge_ts': '2026-10-17T12:00:00Z'})
    response.json.return_value = body
    return response


def payload(**overrides):
    data = {
        'first_name': 'Ana', 'last_name': 'Silva', 'email': 'Ana@Juniper.example.com', 'phone': '(555) 123-4567',
        'brand_name': 'Juniper Goods', 'instagram': '@Juniper.Goods', 'weekend_ids': [], 'booth_size': 'not_sure',
        'consent': True, 'turnstile_token': 'XXXX.DUMMY.TOKEN.XXXX', 'website': '',
    }
    data.update(overrides)
    return data


# Limits are relaxed here; test_protection.py covers them and Turnstile itself.
@override_settings(PUBLIC_APPLICATIONS_ENABLED=True, DEBUG=False, TURNSTILE_SECRET_KEY=TEST_SECRET, PUBLIC_APPLICATIONS_EMAIL_LIMIT_PER_DAY=50)
class PublicSubmissionTestCase(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = date.today()
        cls.first = MarketWeekend.objects.create(label='First', start_date=today + timedelta(days=10), end_date=today + timedelta(days=11), status='OPEN')
        cls.second = MarketWeekend.objects.create(label='Second', start_date=today + timedelta(days=20), end_date=today + timedelta(days=21), status='OPEN')
        cls.closed = MarketWeekend.objects.create(label='Closed', start_date=today + timedelta(days=30), end_date=today + timedelta(days=31), status='CLOSED')
        cls.small_1 = BoothVariant.objects.create(weekend=cls.first, name='8ft x 8ft', price_cents=15000, capacity=1)
        cls.large_1 = BoothVariant.objects.create(weekend=cls.first, name='10ft x 10ft', price_cents=22500, capacity=1)
        cls.small_2 = BoothVariant.objects.create(weekend=cls.second, name='8ft x 8ft', price_cents=15000, capacity=0)

    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        rates = mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'public_apply': '1000/hour'})
        rates.start()
        self.addCleanup(rates.stop)
        patcher = mock.patch('public_api.turnstile.requests.post', return_value=siteverify())
        self.siteverify = patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, **overrides):
        overrides.setdefault('weekend_ids', [str(self.first.pk), str(self.second.pk)])
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(URL, payload(**overrides), format='json', HTTP_USER_AGENT='TestBrowser/1.0')


class ValidationTests(PublicSubmissionTestCase):
    def errors(self, **overrides):
        response = self.submit(**overrides)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Application.objects.exists())
        return response.json()

    def test_each_required_field(self):
        for field in ('first_name', 'last_name', 'email', 'phone', 'brand_name', 'instagram', 'booth_size', 'consent'):
            data = payload(weekend_ids=[str(self.first.pk)])
            data.pop(field)
            response = self.client.post(URL, data, format='json')
            self.assertEqual(response.status_code, 400, field)
            self.assertIn(field, response.json())
        self.assertIn('weekend_ids', self.errors(weekend_ids=[]))
        self.assertIn('first_name', self.errors(first_name='\x00\x01 '))

    def test_bad_email_phone_and_instagram(self):
        self.assertIn('email', self.errors(email='not-an-email'))
        self.assertIn('phone', self.errors(phone='12345'))
        self.assertIn('phone', self.errors(phone='1234567890123456'))
        for handle in ('bad handle!', '@' + 'a' * 31, 'https://instagram.com/', 'x/y'):
            self.assertIn('instagram', self.errors(instagram=handle), handle)

    def test_instagram_forms_are_normalised(self):
        for index, handle in enumerate(['@Juniper.Goods', 'https://www.instagram.com/juniper.goods/?hl=en', 'instagram.com/juniper.goods']):
            self.assertEqual(self.submit(instagram=handle, email=f'ana{index}@example.com', phone=f'555123456{index}').status_code, 200)
        self.assertEqual(set(Vendor.objects.values_list('instagram', flat=True)), {'juniper.goods'})

    def test_consent_weekends_and_booth_size(self):
        self.assertIn('consent', self.errors(consent=False))
        self.assertIn('weekend_ids', self.errors(weekend_ids=[str(self.closed.pk)]))
        self.assertIn('weekend_ids', self.errors(weekend_ids=['8d7f8b6e-0000-4000-8000-000000000000']))
        many = [str(MarketWeekend.objects.create(label=f'W{i}', start_date=date.today() + timedelta(days=50 + i),
                                                  end_date=date.today() + timedelta(days=50 + i), status='OPEN').pk) for i in range(13)]
        self.assertIn('weekend_ids', self.errors(weekend_ids=many))
        self.assertIn('booth_size', self.errors(booth_size='Giant tent'))
        self.assertIn('booth_size', self.errors(booth_size='10ft x 10ft', weekend_ids=[str(self.second.pk)]))

    def test_overlong_values_are_refused_and_errors_reveal_nothing(self):
        self.assertIn('brand_name', self.errors(brand_name='b' * 201))
        Vendor.objects.create(brand_name='Existing', email='taken@example.com')
        body = self.errors(email='taken@example.com', consent=False)
        self.assertEqual(list(body), ['consent'])


class SubmissionTests(PublicSubmissionTestCase):
    def test_size_creates_one_requested_line_per_weekend_offering_it(self):
        self.assertEqual(self.submit(booth_size='10ft x 10ft').json(), {'status': 'received'})
        application = Application.objects.get()
        self.assertEqual(application.source, 'Website form')
        self.assertEqual(application.stage, Application.Stage.NEW)
        self.assertEqual([(line.variant, line.status) for line in application.lines.all()], [(self.large_1, 'REQUESTED')])
        self.assertEqual(set(application.requested_weekends.all()), {self.first, self.second})
        self.assertEqual(application.submitted_by.username, 'website-form')
        self.assertTrue(Task.objects.filter(application=application, task_type=Task.TaskType.DISCOVERY_CALL).exists())
        vendor = application.vendor
        self.assertEqual((vendor.email, vendor.phone, vendor.lead_source), ('ana@juniper.example.com', '(555) 123-4567', 'Website form'))

    def test_full_weekend_is_fine_and_not_sure_creates_no_lines(self):
        self.submit(booth_size='8ft x 8ft')
        application = Application.objects.get()
        self.assertEqual(sorted(line.variant.weekend.label for line in application.lines.all()), ['First', 'Second'])
        self.assertTrue(all(line.status == 'REQUESTED' for line in application.lines.all()))
        self.submit(booth_size='not_sure', email='other@example.com', phone='5559998888', instagram='other')
        other = Application.objects.get(vendor__email='other@example.com')
        self.assertFalse(other.lines.exists())
        self.assertEqual(set(other.requested_weekends.all()), {self.first, self.second})
        self.assertEqual(other.booth_requirements, {'size': 'not sure yet'})

    def test_attribution_and_consent_are_recorded_without_ip(self):
        self.client.post(URL, payload(weekend_ids=[str(self.first.pk)], attribution={'utm_campaign': 'fall-ads', 'gclid': 'g' * 400, 'ip': '203.0.113.7'}),
                         format='json', HTTP_USER_AGENT='Mozilla/5.0 ' + 'u' * 500, REMOTE_ADDR='203.0.113.7', HTTP_X_FORWARDED_FOR='198.51.100.4')
        event = LeadIntakeEvent.objects.get()
        self.assertEqual((event.source_detail, event.metadata['utm_campaign'], len(event.metadata['gclid'])), ('Website form', 'fall-ads', 255))
        self.assertEqual(len(event.metadata['user_agent']), 300)
        self.assertEqual(event.metadata['consent']['text_version'], '2026-10')
        stored = str(LeadIntakeEvent.objects.values_list('metadata', flat=True)[0]) + str(list(ActivityEvent.objects.values_list('metadata', flat=True)))
        for ip in ('203.0.113.7', '198.51.100.4'):
            self.assertNotIn(ip, stored)
        self.assertEqual(self.as_user(self.member).get(f'/api/v1/applications/{Application.objects.get().pk}/').json()['campaign'], 'fall-ads')

    def test_identical_response_for_new_existing_bundled_and_ambiguous(self):
        responses = [self.submit()]  # new vendor
        responses.append(self.submit(booth_size='8ft x 8ft'))  # same vendor again: bundled
        Application.objects.update(stage=Application.Stage.WON)
        responses.append(self.submit())  # existing vendor, nothing open: new application
        Vendor.objects.create(brand_name='Phone Twin', phone='555-000-1111')
        responses.append(self.submit(phone='555-000-1111'))  # email matches one vendor, phone another
        bodies = {(response.status_code, response.content) for response in responses}
        self.assertEqual(bodies, {(200, b'{"status":"received"}')})
        self.assertEqual(Vendor.objects.filter(email_normalized='ana@juniper.example.com').count(), 1)

    def test_repeat_applicant_is_bundled_and_existing_lines_are_skipped(self):
        self.submit(booth_size='8ft x 8ft', weekend_ids=[str(self.first.pk)])
        self.submit(booth_size='8ft x 8ft', weekend_ids=[str(self.first.pk), str(self.second.pk)])
        application = Application.objects.get()
        self.assertEqual(application.lines.filter(variant=self.small_1).count(), 1)
        self.assertTrue(application.lines.filter(variant=self.small_2).exists())
        self.assertEqual(set(application.requested_weekends.all()), {self.first, self.second})
        bundled = ActivityEvent.objects.get(event_type=ActivityEvent.EventType.APPLICATION_BUNDLED)
        self.assertEqual(bundled.metadata['skipped_existing_variant_ids'], [str(self.small_1.pk)])
        self.assertEqual(LeadIntakeEvent.objects.count(), 2)

    def test_ambiguous_match_attaches_to_email_match_with_note_and_task(self):
        by_email = Vendor.objects.create(brand_name='Email Match', email='ana@juniper.example.com')
        Vendor.objects.create(brand_name='Phone Match', phone='5551234567')
        self.assertEqual(self.submit().json(), {'status': 'received'})
        application = Application.objects.get()
        self.assertEqual(application.vendor, by_email)
        note = InternalNote.objects.get(vendor=by_email)
        self.assertIn('Phone also matches Phone Match', note.body)
        self.assertEqual(note.application, application)
        task = Task.objects.get(title=DUPLICATE_REVIEW_TITLE)
        self.assertEqual((task.vendor, task.application, task.owner, task.task_type), (by_email, application, None, Task.TaskType.FOLLOW_UP))
        event = LeadIntakeEvent.objects.get(vendor=by_email)
        self.assertEqual(len(event.metadata['possible_duplicates']), 1)
        self.assertEqual(event.metadata['submitted_contact']['phone'], '(555) 123-4567')

    def test_honeypot_creates_nothing(self):
        with self.assertLogs('public_api.views', 'INFO') as logs:
            response = self.submit(website='http://spam.example.com', first_name='Spammy')
        self.assertEqual(response.json(), {'status': 'received'})
        self.assertFalse(Vendor.objects.exists())
        self.assertNotIn('spam.example.com', ''.join(logs.output))
        self.assertNotIn('Spammy', ''.join(logs.output))

    def test_logs_only_the_application_id(self):
        with self.assertLogs('public_api.views', 'INFO') as logs:
            self.submit()
        text = ''.join(logs.output)
        self.assertIn(str(Application.objects.get().pk), text)
        for personal in ('Ana', 'Silva', 'juniper', '555', 'DUMMY'):
            self.assertNotIn(personal, text)

    def test_atomic_nothing_saved_when_a_step_fails(self):
        with mock.patch('public_api.services.add_note', side_effect=RuntimeError('boom')):
            Vendor.objects.create(brand_name='Email Match', email='ana@juniper.example.com')
            Vendor.objects.create(brand_name='Phone Match', phone='5551234567')
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertFalse(Application.objects.exists())
        self.assertFalse(LeadIntakeEvent.objects.exists())

    def test_application_received_email_for_new_not_bundled(self):
        EmailTemplate.objects.filter(key='application_received').update(mode='draft')
        self.submit()
        self.assertEqual(EmailMessage.objects.filter(template_key='application_received').count(), 1)
        self.submit()  # bundled
        self.assertEqual(EmailMessage.objects.filter(template_key='application_received').count(), 1)
        self.assertEqual(EmailTemplate.objects.get(key='application_received').mode, 'draft')

    def test_received_email_respects_template_off(self):
        self.submit()
        self.assertFalse(EmailMessage.objects.exists())
