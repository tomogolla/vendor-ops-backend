"""v1.3 sales pipeline: stages, lines, capacity, entry paths, billing stubs."""

import importlib
from datetime import timedelta

from django.apps import apps as global_apps
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from catalog.models import AddOn, BoothVariant, MarketWeekend
from config.testing import ApiTestCase
from tasks.models import Task

from . import pipeline
from .models import ActivityEvent, Application, ApplicationLine, Vendor

Event = ActivityEvent.EventType
Stage = Application.Stage
LineStatus = ApplicationLine.Status


class PipelineTestCase(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.weekend = MarketWeekend.objects.create(
            label='Oct 10 & 11', start_date='2026-10-10', end_date='2026-10-11', status=MarketWeekend.Status.OPEN,
        )
        cls.booth = BoothVariant.objects.create(weekend=cls.weekend, name='8ft x 8ft', price_cents=15000, capacity=2)
        cls.big_booth = BoothVariant.objects.create(weekend=cls.weekend, name='10ft x 10ft', price_cents=22500, capacity=5)
        cls.tent = AddOn.objects.create(weekend=cls.weekend, name='Tent rental', price_cents=4000, capacity=1)
        cls.table = AddOn.objects.create(weekend=cls.weekend, name='Table', price_cents=1000)
        cls.other_weekend = MarketWeekend.objects.create(
            label='Oct 17 & 18', start_date='2026-10-17', end_date='2026-10-18', status=MarketWeekend.Status.OPEN,
        )
        cls.other_booth = BoothVariant.objects.create(
            weekend=cls.other_weekend, name='8ft x 8ft', price_cents=15000, capacity=10,
        )
        cls.other_tent = AddOn.objects.create(weekend=cls.other_weekend, name='Tent rental', price_cents=4000)

    def application(self, client=None, **payload) -> Application:
        payload.setdefault('vendor_data', {'brand_name': 'Juniper', 'email': 'ana@juniper.example.com'})
        response = (client or self.as_user(self.member)).post('/api/v1/applications/', payload, format='json')
        self.assertIn(response.status_code, (200, 201), response.content)
        return Application.objects.get(pk=response.json()['id'])

    def at_stage(self, stage, **payload) -> Application:
        """An application moved to `stage` directly, bypassing the services that normally get it there."""
        application = self.application(**payload)
        if stage != Stage.NEW:
            pipeline.move_stage(application, stage, actor=self.admin)
        return application

    def stage_url(self, application):
        return f'/api/v1/applications/{application.pk}/stage/'

    def vendor_payload(self, n):
        return {'vendor_data': {'brand_name': f'Vendor {n}', 'instagram': f'@vendor{n}'}}

    def request_line(self, application, user=None, **payload):
        if 'variant' in payload or 'add_on' in payload:
            payload = {k: (str(v.pk) if hasattr(v, 'pk') else v) for k, v in payload.items()}
        return self.as_user(user or self.member).post(
            f'/api/v1/applications/{application.pk}/lines/', payload, format='json',
        )

    def line(self, application, product=None, quantity=1, parent=None) -> ApplicationLine:
        product = product or self.booth
        field = 'variant' if isinstance(product, BoothVariant) else 'add_on'
        payload = {field: product, 'quantity': quantity}
        if parent is not None:
            payload['parent_line'] = str(parent.pk)
        response = self.request_line(application, **payload)
        self.assertEqual(response.status_code, 201, response.content)
        return ApplicationLine.objects.get(pk=response.json()['id'])

    def decide(self, line, action, user=None, **payload):
        return self.as_user(user or self.admin).post(f'/api/v1/application-lines/{line.pk}/{action}/', payload, format='json')

    def usage(self, product):
        body = self.as_user(self.member).get(f'/api/v1/market-weekends/{product.weekend_id}/').json()
        products = body['variants'] if isinstance(product, BoothVariant) else body['add_ons']
        return next(item for item in products if item['id'] == str(product.pk))


class StageTransitionTests(PipelineTestCase):
    def move(self, application, to, user=None, **extra):
        return self.as_user(user or self.admin).post(self.stage_url(application), {'stage': to, **extra}, format='json')

    def test_allowed_manual_transitions(self):
        allowed = [
            (Stage.NEW, Stage.IN_REVIEW), (Stage.NEW, Stage.DECLINED), (Stage.NEW, Stage.WITHDRAWN),
            (Stage.IN_REVIEW, Stage.DECLINED), (Stage.IN_REVIEW, Stage.WITHDRAWN),
            (Stage.DECLINED, Stage.IN_REVIEW), (Stage.WITHDRAWN, Stage.IN_REVIEW),
        ]
        for index, (start, to) in enumerate(allowed):
            with self.subTest(start=start, to=to):
                application = self.at_stage(start, vendor_data={'brand_name': f'V{index}', 'instagram': f'@v{index}'})
                response = self.move(application, to, reason='Because')
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['stage'], to)
                event = application.activity.filter(event_type=Event.APPLICATION_STAGE_CHANGED).latest('created_at')
                self.assertEqual(
                    (event.metadata['from'], event.metadata['to'], event.metadata['reason']), (start, to, 'Because'),
                )

    def test_invalid_transitions(self):
        refused = [
            (Stage.NEW, Stage.NEW), (Stage.IN_REVIEW, Stage.NEW), (Stage.DECLINED, Stage.WITHDRAWN),
            (Stage.DECLINED, Stage.NEW), (Stage.NEW, Stage.AWAITING_PAYMENT), (Stage.IN_REVIEW, Stage.WON),
        ]
        for index, (start, to) in enumerate(refused):
            with self.subTest(start=start, to=to):
                application = self.at_stage(start, vendor_data={'brand_name': f'V{index}', 'instagram': f'@v{index}'})
                self.assertEqual(self.move(application, to).status_code, 400)
                application.refresh_from_db()
                self.assertEqual(application.stage, start)

    def test_nothing_leaves_awaiting_payment_or_won_by_hand(self):
        for index, start in enumerate([Stage.AWAITING_PAYMENT, Stage.WON]):
            application = self.at_stage(start, vendor_data={'brand_name': f'V{index}', 'instagram': f'@v{index}'})
            for to in Stage.values:
                with self.subTest(start=start, to=to):
                    response = self.move(application, to)
                    self.assertEqual(response.status_code, 400)
                    if to != start:
                        self.assertIn('not designed yet', response.json()['stage'])
            with self.assertRaises(ValidationError):
                pipeline.change_application_stage(application, Stage.DECLINED, self.admin)

    def test_only_administrators_change_stage(self):
        application = self.application()
        self.assertEqual(self.move(application, Stage.IN_REVIEW, user=self.member).status_code, 403)
        with self.assertRaises(PermissionDenied):
            pipeline.change_application_stage(application, Stage.IN_REVIEW, self.member)

    def test_stage_change_updates_stage_entered_at_and_legacy_status(self):
        application = self.application()
        before = application.stage_entered_at
        self.move(application, Stage.IN_REVIEW)
        application.refresh_from_db()
        self.assertGreater(application.stage_entered_at, before)
        self.assertEqual(application.status, Application.Status.IN_REVIEW)

    def test_decline_cancels_open_call_and_review_tasks(self):
        application = self.at_stage(Stage.IN_REVIEW)
        open_types = set(application.tasks.filter(status__in=Task.OPEN_STATUSES).values_list('task_type', flat=True))
        self.assertEqual(open_types, {Task.TaskType.DISCOVERY_CALL, Task.TaskType.APPLICATION_REVIEW})
        self.move(application, Stage.DECLINED, reason='Not a fit')
        self.assertFalse(Task.objects.filter(vendor=application.vendor, status__in=Task.OPEN_STATUSES).exists())
        application.refresh_from_db()
        self.assertEqual((application.decision, application.decision_reason), ('DECLINED', 'Not a fit'))

    def test_reopen_creates_review_task(self):
        application = self.at_stage(Stage.DECLINED)
        self.assertEqual(self.move(application, Stage.IN_REVIEW).status_code, 200)
        self.assertTrue(application.tasks.filter(
            task_type=Task.TaskType.APPLICATION_REVIEW, status__in=Task.OPEN_STATUSES,
        ).exists())


