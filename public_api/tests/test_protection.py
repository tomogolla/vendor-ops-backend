"""Turnstile (with hostname and action), kill switch, throttles (not spoofable), per-email limit,
daily cap and system checks. Cloudflare is never contacted: siteverify is mocked, and only
Cloudflare's documented test keys are used."""

from unittest import mock

import environ
import requests
from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from config.testing import ApiTestCase
from vendors.models import Application, LeadIntakeEvent, Vendor

from ..checks import public_form_check
from .test_submit import TEST_SECRET, URL, PublicSubmissionTestCase, payload, siteverify

FAIL_SECRET = '2x0000000000000000000000000000000AA'
NOT_VERIFIED = {'detail': 'We could not verify that you are not a robot. Please try again.'}


class TurnstileTests(PublicSubmissionTestCase):
    def verify_with(self, response=None, **submit):
        with mock.patch('public_api.turnstile.requests.post', return_value=response or siteverify()) as post:
            result = self.submit(**submit)
        return result, post

    def assert_refused(self, response, status=400):
        self.assertEqual(response.status_code, status, response.content)
        if status == 400:
            self.assertEqual(response.json(), NOT_VERIFIED)
        self.assertFalse(Vendor.objects.exists())
        self.assertFalse(LeadIntakeEvent.objects.exists())

    def test_pass_sends_secret_token_and_the_nginx_client_ip(self):
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify()) as post:
            response = self.client.post(URL, payload(weekend_ids=[str(self.first.pk)]), format='json',
                                        HTTP_X_FORWARDED_FOR='10.9.9.9, 203.0.113.5')
        self.assertEqual(response.json(), {'status': 'received'})
        self.assertTrue(Application.objects.exists())
        self.assertEqual(post.call_args.args[0], 'https://challenges.cloudflare.com/turnstile/v0/siteverify')
        self.assertEqual(post.call_args.kwargs['data'], {'secret': TEST_SECRET, 'response': 'XXXX.DUMMY.TOKEN.XXXX', 'remoteip': '203.0.113.5'})
        self.assertEqual(post.call_args.kwargs['timeout'], 5)

    def test_verified_before_anything_is_created(self):
        def check_nothing_yet(*args, **kwargs):
            self.assertFalse(Vendor.objects.exists())
            self.assertFalse(Application.objects.exists())
            return siteverify()
        with mock.patch('public_api.turnstile.requests.post', side_effect=check_nothing_yet) as post:
            self.assertEqual(self.submit().status_code, 200)
        post.assert_called_once()

    def test_no_reliable_ip_means_no_remoteip(self):
        with mock.patch('public_api.turnstile.requests.post', return_value=siteverify()) as post:
            self.client.post(URL, payload(weekend_ids=[str(self.first.pk)]), format='json', REMOTE_ADDR='')
        self.assertNotIn('remoteip', post.call_args.kwargs['data'])

    def test_missing_token_never_calls_out(self):
        for token in ('', None):
            response, post = self.verify_with(turnstile_token=token)
            self.assert_refused(response)
            post.assert_not_called()
        data = payload(weekend_ids=[str(self.first.pk)])
        data.pop('turnstile_token')
        with mock.patch('public_api.turnstile.requests.post') as post:
            self.assert_refused(self.client.post(URL, data, format='json'))
        post.assert_not_called()

    def test_invalid_expired_and_reused_tokens_are_refused(self):
        for codes in (['invalid-input-response'], ['timeout-or-duplicate'], ['missing-input-response'], []):
            response, _ = self.verify_with(siteverify(False, codes))
            self.assert_refused(response)

    def test_wrong_hostname_or_action_is_refused(self):
        for answer in (siteverify(hostname='evil.example.com'), siteverify(hostname=''), siteverify(action='login'), siteverify(action='')):
            response, _ = self.verify_with(answer)
            self.assert_refused(response)

    @override_settings(TURNSTILE_EXPECTED_HOSTNAMES=['ops.thegoodflea.com', 'staging.thegoodflea.com'])
    def test_any_configured_hostname_is_accepted(self):
        response, _ = self.verify_with(siteverify(hostname='staging.thegoodflea.com'))
        self.assertEqual(response.status_code, 200)

    def test_unreachable_or_misconfigured_fails_closed(self):
        for effect in (requests.ConnectionError('down'), requests.Timeout('slow')):
            with mock.patch('public_api.turnstile.requests.post', side_effect=effect):
                self.assert_refused(self.submit(), 503)
        for codes in (['invalid-input-secret'], ['missing-input-secret'], ['internal-error'], ['bad-request']):
            response, _ = self.verify_with(siteverify(False, codes))
            self.assert_refused(response, 503)
        broken = siteverify()
        broken.json.side_effect = ValueError('not json')
        self.assert_refused(self.verify_with(broken)[0], 503)
        listy = siteverify()
        listy.json.return_value = ['success']
        self.assert_refused(self.verify_with(listy)[0], 503)

    @override_settings(TURNSTILE_SECRET_KEY=FAIL_SECRET)
    def test_failing_test_secret(self):
        self.assert_refused(self.verify_with(siteverify(False, ['invalid-input-response']))[0])

    @override_settings(TURNSTILE_SECRET_KEY='')
    def test_missing_secret_refuses_in_production(self):
        response, post = self.verify_with()
        self.assert_refused(response, 503)
        post.assert_not_called()

    @override_settings(TURNSTILE_SECRET_KEY='', DEBUG=True)
    def test_missing_secret_refuses_in_development_too(self):
        response, post = self.verify_with()
        self.assert_refused(response, 503)
        post.assert_not_called()

    @override_settings(DEBUG=True)
    def test_test_secret_in_development_ignores_placeholder_hostname(self):
        response, _ = self.verify_with(siteverify(hostname='example.com', action='test'))
        self.assertEqual(response.status_code, 200)

    @override_settings(DEBUG=True, TURNSTILE_SECRET_KEY='0x4AAAAAAA-real-looking-secret')
    def test_real_secret_in_development_still_checks_hostname(self):
        self.assert_refused(self.verify_with(siteverify(hostname='localhost'))[0])

    def test_retry_with_a_new_token_after_a_failure(self):
        self.assert_refused(self.verify_with(siteverify(False, ['timeout-or-duplicate']))[0])
        response, _ = self.verify_with(turnstile_token='a-fresh-token')
        self.assertEqual(response.json(), {'status': 'received'})


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

    def test_repeat_submission_is_deduplicated_into_one_application(self):
        self.assertEqual(self.submit().json(), {'status': 'received'})
        self.assertEqual(self.submit().json(), {'status': 'received'})
        self.assertEqual(Vendor.objects.count(), 1)
        self.assertEqual(Application.objects.count(), 1)


