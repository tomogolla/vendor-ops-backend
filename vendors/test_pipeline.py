"""v1.3 sales pipeline: stages, lines, capacity, entry paths, billing stubs."""

import importlib
from datetime import timedelta

from django.apps import apps as global_apps
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from config.testing import ApiTestCase
from tasks.models import Task

from . import pipeline
from .models import ActivityEvent, Application, Vendor

Event = ActivityEvent.EventType
Stage = Application.Stage


class PipelineTestCase(ApiTestCase):
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