class StageMappingMigrationTests(PipelineTestCase):
    def test_status_to_stage_mapping(self):
        migration = importlib.import_module('vendors.migrations.0003_map_status_to_stage')
        vendor = Vendor.objects.create(brand_name='Legacy', instagram='@legacy')
        long_ago = timezone.now() - timedelta(days=30)
        created = {}
        for status in Application.Status.values:
            application = Application.objects.create(vendor=vendor, status=status)
            Application.objects.filter(pk=application.pk).update(
                stage=Stage.NEW, legacy_status=None, stage_entered_at=None, updated_at=long_ago,
            )
            created[status] = application.pk
        already = Application.objects.create(vendor=vendor, status='APPROVED', stage=Stage.WON, legacy_status='APPROVED')

        migration.map_status_to_stage(global_apps, None)

        expected = {
            'SUBMITTED': Stage.NEW, 'IN_REVIEW': Stage.IN_REVIEW, 'WAITLISTED': Stage.IN_REVIEW,
            'APPROVED': Stage.AWAITING_PAYMENT, 'DECLINED': Stage.DECLINED, 'WITHDRAWN': Stage.WITHDRAWN,
        }
        for status, stage in expected.items():
            with self.subTest(status=status):
                application = Application.objects.get(pk=created[status])
                self.assertEqual((application.stage, application.legacy_status, application.status), (stage, status, status))
                self.assertEqual(application.stage_entered_at, long_ago)
        already.refresh_from_db()
        self.assertEqual(already.stage, Stage.WON, 'rows already mapped are left alone')