@override_settings(PUBLIC_APPLICATIONS_ENABLED=True, TURNSTILE_SECRET_KEY=TEST_SECRET)
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
    def ids(self, **overrides):
        values = {'PUBLIC_APPLICATIONS_ENABLED': True, 'DEBUG': False, 'TURNSTILE_SECRET_KEY': '0x4AAA-real-looking',
                  'TURNSTILE_EXPECTED_HOSTNAMES': ['ops.thegoodflea.com'], 'TURNSTILE_EXPECTED_ACTION': 'apply', **overrides}
        with override_settings(**values):
            return [problem.id for problem in public_form_check(None)]

    def test_production_checks(self):
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.ids(TURNSTILE_SECRET_KEY=''), ['public_api.E001'])
        self.assertEqual(self.ids(TURNSTILE_SECRET_KEY=TEST_SECRET), ['public_api.W001'])
        self.assertEqual(self.ids(TURNSTILE_EXPECTED_HOSTNAMES=[]), ['public_api.E002'])
        self.assertEqual(self.ids(TURNSTILE_EXPECTED_ACTION=''), ['public_api.E003'])
        self.assertEqual(self.ids(PUBLIC_APPLICATIONS_ENABLED=False, TURNSTILE_SECRET_KEY=''), [])
        self.assertEqual(self.ids(DEBUG=True, TURNSTILE_SECRET_KEY=''), [])

    def test_defaults(self):
        self.assertEqual((settings.TURNSTILE_EXPECTED_HOSTNAMES, settings.TURNSTILE_EXPECTED_ACTION), (['ops.thegoodflea.com'], 'apply'))
        self.assertFalse(settings.PUBLIC_APPLICATIONS_ENABLED)

    def test_shared_cache_outside_tests_memory_in_tests(self):
        self.assertEqual(settings.CACHES['default']['BACKEND'], 'django.core.cache.backends.locmem.LocMemCache')
        production = environ.Env.cache_url_config('dbcache://django_cache')
        self.assertEqual((production['BACKEND'], production['LOCATION']), ('django.core.cache.backends.db.DatabaseCache', 'django_cache'))
