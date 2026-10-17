"""Step 5: POST /api/v1/applications/{id}/lines-from-requests/ and the staff view of web applications."""

from datetime import date, timedelta

from catalog.models import BoothVariant, MarketWeekend
from config.testing import ApiTestCase
from vendors.models import ActivityEvent, Application, ApplicationLine, Vendor


class LinesFromRequestsTests(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = date.today()
        weekend = lambda label, days, status='OPEN': MarketWeekend.objects.create(  # noqa: E731
            label=label, start_date=today + timedelta(days=days), end_date=today + timedelta(days=days + 1), status=status)
        cls.first, cls.second, cls.third, cls.closed = weekend('First', 10), weekend('Second', 20), weekend('Third', 30), weekend('Closed', 40, 'CLOSED')
        cls.small_1 = BoothVariant.objects.create(weekend=cls.first, name='8ft x 8ft', price_cents=15000, capacity=0)
        cls.small_2 = BoothVariant.objects.create(weekend=cls.second, name='8ft x 8ft', price_cents=15000, capacity=3)
        cls.large_3 = BoothVariant.objects.create(weekend=cls.third, name='10ft x 10ft', price_cents=22500, capacity=3)
        BoothVariant.objects.create(weekend=cls.closed, name='8ft x 8ft', price_cents=15000, capacity=3)

    def setUp(self):
        vendor = Vendor.objects.create(brand_name='Juniper', email='ana@example.com')
        self.application = Application.objects.create(vendor=vendor, source='Website form')
        self.application.requested_weekends.add(self.first, self.second, self.third, self.closed)
        ApplicationLine.objects.create(application=self.application, variant=self.small_2, quantity=1, unit_price_cents=15000)
        self.url = f'/api/v1/applications/{self.application.pk}/lines-from-requests/'

    def test_creates_lines_and_explains_skips(self):
        response = self.as_user(self.member).post(self.url, {'booth_size': '8ft x 8ft'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual([(line['product_name'], line['weekend']['label'], line['status']) for line in body['created']],
                         [('8ft x 8ft', 'First', 'REQUESTED')])  # full weekends are fine: requested lines use no capacity
        self.assertEqual([(item['weekend']['label'], item['reason']) for item in body['skipped']], [
            ('Second', 'already has an open booth line'),
            ('Third', '8ft x 8ft is not offered that weekend'),
            ('Closed', 'the weekend is closed'),
        ])
        event = ActivityEvent.objects.get(event_type=ActivityEvent.EventType.LINES_FROM_REQUESTS)
        self.assertEqual((event.application, event.actor, event.metadata['booth_size']), (self.application, self.member, '8ft x 8ft'))
        self.assertEqual(len(event.metadata['skipped']), 3)
        again = self.as_user(self.member).post(self.url, {'booth_size': '8ft x 8ft'}, format='json').json()
        self.assertEqual(again['created'], [])

    def test_validation_and_stage(self):
        self.assertEqual(self.as_user(self.member).post(self.url, {}, format='json').status_code, 400)
        Application.objects.filter(pk=self.application.pk).update(stage=Application.Stage.DECLINED)
        self.assertEqual(self.as_user(self.member).post(self.url, {'booth_size': '8ft x 8ft'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.url, {'booth_size': '8ft x 8ft'}, format='json').status_code, 401)

    def test_staff_list_and_detail_show_requested_weekends_and_campaign(self):
        client = self.as_user(self.member)
        detail = client.get(f'/api/v1/applications/{self.application.pk}/').json()
        row = next(item for item in client.get('/api/v1/applications/').json()['results'] if item['id'] == str(self.application.pk))
        for data in (detail, row):
            self.assertEqual([weekend['label'] for weekend in data['requested_weekends']], ['First', 'Second', 'Third', 'Closed'])
            self.assertEqual(data['campaign'], '')
            self.assertEqual(len(data['lines']), 1)