class LegacyCompatibilityTests(PipelineTestCase):
    """DEPRECATED (v1.3) read fields and POST /vendors/{id}/status/, kept for the current frontend."""

    def test_read_fields_are_derived_from_stage(self):
        expected = {
            Stage.NEW: ('SUBMITTED', 'applied'), Stage.IN_REVIEW: ('IN_REVIEW', 'in_review'),
            Stage.AWAITING_PAYMENT: ('APPROVED', 'approved'), Stage.WON: ('APPROVED', 'approved'),
            Stage.DECLINED: ('DECLINED', 'declined'), Stage.WITHDRAWN: ('WITHDRAWN', 'declined'),
        }
        client = self.as_user(self.member)
        for index, (stage, (status, display)) in enumerate(expected.items()):
            with self.subTest(stage=stage):
                application = self.at_stage(stage, vendor_data={'brand_name': f'V{index}', 'instagram': f'@v{index}'})
                self.assertEqual(client.get(f'/api/v1/applications/{application.pk}/').json()['status'], status)
                self.assertEqual(client.get(f'/api/v1/vendors/{application.vendor_id}/').json()['status'], display)

    def test_status_endpoint_supported_transitions(self):
        application = self.application()
        admin = self.as_user(self.admin)
        url = f'/api/v1/vendors/{application.vendor_id}/status/'
        self.assertEqual(admin.post(url, {'status': 'in_review'}).json()['status'], 'in_review')
        self.assertEqual(admin.post(url, {'status': 'declined', 'note': 'No'}).json()['status'], 'declined')
        self.assertEqual(admin.post(url, {'status': 'in_review'}).json()['status'], 'in_review')
        application.refresh_from_db()
        self.assertEqual(application.stage, Stage.IN_REVIEW)

        pipeline.move_stage(application, Stage.WON, actor=self.admin)
        self.assertEqual(admin.post(url, {'status': 'suspended'}).json()['status'], 'suspended')
        self.assertEqual(admin.post(url, {'status': 'approved'}).json()['status'], 'approved')

    def test_reinstating_an_unpaid_vendor_restores_its_previous_state(self):
        application = self.at_stage(Stage.AWAITING_PAYMENT)
        admin = self.as_user(self.admin)
        url = f'/api/v1/vendors/{application.vendor_id}/status/'
        before = Vendor.objects.get(pk=application.vendor_id).journey_stage
        self.assertEqual(admin.post(url, {'status': 'suspended'}).json()['journey_stage'], 'SUSPENDED')
        body = admin.post(url, {'status': 'approved'}).json()
        self.assertEqual((body['journey_stage'], body['status']), (before, 'approved'))

    def test_approved_and_waitlisted_point_to_line_decisions(self):
        application = self.at_stage(Stage.IN_REVIEW)
        for status in ('approved', 'waitlisted'):
            with self.subTest(status=status):
                response = self.as_user(self.admin).post(f'/api/v1/vendors/{application.vendor_id}/status/', {'status': status})
                self.assertEqual(response.status_code, 400)
                body = response.json()
                self.assertEqual(body['code'], 'use_line_decisions')
                self.assertIn('/application-lines/', body['status'])

    def test_legacy_status_patch(self):
        application = self.application()
        admin = self.as_user(self.admin)
        url = f'/api/v1/applications/{application.pk}/'
        self.assertEqual(admin.patch(url, {'status': 'IN_REVIEW'}, format='json').json()['stage'], Stage.IN_REVIEW)
        response = admin.patch(url, {'status': 'APPROVED'}, format='json')
        self.assertEqual((response.status_code, response.json()['code']), (400, 'use_line_decisions'))
        self.assertEqual(admin.patch(url, {'status': 'DECLINED'}, format='json').json()['stage'], Stage.DECLINED)


class DiscountValidationTests(PipelineTestCase):
    def patch(self, application, user=None, **data):
        return self.as_user(user or self.admin).patch(f'/api/v1/applications/{application.pk}/', data, format='json')

    def test_percent_and_fixed_ranges(self):
        application = self.application()
        self.assertEqual(self.patch(application, discount_type='PERCENT', discount_value='101').status_code, 400)
        self.assertEqual(self.patch(application, discount_type='PERCENT', discount_value='-1').status_code, 400)
        self.assertEqual(self.patch(application, discount_type='FIXED', discount_value='-5').status_code, 400)
        self.assertEqual(self.patch(application, discount_type='FIXED', discount_value='10.5').status_code, 400)
        body = self.patch(application, discount_type='PERCENT', discount_value='12.5', discount_label='New Vendor Trial').json()
        self.assertEqual((body['discount_type'], body['discount_value'], body['discount_label']),
                         ('PERCENT', '12.50', 'New Vendor Trial'))
        self.assertTrue(application.activity.filter(event_type=Event.APPLICATION_DISCOUNT_CHANGED).exists())

    def test_only_administrators_set_discounts(self):
        application = self.application()
        self.assertEqual(self.patch(application, user=self.member, discount_type='FIXED', discount_value='500').status_code, 403)


