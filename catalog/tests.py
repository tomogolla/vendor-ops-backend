from config.testing import ApiTestCase

from .models import AddOn, BoothVariant, MarketWeekend

WEEKENDS = '/api/v1/market-weekends/'


class CatalogApiTests(ApiTestCase):
    def setUp(self):
        self.admin_client = self.as_user(self.admin)
        response = self.admin_client.post(WEEKENDS, {
            'label': 'Oct 10 & 11', 'start_date': '2026-10-10', 'end_date': '2026-10-11', 'status': 'OPEN',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.weekend = response.json()

    def url(self, *parts):
        return WEEKENDS + self.weekend['id'] + '/' + ''.join(f'{part}/' for part in parts)

    def test_admin_creates_variants_and_add_ons(self):
        variant = self.admin_client.post(self.url('variants'), {
            'name': '8ft x 8ft', 'dimensions': '8 x 8 ft', 'price_cents': 15000, 'capacity': 20,
        }, format='json')
        self.assertEqual(variant.status_code, 201, variant.content)
        add_on = self.admin_client.post(self.url('add-ons'), {'name': 'Tent rental', 'price_cents': 4000}, format='json')
        self.assertEqual(add_on.status_code, 201, add_on.content)

        body = self.as_user(self.member).get(self.url()).json()
        self.assertEqual(body['currency'], 'USD')
        self.assertEqual(
            {k: body['variants'][0][k] for k in ('name', 'capacity', 'confirmed', 'remaining', 'waitlisted')},
            {'name': '8ft x 8ft', 'capacity': 20, 'confirmed': 0, 'remaining': 20, 'waitlisted': 0},
        )
        self.assertEqual((body['add_ons'][0]['capacity'], body['add_ons'][0]['remaining']), (None, None))

    def test_variant_name_unique_per_weekend(self):
        payload = {'name': '8ft x 8ft', 'price_cents': 15000, 'capacity': 20}
        self.assertEqual(self.admin_client.post(self.url('variants'), payload, format='json').status_code, 201)
        self.assertEqual(self.admin_client.post(self.url('variants'), payload, format='json').status_code, 400)
        other = MarketWeekend.objects.create(label='Oct 17 & 18', start_date='2026-10-17', end_date='2026-10-18')
        response = self.admin_client.post(f'{WEEKENDS}{other.pk}/variants/', payload, format='json')
        self.assertEqual(response.status_code, 201)

    def test_validation(self):
        bad_dates = {'label': 'Backwards', 'start_date': '2026-10-11', 'end_date': '2026-10-10'}
        self.assertEqual(self.admin_client.post(WEEKENDS, bad_dates, format='json').status_code, 400)
        negative = {'name': 'Big', 'price_cents': -1, 'capacity': 2}
        self.assertEqual(self.admin_client.post(self.url('variants'), negative, format='json').status_code, 400)
        self.assertEqual(
            self.admin_client.post(self.url('variants'), {'name': 'Big', 'price_cents': 100, 'capacity': -1},
                                   format='json').status_code, 400,
        )

    def test_members_read_only(self):
        member = self.as_user(self.member)
        self.assertEqual(member.get(WEEKENDS).status_code, 200)
        self.assertEqual(member.post(WEEKENDS, {'label': 'X'}, format='json').status_code, 403)
        self.assertEqual(member.patch(self.url(), {'status': 'CLOSED'}, format='json').status_code, 403)
        self.assertEqual(
            member.post(self.url('variants'), {'name': 'A', 'price_cents': 1, 'capacity': 1}, format='json').status_code,
            403,
        )

    def test_update_and_delete_unused_products(self):
        variant = BoothVariant.objects.create(
            weekend_id=self.weekend['id'], name='10ft x 10ft', price_cents=20000, capacity=5,
        )
        add_on = AddOn.objects.create(weekend_id=self.weekend['id'], name='Table', price_cents=1000, capacity=3)
        response = self.admin_client.patch(self.url('variants', variant.pk), {'price_cents': 22000}, format='json')
        self.assertEqual(response.json()['price_cents'], 22000)
        self.assertEqual(self.admin_client.get(self.url('add-ons', add_on.pk)).json()['remaining'], 3)
        self.assertEqual(self.admin_client.delete(self.url('variants', variant.pk)).status_code, 204)
        self.assertEqual(self.admin_client.delete(self.url('add-ons', add_on.pk)).status_code, 204)
        self.assertEqual(self.admin_client.get(self.url('variants', 'not-a-uuid')).status_code, 404)
