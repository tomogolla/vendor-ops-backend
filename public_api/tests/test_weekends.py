"""Step 2: GET /api/v1/public/market-weekends/."""

from datetime import date, timedelta

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from catalog.models import BoothVariant, MarketWeekend
from config.testing import ApiTestCase
from vendors.models import Application, ApplicationLine, Vendor

URL = '/api/v1/public/market-weekends/'


@override_settings(PUBLIC_APPLICATIONS_ENABLED=True)
class PublicWeekendsTests(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = date.today()
        cls.later = MarketWeekend.objects.create(label='Later', start_date=today + timedelta(days=40), end_date=today + timedelta(days=41), status='OPEN')
        cls.soon = MarketWeekend.objects.create(label='Soon', start_date=today + timedelta(days=10), end_date=today + timedelta(days=11), status='OPEN')
        MarketWeekend.objects.create(label='Draft', start_date=today + timedelta(days=20), end_date=today + timedelta(days=21), status='DRAFT')
        MarketWeekend.objects.create(label='Closed', start_date=today + timedelta(days=30), end_date=today + timedelta(days=31), status='CLOSED')
        MarketWeekend.objects.create(label='Past', start_date=today - timedelta(days=10), end_date=today - timedelta(days=9), status='OPEN')
        cls.small = BoothVariant.objects.create(weekend=cls.soon, name='8ft x 8ft', dimensions='8 by 8 feet', price_cents=15000, capacity=1)
        cls.large = BoothVariant.objects.create(weekend=cls.soon, name='10ft x 10ft', dimensions='', price_cents=22500, capacity=4)
        cls.full = BoothVariant.objects.create(weekend=cls.later, name='8ft x 8ft', dimensions='', price_cents=16000, capacity=1)
        vendor = Vendor.objects.create(brand_name='Booked Co', email='booked@example.com')
        application = Application.objects.create(vendor=vendor)
        for variant in (cls.small, cls.full):
            ApplicationLine.objects.create(application=application, variant=variant, quantity=1, unit_price_cents=1, status='CONFIRMED')

    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)

    def test_only_open_upcoming_weekends_soonest_first(self):
        body = self.client.get(URL).json()
        self.assertEqual([weekend['label'] for weekend in body['weekends']], ['Soon', 'Later'])
        soon = body['weekends'][0]
        self.assertEqual(set(soon), {'id', 'label', 'start_date', 'end_date', 'variants', 'waitlist_only'})
        self.assertEqual(soon['variants'], [
            {'name': '8ft x 8ft', 'dimensions': '8 by 8 feet', 'waitlist': True},
            {'name': '10ft x 10ft', 'dimensions': '', 'waitlist': False},
        ])
        self.assertFalse(soon['waitlist_only'])
        self.assertTrue(body['weekends'][1]['waitlist_only'])
        self.assertEqual(body['booth_sizes'], [{'name': '8ft x 8ft', 'dimensions': '8 by 8 feet'}, {'name': '10ft x 10ft', 'dimensions': ''}])

    def test_never_returns_prices_capacities_counts_or_vendors(self):
        response = self.client.get(URL)
        text = response.content.decode()
        for secret in ('price', 'capacity', 'remaining', 'confirmed', 'count', '15000', '22500', 'Booked Co', 'booked@example.com'):
            self.assertNotIn(secret, text)
        self.assertEqual(response['Cache-Control'], 'public, max-age=60')

    def test_no_login_session_or_csrf_needed(self):
        response = self.client.get(URL, HTTP_COOKIE='sessionid=garbage')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('csrftoken', response.cookies)

    @override_settings(PUBLIC_APPLICATIONS_ENABLED=False)
    def test_kill_switch(self):
        self.assertEqual(self.client.get(URL).status_code, 404)