class LineTransitionTests(PipelineTestCase):
    def line_at(self, status, n) -> ApplicationLine:
        variant = BoothVariant.objects.create(weekend=self.weekend, name=f'Booth {n}', price_cents=100, capacity=1)
        line = self.line(self.application(**self.vendor_payload(n)), variant)
        if status == LineStatus.CONFIRMED:
            pipeline.confirm_line(line, actor=self.admin)
        elif status == LineStatus.WAITLISTED:
            pipeline.waitlist_line(line, actor=self.admin)
        elif status == LineStatus.DECLINED:
            pipeline.decline_line(line, actor=self.admin)
        elif status == LineStatus.CANCELLED:
            pipeline.confirm_line(line, actor=self.admin)
            pipeline.release_line(line, actor=self.admin, reason='Changed plans')
        line.refresh_from_db()
        self.assertEqual(line.status, status)
        return line

    def test_transition_map(self):
        actions = {'confirm': LineStatus.CONFIRMED, 'waitlist': LineStatus.WAITLISTED,
                   'decline': LineStatus.DECLINED, 'release': LineStatus.CANCELLED}
        allowed = {
            LineStatus.REQUESTED: {'confirm', 'waitlist', 'decline'},
            LineStatus.WAITLISTED: {'confirm', 'decline'},
            LineStatus.CONFIRMED: {'release'},
            LineStatus.DECLINED: set(),
            LineStatus.CANCELLED: set(),
        }
        n = 0
        for start, permitted in allowed.items():
            for action, target in actions.items():
                n += 1
                with self.subTest(start=start, action=action):
                    line = self.line_at(start, n)
                    response = self.decide(line, action, reason='Because')
                    line.refresh_from_db()
                    if action in permitted:
                        self.assertEqual(response.status_code, 200, response.content)
                        self.assertEqual(line.status, target)
                    else:
                        self.assertEqual(response.status_code, 400, response.content)
                        self.assertEqual(line.status, start)

    def test_decisions_set_timestamps_and_write_events(self):
        line = self.line(self.application(), self.big_booth)
        self.decide(line, 'waitlist')
        self.decide(line, 'confirm')
        self.decide(line, 'release', reason='Vendor cancelled')
        line.refresh_from_db()
        self.assertIsNotNone(line.waitlisted_at)
        self.assertIsNotNone(line.confirmed_at)
        self.assertIsNotNone(line.closed_at)
        self.assertEqual((line.status, line.status_reason), (LineStatus.CANCELLED, 'Vendor cancelled'))
        events = list(line.application.activity.filter(event_type__startswith='LINE_')
                      .order_by('created_at').values_list('event_type', flat=True))
        self.assertEqual(events, [Event.LINE_ADDED, Event.LINE_WAITLISTED, Event.LINE_CONFIRMED, Event.LINE_RELEASED])

    def test_requested_lines_are_edited_and_removed_only_while_requested(self):
        line = self.line(self.application(), self.big_booth)
        member = self.as_user(self.member)
        url = f'/api/v1/application-lines/{line.pk}/'
        self.assertEqual(member.patch(url, {'quantity': 3}, format='json').json()['quantity'], 3)
        self.assertEqual(member.patch(url, {'quantity': 0}, format='json').status_code, 400)
        self.assertEqual(member.patch(url, {'unit_price_cents': 1}, format='json').status_code, 400)
        self.decide(line, 'waitlist')
        self.assertEqual(member.patch(url, {'quantity': 1}, format='json').status_code, 400)
        self.assertEqual(member.delete(url).status_code, 400)

        other = self.line(self.application(**self.vendor_payload(2)), self.big_booth)
        self.assertEqual(member.delete(f'/api/v1/application-lines/{other.pk}/').status_code, 204)
        self.assertFalse(ApplicationLine.objects.filter(pk=other.pk).exists())
        self.assertTrue(other.application.activity.filter(event_type=Event.LINE_REMOVED).exists())


