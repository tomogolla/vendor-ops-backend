"""Step 4: Turnstile, kill switch, throttles (not spoofable), per-email limit, daily cap, checks."""

from unittest import mock

import environ
import requests
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from config.testing import ApiTestCase
from vendors.models import Application, Vendor

from ..checks import public_form_check
from .test_submit import URL, PublicSubmissionTestCase, payload

# Cloudflare's documented always-pass test secret; requests.post is mocked, nothing leaves the machine.
SECRET = '1x0000000000000000000000000000000AA'


def siteverify(success=True, codes=()):
    response = mock.Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {'success': success, 'error-codes': list(codes)}
    return response


@override_settings(DEBUG=False, TURNSTILE_SECRET_KEY=SECRET)
class TurnstileTests(PublicSubmissionTestCase):
    def test_pass_sends_secret_token_and_the_nginx_client_ip(self):
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify()) as post:
            response = self.client.post(URL, payload(weekend_ids=[str(self.first.pk)]), format='json',
                                        HTTP_X_FORWARDED_FOR='10.9.9.9, 203.0.113.5')
        self.assertEqual(response.json(), {'status': 'received'})
        self.assertTrue(Application.objects.exists())
        self.assertEqual(post.call_args.args[0], 'https://challenges.cloudflare.com/turnstile/v0/siteverify')
        self.assertEqual(post.call_args.kwargs['data'], {'secret': SECRET, 'response': 'XXXX.DUMMY.TOKEN.XXXX', 'remoteip': '203.0.113.5'})
        self.assertEqual(post.call_args.kwargs['timeout'], 5)

    def test_no_reliable_ip_means_no_remoteip(self):
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify()) as post:
            self.client.post(URL, payload(weekend_ids=[str(self.first.pk)]), format='json', REMOTE_ADDR='')
        self.assertNotIn('remoteip', post.call_args.kwargs['data'])

    def test_failure_refuses_and_creates_nothing(self):
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify(False, ['invalid-input-response'])):
            response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'We could not verify that you are not a robot. Please try again.'})
        self.assertFalse(Vendor.objects.exists())

    def test_reused_token_fails(self):
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify(False, ['timeout-or-duplicate'])):
            self.assertEqual(self.submit().status_code, 400)

    def test_unreachable_or_misconfigured_fails_closed(self):
        for effect in (requests.ConnectionError('down'), requests.Timeout('slow')):
            with mock.patch('public_api.turnstile.requests.post', side_effect=effect):
                self.assertEqual(self.submit().status_code, 503)
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify(False, ['invalid-input-secret'])):
            self.assertEqual(self.submit().status_code, 503)
        broken = siteverify()
        broken.json.side_effect = ValueError('not json')
        with mock.patch('public_api.turnstile.requests.post', return_value=broken):
            self.assertEqual(self.submit().status_code, 503)
        self.assertFalse(Vendor.objects.exists())

    def test_missing_token_never_calls_out(self):
        with mock.patch('public_api.turnstile.requests.post') as post:
            self.assertEqual(self.submit(turnstile_token='').status_code, 400)
        post.assert_not_called()

    @override_settings(TURNSTILE_SECRET_KEY='')
    def test_production_without_secret_refuses(self):
        with mock.patch('public_api.turnstile.requests.post') as post:
            self.assertEqual(self.submit().status_code, 503)
        post.assert_not_called()
        self.assertFalse(Vendor.objects.exists())