class LineRequestTests(PipelineTestCase):
    def test_duplicate_open_variant_is_rejected(self):
        application = self.application()
        first = self.line(application)
        response = self.request_line(application, variant=self.booth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['line_id'], str(first.pk))
        self.assertEqual(self.request_line(application, variant=self.big_booth).status_code, 201)
        pipeline.decline_line(first, actor=self.admin)
        self.assertEqual(self.request_line(application, variant=self.booth).status_code, 201, 'closed lines do not block')

    def test_add_on_needs_a_booth_line_of_the_same_weekend(self):
        application = self.application()
        booth = self.line(application)
        self.assertEqual(self.request_line(application, add_on=self.tent).status_code, 400)
        response = self.request_line(application, add_on=self.other_tent, parent_line=str(booth.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn('same weekend', response.json()['parent_line'])
        other_application = self.application(**self.vendor_payload(2))
        other_booth = self.line(other_application)
        response = self.request_line(application, add_on=self.tent, parent_line=str(other_booth.pk))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.request_line(application, variant=self.big_booth, parent_line=str(booth.pk)).status_code, 400)
        tent = self.line(application, self.tent, parent=booth)
        self.assertEqual(tent.parent_line, booth)

    def test_exactly_one_product(self):
        application = self.application()
        self.assertEqual(self.request_line(application, quantity=1).status_code, 400)
        both = self.request_line(application, variant=self.booth, add_on=self.table)
        self.assertEqual(both.status_code, 400)

    def test_closed_weekend_and_stage_rules(self):
        application = self.application()
        MarketWeekend.objects.filter(pk=self.weekend.pk).update(status=MarketWeekend.Status.CLOSED)
        self.assertEqual(self.request_line(application, variant=self.booth).status_code, 400)
        MarketWeekend.objects.filter(pk=self.weekend.pk).update(status=MarketWeekend.Status.DRAFT)
        self.assertEqual(self.request_line(application, variant=self.booth).status_code, 201)

        for index, stage in enumerate([Stage.AWAITING_PAYMENT, Stage.DECLINED, Stage.WITHDRAWN]):
            blocked = self.at_stage(stage, **self.vendor_payload(10 + index))
            self.assertEqual(self.request_line(blocked, variant=self.booth).status_code, 400, stage)
        for index, stage in enumerate([Stage.IN_REVIEW, Stage.WON]):
            allowed = self.at_stage(stage, **self.vendor_payload(20 + index))
            self.assertEqual(self.request_line(allowed, variant=self.booth).status_code, 201, stage)

    def test_price_is_a_snapshot(self):
        line = self.line(self.application(), self.booth, quantity=2)
        self.as_user(self.admin).patch(
            f'/api/v1/market-weekends/{self.weekend.pk}/variants/{self.booth.pk}/', {'price_cents': 99900}, format='json',
        )
        self.booth.refresh_from_db()
        self.assertEqual(self.booth.price_cents, 99900)
        line.refresh_from_db()
        self.assertEqual((line.unit_price_cents, line.total_cents), (15000, 30000))
        self.assertEqual(self.line(self.application(**self.vendor_payload(2)), self.booth).unit_price_cents, 99900)

    def test_products_with_lines_cannot_be_deleted(self):
        application = self.application()
        booth = self.line(application)
        self.line(application, self.table, parent=booth)
        admin = self.as_user(self.admin)
        base = f'/api/v1/market-weekends/{self.weekend.pk}/'
        self.assertEqual(admin.delete(f'{base}variants/{self.booth.pk}/').status_code, 400)
        self.assertEqual(admin.delete(f'{base}add-ons/{self.table.pk}/').status_code, 400)
        self.assertEqual(admin.delete(base).status_code, 400)
        self.assertEqual(admin.delete(f'{base}add-ons/{self.tent.pk}/').status_code, 204)


class CapacityTests(PipelineTestCase):
    def test_refuses_at_the_limit_and_succeeds_after_a_release(self):
        first = self.line(self.application(), self.booth, quantity=2)
        second = self.line(self.application(**self.vendor_payload(2)), self.booth)
        self.assertEqual(self.decide(first, 'confirm').status_code, 200)

        response = self.decide(second, 'confirm')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Only 0 of 8ft x 8ft left', response.json()['quantity'])
        self.assertEqual(response.json()['remaining'], '0')

        self.assertEqual(self.decide(second, 'waitlist').status_code, 200)
        usage = self.usage(self.booth)
        self.assertEqual({k: usage[k] for k in ('capacity', 'confirmed', 'remaining', 'waitlisted')},
                         {'capacity': 2, 'confirmed': 2, 'remaining': 0, 'waitlisted': 1})
        self.assertEqual(usage['promotion_suggestions'], [], 'no room, no suggestions')

        self.assertEqual(self.decide(first, 'release').status_code, 400, 'release needs a reason')
        self.assertEqual(self.decide(first, 'release', reason='Moved to November').status_code, 200)
        usage = self.usage(self.booth)
        self.assertEqual((usage['confirmed'], usage['remaining']), (0, 2))
        self.assertEqual([s['line_id'] for s in usage['promotion_suggestions']], [str(second.pk)])
        self.assertTrue(usage['promotion_suggestions'][0]['fits'])

        self.assertEqual(self.decide(second, 'confirm').status_code, 200)

    def test_quantity_larger_than_remaining_is_refused(self):
        self.decide(self.line(self.application(), self.booth), 'confirm')
        response = self.decide(self.line(self.application(**self.vendor_payload(2)), self.booth, quantity=2), 'confirm')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['remaining'], '1')

    def test_suggestions_are_oldest_first(self):
        holder = self.line(self.application(), self.booth, quantity=2)
        self.decide(holder, 'confirm')
        waiting = []
        for n in range(3):
            line = self.line(self.application(**self.vendor_payload(n + 2)), self.booth)
            self.decide(line, 'waitlist')
            waiting.append(line)
        ApplicationLine.objects.filter(pk=waiting[0].pk).update(waitlisted_at=timezone.now() + timedelta(hours=1))
        self.decide(holder, 'release', reason='Freed')
        ids = [s['line_id'] for s in self.usage(self.booth)['promotion_suggestions']]
        self.assertEqual(ids, [str(waiting[1].pk), str(waiting[2].pk), str(waiting[0].pk)])

    def test_unlimited_add_ons_and_capacity_floor(self):
        application = self.application()
        booth = self.line(application, self.big_booth)
        self.decide(booth, 'confirm')
        table = self.line(application, self.table, quantity=7, parent=booth)
        self.assertEqual(self.decide(table, 'confirm').status_code, 200)
        usage = self.usage(self.table)
        self.assertEqual((usage['capacity'], usage['confirmed'], usage['remaining']), (None, 7, None))

        url = f'/api/v1/market-weekends/{self.weekend.pk}/variants/{self.big_booth.pk}/'
        self.assertEqual(self.as_user(self.admin).patch(url, {'capacity': 0}, format='json').status_code, 400)
        self.assertEqual(self.as_user(self.admin).patch(url, {'capacity': 1}, format='json').status_code, 200)


class AddOnLineTests(PipelineTestCase):
    def test_add_on_needs_confirmed_parent(self):
        application = self.application()
        booth = self.line(application)
        tent = self.line(application, self.tent, parent=booth)
        response = self.decide(tent, 'confirm')
        self.assertEqual(response.status_code, 400)
        self.assertIn('booth line', response.json()['parent_line'])
        self.decide(booth, 'waitlist')
        self.assertEqual(self.decide(tent, 'confirm').status_code, 400)
        self.decide(booth, 'confirm')
        self.assertEqual(self.decide(tent, 'confirm').status_code, 200)

    def test_cancelling_the_parent_cascades(self):
        application = self.application()
        booth = self.line(application)
        tent = self.line(application, self.tent, parent=booth)
        table = self.line(application, self.table, parent=booth)
        self.decide(booth, 'confirm')
        self.decide(tent, 'confirm')
        self.decide(booth, 'release', reason='Vendor dropped out')
        tent.refresh_from_db()
        table.refresh_from_db()
        self.assertEqual((tent.status, table.status), (LineStatus.CANCELLED, LineStatus.CANCELLED))
        self.assertEqual(self.usage(self.tent)['remaining'], 1)
        events = set(application.activity.values_list('event_type', flat=True))
        self.assertTrue({Event.LINE_RELEASED, Event.LINE_CANCELLED} <= events)

    def test_declining_the_parent_cascades(self):
        application = self.application()
        booth = self.line(application)
        tent = self.line(application, self.tent, parent=booth)
        self.decide(booth, 'decline')
        tent.refresh_from_db()
        self.assertEqual(tent.status, LineStatus.CANCELLED)

    def test_removing_a_requested_booth_removes_requested_add_ons(self):
        application = self.application()
        booth = self.line(application)
        self.line(application, self.table, parent=booth)
        self.assertEqual(self.as_user(self.member).delete(f'/api/v1/application-lines/{booth.pk}/').status_code, 204)
        self.assertFalse(application.lines.exists())


class ReleaseRuleTests(PipelineTestCase):
    def test_release_requires_a_reason_and_is_blocked_after_first_payment(self):
        application = self.application()
        line = self.line(application)
        self.decide(line, 'confirm')
        self.assertEqual(self.decide(line, 'release', reason='   ').status_code, 400)
        Application.objects.filter(pk=application.pk).update(first_paid_at=timezone.now())
        response = self.decide(line, 'release', reason='Changed plans')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), ['Refund rules are not implemented yet'])
        line.refresh_from_db()
        self.assertEqual(line.status, LineStatus.CONFIRMED)


class ApplicationDeclineTests(PipelineTestCase):
    def test_decline_frees_held_capacity_and_cancels_open_tasks(self):
        application = self.at_stage(Stage.IN_REVIEW)
        booth = self.line(application, self.booth, quantity=2)
        tent = self.line(application, self.tent, parent=booth)
        waiting = self.line(application, self.big_booth)
        self.decide(booth, 'confirm')
        self.decide(tent, 'confirm')
        self.decide(waiting, 'waitlist')
        self.assertEqual(self.usage(self.booth)['remaining'], 0)

        self.as_user(self.admin).post(self.stage_url(application), {'stage': 'DECLINED', 'reason': 'Not a fit'}, format='json')
        statuses = set(application.lines.values_list('status', flat=True))
        self.assertEqual(statuses, {LineStatus.CANCELLED})
        self.assertEqual((self.usage(self.booth)['remaining'], self.usage(self.tent)['remaining']), (2, 1))
        self.assertFalse(application.tasks.filter(status__in=Task.OPEN_STATUSES).exists())
        booth.refresh_from_db()
        self.assertEqual(booth.status_reason, 'Application declined: Not a fit')

    def test_reopen_does_not_touch_lines(self):
        application = self.at_stage(Stage.IN_REVIEW)
        line = self.line(application)
        self.as_user(self.admin).post(self.stage_url(application), {'stage': 'WITHDRAWN'}, format='json')
        self.as_user(self.admin).post(self.stage_url(application), {'stage': 'IN_REVIEW'}, format='json')
        line.refresh_from_db()
        self.assertEqual(line.status, LineStatus.CANCELLED)