class LimitTests(PublicSubmissionTestCase):
    @override_settings(PUBLIC_APPLICATIONS_ENABLED=False)
    def test_kill_switch(self):
        self.assertEqual(self.submit().status_code, 404)
        self.assertFalse(Vendor.objects.exists())

    @override_settings(PUBLIC_APPLICATIONS_DAILY_CAP=1)
    def test_daily_cap(self):
        self.assertEqual(self.submit().status_code, 200)
        with self.assertLogs('public_api.views', 'WARNING') as logs:
            response = self.submit(email='second@example.com', phone='5550001111', instagram='second')
        self.assertEqual(response.status_code, 503)
        self.assertIn('daily cap', logs.output[0])
        self.assertEqual(Application.objects.count(), 1)
        Application.objects.update(source='Staff entry')  # only website applications count
        self.assertEqual(self.submit(email='third@example.com', phone='5550002222', instagram='third').status_code, 200)

    @override_settings(PUBLIC_APPLICATIONS_EMAIL_LIMIT_PER_DAY=3)
    def test_three_per_email_per_day(self):
        for _ in range(3):
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit(email='ANA@juniper.example.com ').status_code, 429)
        self.assertEqual(self.submit(email='someone@example.com', phone='5550003333', instagram='someone').status_code, 200)
        keys = list(cache._cache)  # LocMemCache under tests
        self.assertTrue(keys)
        self.assertFalse(any('juniper' in str(key) or 'someone' in str(key) for key in keys))

    @override_settings(PUBLIC_APPLICATIONS_EMAIL_LIMIT_PER_DAY=1)
    def test_failed_validation_does_not_use_up_the_email_limit(self):
        for _ in range(3):
            self.assertEqual(self.submit(consent=False).status_code, 400)
        self.assertEqual(self.submit().status_code, 200)


@override_settings(PUBLIC_APPLICATIONS_ENABLED=True, DEBUG=True, TURNSTILE_SECRET_KEY='')
class ThrottleIdentityTests(ApiTestCase):
    def setUp(self):
        cache.clear()
        rates = mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'public_apply': '2/hour', 'public_weekends': '2/hour'})
        rates.start()
        self.addCleanup(rates.stop)

    def post(self, forwarded):
        # The honeypot answers quickly and saves nothing; the throttle runs before it.
        return APIClient().post(URL, payload(website='bot'), format='json', HTTP_X_FORWARDED_FOR=forwarded, REMOTE_ADDR='')

    def test_spoofed_forwarded_prefix_does_not_change_the_identity(self):
        self.assertEqual(settings.REST_FRAMEWORK['NUM_PROXIES'], 1)
        self.assertEqual(self.post('1.1.1.1, 203.0.113.9').status_code, 200)
        self.assertEqual(self.post('2.2.2.2, 203.0.113.9').status_code, 200)
        self.assertEqual(self.post('3.3.3.3, 4.4.4.4, 203.0.113.9').status_code, 429)
        self.assertEqual(self.post('203.0.113.9').status_code, 429)
        self.assertEqual(self.post('1.1.1.1, 203.0.113.10').status_code, 200, 'a different client as seen by nginx')

    def test_weekends_throttle(self):
        client = APIClient()
        for _ in range(2):
            self.assertEqual(client.get('/api/v1/public/market-weekends/', HTTP_X_FORWARDED_FOR='203.0.113.9').status_code, 200)
        self.assertEqual(client.get('/api/v1/public/market-weekends/', HTTP_X_FORWARDED_FOR='9.9.9.9, 203.0.113.9').status_code, 429)


class SettingsTests(ApiTestCase):
    def test_check_errors_when_on_in_production_without_secret(self):
        with override_settings(PUBLIC_APPLICATIONS_ENABLED=True, DEBUG=False, TURNSTILE_SECRET_KEY=''):
            self.assertEqual([problem.id for problem in public_form_check(None)], ['public_api.E001'])
        with override_settings(PUBLIC_APPLICATIONS_ENABLED=True, DEBUG=False, TURNSTILE_SECRET_KEY=SECRET):
            self.assertEqual([problem.id for problem in public_form_check(None)], ['public_api.W001'])
        with override_settings(PUBLIC_APPLICATIONS_ENABLED=True, DEBUG=False, TURNSTILE_SECRET_KEY='0x4AAA-real-looking'):
            self.assertEqual(public_form_check(None), [])
        for enabled, debug in ((False, False), (True, True)):
            with override_settings(PUBLIC_APPLICATIONS_ENABLED=enabled, DEBUG=debug, TURNSTILE_SECRET_KEY=''):
                self.assertEqual(public_form_check(None), [])

    def test_shared_cache_outside_tests_memory_in_tests(self):
        self.assertEqual(settings.CACHES['default']['BACKEND'], 'django.core.cache.backends.locmem.LocMemCache')
        production = environ.Env.cache_url_config('dbcache://django_cache')
        self.assertEqual((production['BACKEND'], production['LOCATION']), ('django.core.cache.backends.db.DatabaseCache', 'django_cache'))