class TotalsTests(PipelineTestCase):
    def test_discount_maths_and_rounding(self):
        percent, fixed, none = (Application.DiscountType.PERCENT, Application.DiscountType.FIXED,
                                Application.DiscountType.NONE)
        cases = [
            (percent, '10', 15005, 1501),       # 1500.5 rounds half-up
            (percent, '12.5', 333, 42),         # 41.625
            (percent, '33.33', 100, 33),        # 33.33
            (percent, '50', 1, 1),              # 0.5 rounds up
            (percent, '0.01', 4999, 0),         # 0.4999
            (percent, '100', 15000, 15000),
            (fixed, '2500', 15000, 2500),
            (fixed, '50000', 30000, 30000),     # capped at the subtotal
            (fixed, '500', 0, 0),
            (none, '0', 15000, 0),
        ]
        for discount_type, value, subtotal, expected in cases:
            with self.subTest(discount_type=discount_type, value=value, subtotal=subtotal):
                self.assertEqual(pipeline.discount_cents(discount_type, value, subtotal), expected)

    def test_totals_on_the_application(self):
        application = self.application()
        booth = self.line(application, self.booth, quantity=2)        # 30000, confirmed
        tent = self.line(application, self.tent, parent=booth)        # 4000, requested
        waiting = self.line(application, self.big_booth)              # 22500, waitlisted
        declined = self.line(application, self.other_booth)           # 15000, declined
        self.decide(booth, 'confirm')
        self.decide(waiting, 'waitlist')
        self.decide(declined, 'decline')
        admin = self.as_user(self.admin)
        url = f'/api/v1/applications/{application.pk}/'
        body = admin.patch(url, {'discount_type': 'PERCENT', 'discount_value': '12.5',
                                 'discount_label': 'New Vendor Trial'}, format='json').json()
        self.assertEqual(
            {k: body[k] for k in ('currency', 'requested_total_cents', 'confirmed_subtotal_cents',
                                  'discount_cents', 'confirmed_total_cents')},
            {'currency': 'USD', 'requested_total_cents': 56500, 'confirmed_subtotal_cents': 30000,
             'discount_cents': 3750, 'confirmed_total_cents': 26250},
        )
        self.assertEqual(len(body['lines']), 4)
        self.assertEqual(tent.parent_line_id, booth.pk)
        body = admin.patch(url, {'discount_type': 'FIXED', 'discount_value': '40000'}, format='json').json()
        self.assertEqual((body['discount_cents'], body['confirmed_total_cents']), (30000, 0))


class LinePermissionTests(PipelineTestCase):
    def test_members_request_but_do_not_decide(self):
        application = self.application()
        line = self.line(application)
        self.assertEqual(line.created_by, self.member)
        for action in ('confirm', 'waitlist', 'decline'):
            self.assertEqual(self.decide(line, action, user=self.member, reason='x').status_code, 403, action)
        self.decide(line, 'confirm')
        self.assertEqual(self.decide(line, 'release', user=self.member, reason='x').status_code, 403)
        line.refresh_from_db()
        self.assertEqual(line.status, LineStatus.CONFIRMED)
        with self.assertRaises(PermissionDenied):
            pipeline.release_line(line, actor=self.member, reason='x')
        self.assertEqual(self.as_user(self.other).get(f'/api/v1/applications/{application.pk}/lines/').status_code, 200)


class EntryPathTests(PipelineTestCase):
    def assert_new_with_call(self, application):
        self.assertEqual(application.stage, Stage.NEW)
        tasks = list(application.tasks.all())
        self.assertEqual([(t.task_type, t.status) for t in tasks], [(Task.TaskType.DISCOVERY_CALL, Task.Status.TODO)])

    def test_add_vendor_via_applications_creates_new_and_a_call(self):
        response = self.as_user(self.member).post('/api/v1/applications/', {
            'vendor_data': {'brand_name': 'Juniper', 'email': 'ana@juniper.example.com'},
            'requested_lines': [{'variant': str(self.booth.pk), 'quantity': 1,
                                 'add_ons': [{'add_on': str(self.tent.pk)}]}],
            'notes': 'Met at the October market.',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertFalse(body['bundled'])
        application = Application.objects.get(pk=body['id'])
        self.assert_new_with_call(application)
        lines = list(application.lines.order_by('created_at'))
        self.assertEqual([(line.variant, line.add_on, line.parent_line) for line in lines],
                         [(self.booth, None, None), (None, self.tent, lines[0])])
        self.assertEqual(application.internal_notes.get().body, 'Met at the October market.')

    def test_register_lead_goes_through_submission(self):
        vendor = Vendor.objects.get(pk=self.lead()['id'])
        self.assert_new_with_call(vendor.applications.get())
        self.assertEqual(vendor.intake_events.get().source_type, 'MANUAL_REGISTRATION')

    def test_csv_import_creates_new_applications_and_attaches_matched_rows(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        paid = self.at_stage(Stage.AWAITING_PAYMENT, vendor_data={'brand_name': 'Acme', 'email': 'cy@acme.example.com'})
        csv = 'Brand,Email,Notes\nFresh,fresh@example.com,\nAcme Again,CY@acme.example.com,Wants November too\n'
        upload = SimpleUploadedFile('rows.csv', csv.encode(), content_type='text/csv')
        summary = self.as_user(self.admin).post('/api/v1/imports/vendors/confirm/', {'file': upload}, format='multipart').json()
        self.assertEqual((summary['created'], summary['matched'], summary['applications_created']), (1, 1, 2))
        self.assert_new_with_call(Vendor.objects.get(brand_name='Fresh').applications.get())

        acme_applications = paid.vendor.applications.order_by('submitted_at')
        self.assertEqual([a.stage for a in acme_applications], [Stage.AWAITING_PAYMENT, Stage.NEW])
        self.assertEqual(acme_applications.last().internal_notes.get().body, 'Wants November too')

    def test_starts_in_review_when_already_reached(self):
        application = self.application()
        call = application.tasks.get(task_type=Task.TaskType.DISCOVERY_CALL)
        self.as_user(self.member).post(f'/api/v1/tasks/{call.pk}/log-call/', {'outcome': 'Interested'}, format='json')
        pipeline.move_stage(application, Stage.WON, actor=self.admin)

        again = self.application()
        self.assertNotEqual(again.pk, application.pk)
        self.assertEqual(again.stage, Stage.IN_REVIEW)
        types = set(again.tasks.filter(status__in=Task.OPEN_STATUSES).values_list('task_type', flat=True))
        self.assertEqual(types, {Task.TaskType.APPLICATION_REVIEW})
        self.assertFalse(Task.objects.filter(vendor=again.vendor, task_type=Task.TaskType.DISCOVERY_CALL,
                                             status__in=Task.OPEN_STATUSES).exists())


class CallAutomationTests(PipelineTestCase):
    def log(self, task, outcome='No Answer'):
        return self.as_user(self.member).post(f'/api/v1/tasks/{task.pk}/log-call/', {'outcome': outcome}, format='json')

    def test_first_call_attempt_moves_new_to_in_review(self):
        application = self.application()
        call = application.tasks.get(task_type=Task.TaskType.DISCOVERY_CALL)
        self.assertEqual(self.log(call).status_code, 200)
        application.refresh_from_db()
        self.assertEqual(application.stage, Stage.IN_REVIEW)
        self.assertTrue(application.tasks.filter(task_type=Task.TaskType.APPLICATION_REVIEW).exists())
        event = application.activity.get(event_type=Event.APPLICATION_STAGE_CHANGED)
        self.assertEqual((event.metadata['system'], event.actor), (True, self.member))

    def test_five_no_answers_leave_the_stage_and_flag_call_exhausted(self):
        application = self.application()
        call = application.tasks.get(task_type=Task.TaskType.DISCOVERY_CALL)
        url = f'/api/v1/applications/{application.pk}/'
        for _ in range(4):
            self.log(call)
        self.assertFalse(self.as_user(self.member).get(url).json()['call_exhausted'])
        self.assertEqual(self.log(call).json()['status'], 'COMPLETED')
        body = self.as_user(self.member).get(url).json()
        self.assertEqual((body['stage'], body['call_exhausted']), (Stage.IN_REVIEW, True))


class BundlingTests(PipelineTestCase):
    def submit(self, **payload):
        payload.setdefault('vendor_data', {'brand_name': 'Juniper', 'email': 'ana@juniper.example.com'})
        return self.as_user(self.member).post('/api/v1/applications/', payload, format='json')

    def test_second_submission_is_bundled_into_the_open_application(self):
        first = self.submit(requested_lines=[{'variant': str(self.booth.pk)}]).json()
        tasks_before = Task.objects.count()
        response = self.submit(requested_lines=[{'variant': str(self.other_booth.pk), 'quantity': 2}], notes='Also the 17th')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()['id'], response.json()['bundled']), (first['id'], True))
        application = Application.objects.get(pk=first['id'])
        self.assertEqual(application.lines.count(), 2)
        self.assertEqual(Task.objects.count(), tasks_before, 'no new tasks')
        self.assertEqual(application.internal_notes.get().body, 'Also the 17th')
        self.assertTrue(application.activity.filter(event_type=Event.APPLICATION_BUNDLED).exists())

    def test_bundles_into_in_review_but_not_after_awaiting_payment(self):
        application = self.at_stage(Stage.IN_REVIEW)
        self.assertEqual(self.submit().json()['id'], str(application.pk))
        pipeline.move_stage(application, Stage.AWAITING_PAYMENT, actor=self.admin)
        response = self.submit()
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()['id'], str(application.pk))

    def test_bundled_duplicate_line_is_rejected(self):
        self.submit(requested_lines=[{'variant': str(self.booth.pk)}])
        response = self.submit(requested_lines=[{'variant': str(self.booth.pk)}])
        self.assertEqual(response.status_code, 400)


class BackfillTests(PipelineTestCase):
    def test_backfill_is_idempotent_and_supports_dry_run(self):
        from io import StringIO

        from django.core.management import call_command

        lead = Vendor.objects.create(brand_name='Old Lead', instagram='@oldlead', lead_source='Instagram')
        not_qualified = Vendor.objects.create(
            brand_name='Old No', instagram='@oldno', qualification_status=Vendor.Qualification.NOT_QUALIFIED,
        )
        call = Task.objects.create(vendor=lead, title='Call', task_type=Task.TaskType.DISCOVERY_CALL)
        already = self.application()

        out = StringIO()
        call_command('backfill_applications', '--dry-run', stdout=out)
        self.assertIn('Would create 1 application(s) at NEW and 1 at DECLINED', out.getvalue())
        self.assertFalse(lead.applications.exists())

        call_command('backfill_applications', stdout=StringIO())
        application = lead.applications.get()
        self.assertEqual((application.stage, application.source), (Stage.NEW, 'Instagram'))
        self.assertEqual(application.submitted_at, lead.created_at)
        call.refresh_from_db()
        self.assertEqual(call.application, application)
        declined = not_qualified.applications.get()
        self.assertEqual((declined.stage, declined.decision_reason), (Stage.DECLINED, 'Not qualified (migrated)'))
        self.assertEqual(already.vendor.applications.count(), 1)

        out = StringIO()
        call_command('backfill_applications', stdout=out)
        self.assertIn('Created 0 application(s) at NEW and 0 at DECLINED', out.getvalue())
