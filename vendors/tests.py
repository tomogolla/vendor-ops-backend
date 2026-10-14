from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from config.testing import ApiTestCase
from tasks.models import Task
from tasks.scheduling import discovery_call_due_at

from .models import ActivityEvent, Application, LeadIntakeEvent, Vendor
from .normalization import normalize_email, normalize_instagram, normalize_phone

NY = ZoneInfo('America/New_York')
Event = ActivityEvent.EventType


class NormalizationTests(SimpleTestCase):
    def test_normalizers(self):
        self.assertEqual(normalize_email('  Cy@ACME.com '), 'cy@acme.com')
        self.assertEqual(normalize_instagram(' @Wayne_Ent '), 'wayne_ent')
        self.assertEqual(normalize_instagram('https://www.instagram.com/wayne_ent/?hl=en'), 'wayne_ent')
        self.assertEqual(normalize_phone('+1 (555) 123-4567'), '5551234567')
        self.assertEqual(normalize_phone('555.123.4567'), '5551234567')


@override_settings(
    BUSINESS_HOURS={'timezone': 'America/New_York', 'days': [0, 1, 2, 3, 4], 'open': '09:00', 'close': '17:00'},
    DISCOVERY_CALL_DUE_BUSINESS_HOURS=2,
)
class BusinessHoursTests(SimpleTestCase):
    def due(self, *args):
        return discovery_call_due_at(datetime(*args, tzinfo=NY)).astimezone(NY).replace(tzinfo=None)

    def test_within_hours_adds_two_hours(self):
        self.assertEqual(self.due(2026, 10, 12, 10, 0), datetime(2026, 10, 12, 12, 0))  # Monday

    def test_spills_into_next_business_day(self):
        self.assertEqual(self.due(2026, 10, 12, 16, 0), datetime(2026, 10, 13, 10, 0))
        self.assertEqual(self.due(2026, 10, 16, 16, 30), datetime(2026, 10, 19, 10, 30))  # Friday -> Monday

    def test_outside_hours_uses_next_opening(self):
        self.assertEqual(self.due(2026, 10, 12, 7, 0), datetime(2026, 10, 12, 9, 0))
        self.assertEqual(self.due(2026, 10, 16, 18, 0), datetime(2026, 10, 19, 9, 0))
        self.assertEqual(self.due(2026, 10, 17, 12, 0), datetime(2026, 10, 19, 9, 0))  # Saturday


class ManualRegistrationTests(ApiTestCase):
    def test_creates_vendor_intake_activity_and_high_priority_call(self):
        data = self.lead(
            self.as_user(self.member), email='Lil@Ember.example.com ', phone='+1 555 210 3344', owner=str(self.member.pk),
        )
        vendor = Vendor.objects.get(pk=data['id'])
        # Changed in v1.3: registration opens an application at NEW, so the vendor reads as applied.
        self.assertEqual(data['status'], 'applied')
        application = vendor.applications.get()
        self.assertEqual(application.stage, Application.Stage.NEW)
        self.assertEqual((vendor.email_normalized, vendor.phone_normalized, vendor.instagram_normalized),
                         ('lil@ember.example.com', '5552103344', 'mossandthread'))
        self.assertEqual(vendor.created_by, self.member)

        intake = vendor.intake_events.get()
        self.assertEqual(intake.source_type, LeadIntakeEvent.SourceType.MANUAL_REGISTRATION)
        self.assertEqual(intake.captured_by, self.member)

        task = vendor.tasks.get()
        self.assertEqual((task.task_type, task.priority, task.status, task.owner),
                         (Task.TaskType.DISCOVERY_CALL, Task.Priority.HIGH, Task.Status.TODO, self.member))
        self.assertIsNotNone(task.due_at)
        self.assertEqual(task.created_by, self.member)
        self.assertEqual(task.application, application)

        events = list(vendor.activity.order_by('created_at').values_list('event_type', flat=True))
        self.assertEqual(events, [Event.LEAD_REGISTERED, Event.APPLICATION_SUBMITTED, Event.TASK_CREATED])
        self.assertEqual(data['call']['open_task_id'], str(task.pk))
        self.assertTrue(data['call']['never_called'])

    def test_unassigned_lead_goes_to_team_queue(self):
        data = self.lead()
        self.assertIsNone(Task.objects.get(vendor_id=data['id']).owner)

    def test_requires_brand_and_one_contact(self):
        client = self.as_user(self.member)
        self.assertEqual(client.post('/api/v1/vendors/', {'instagram': '@x'}, format='json').status_code, 400)
        response = client.post('/api/v1/vendors/', {'brand_name': 'No Contact'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('contact', response.json())


class DuplicateDetectionTests(ApiTestCase):
    def setUp(self):
        self.existing = self.lead(email='cy@acme.example.com', phone='(555) 123-4567', instagram='@acme', brand_name='Acme')

    def assert_conflict(self, matched_on, **fields):
        response = self.as_user(self.member).post('/api/v1/vendors/', {'brand_name': 'Other', **fields}, format='json')
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'possible_duplicate')
        self.assertEqual(body['matches'][0]['vendor']['id'], self.existing['id'])
        self.assertEqual(body['matches'][0]['matched_on'], matched_on)

    def test_matches_on_normalized_email_phone_or_instagram(self):
        self.assert_conflict(['email'], email='CY@ACME.example.com')
        self.assert_conflict(['phone'], phone='+1 555-123-4567')
        self.assert_conflict(['instagram'], instagram='ACME')

    def test_confirmed_duplicate_is_created_not_merged(self):
        response = self.as_user(self.member).post(
            '/api/v1/vendors/', {'brand_name': 'Acme 2', 'email': 'cy@acme.example.com', 'confirm_duplicate': True},
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Vendor.objects.count(), 2)

    def test_same_business_name_alone_never_blocks_or_merges(self):
        response = self.as_user(self.member).post('/api/v1/vendors/', {'brand_name': 'acme', 'instagram': '@acme_two'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Vendor.objects.filter(brand_name__iexact='acme').count(), 2)

    def test_duplicates_endpoint_reports_name_hints_separately(self):
        body = self.as_user(self.member).get('/api/v1/vendors/duplicates/', {'brand_name': 'ACME', 'email': 'x@y.com'}).json()
        self.assertEqual(body['matches'], [])
        self.assertEqual(body['name_matches'][0]['id'], self.existing['id'])


class ApplicationSubmissionTests(ApiTestCase):
    def submit(self, client=None, **payload):
        response = (client or self.as_user(self.member)).post('/api/v1/applications/', payload, format='json')
        return response

    def test_new_contact_creates_vendor_application_call_and_review(self):
        response = self.submit(
            vendor_data={'brand_name': 'Juniper', 'email': 'ana@juniper.example.com', 'first_name': 'Ana'},
            booth_requirements={'size': '8ft x 8ft', 'needs_tent': True},
            preferred_market_dates=['October 10 & 11'],
        )
        self.assertEqual(response.status_code, 201, response.content)
        application = Application.objects.get(pk=response.json()['id'])
        vendor = application.vendor
        self.assertEqual(vendor.display_status, 'applied')
        self.assertEqual(vendor.intake_events.get().source_type, LeadIntakeEvent.SourceType.APPLICATION_SUBMISSION)
        # Changed in v1.3: the review task is created when the application enters IN_REVIEW, not at submission.
        self.assertEqual(application.stage, Application.Stage.NEW)
        types = sorted(vendor.tasks.values_list('task_type', flat=True))
        self.assertEqual(types, [Task.TaskType.DISCOVERY_CALL])
        self.assertTrue(vendor.activity.filter(event_type=Event.APPLICATION_SUBMITTED, application=application).exists())

    def test_matches_existing_vendor_by_contact_details(self):
        vendor = self.lead()
        response = self.submit(vendor_data={'brand_name': 'Moss and Thread Studio', 'instagram': 'MossAndThread'})
        self.assertEqual(response.json()['vendor'], vendor['id'])
        self.assertEqual(Vendor.objects.count(), 1)

    def test_ambiguous_match_is_rejected(self):
        self.lead(brand_name='A', instagram='@a', email='a@example.com')
        self.lead(brand_name='B', instagram='@b')
        response = self.submit(vendor_data={'brand_name': 'AB', 'email': 'a@example.com', 'instagram': '@b'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'ambiguous_vendor')

    def test_no_duplicate_open_call_tasks(self):
        vendor = self.lead()
        self.submit(vendor=vendor['id'])
        self.submit(vendor=vendor['id'])
        calls = Task.objects.filter(vendor_id=vendor['id'], task_type=Task.TaskType.DISCOVERY_CALL)
        self.assertEqual(calls.count(), 1)
        # Changed in v1.3: repeat submissions are bundled into the open NEW application, and review
        # tasks only exist from IN_REVIEW.
        self.assertEqual(Application.objects.filter(vendor_id=vendor['id']).count(), 1)
        self.assertEqual(Task.objects.filter(vendor_id=vendor['id'], task_type=Task.TaskType.APPLICATION_REVIEW).count(), 0)

    def test_reached_vendor_gets_review_but_no_new_call(self):
        vendor = self.lead()
        call = Task.objects.get(vendor_id=vendor['id'])
        self.as_user(self.member).post(f'/api/v1/tasks/{call.pk}/log-call/', {'outcome': 'Interested'}, format='json')
        self.submit(vendor=vendor['id'])
        calls = Task.objects.filter(vendor_id=vendor['id'], task_type=Task.TaskType.DISCOVERY_CALL)
        self.assertEqual([c.status for c in calls], [Task.Status.COMPLETED])

    def test_members_cannot_set_status_on_submission_or_patch(self):
        vendor = self.lead()
        self.assertEqual(self.submit(vendor=vendor['id'], status='APPROVED').status_code, 403)
        app_id = self.submit(vendor=vendor['id']).json()['id']
        response = self.as_user(self.member).patch(f'/api/v1/applications/{app_id}/', {'status': 'IN_REVIEW'}, format='json')
        self.assertEqual(response.status_code, 403)


class StatusTests(ApiTestCase):
    # Changed in v1.3: 'approved' is no longer a status transition (lines are confirmed and the
    # application billed instead), so the legacy endpoint answers 400. Suspend and reinstate still work.
    def test_admin_decision_completes_review_and_updates_vendor(self):
        from .pipeline import move_stage

        vendor = self.lead()
        admin = self.as_user(self.admin)
        url = f"/api/v1/vendors/{vendor['id']}/status/"
        self.assertEqual(vendor['status'], 'applied')  # v1.3: registration already submits an application
        self.assertEqual(admin.post(url, {'status': 'in_review'}).json()['status'], 'in_review')
        response = admin.post(url, {'status': 'approved', 'note': 'Great fit'})
        self.assertEqual((response.status_code, response.json()['code']), (400, 'use_line_decisions'))

        application = Application.objects.get(vendor_id=vendor['id'])
        move_stage(application, Application.Stage.AWAITING_PAYMENT, actor=self.admin, reason='Great fit')
        self.assertEqual(admin.get(f"/api/v1/vendors/{vendor['id']}/").json()['status'], 'approved')
        self.assertEqual((application.decision, application.decision_reason), ('APPROVED', 'Great fit'))
        self.assertEqual(admin.post(url, {'status': 'suspended'}).json()['status'], 'suspended')
        self.assertEqual(admin.post(url, {'status': 'approved'}).json()['status'], 'approved')

    def test_invalid_transition_is_rejected(self):
        vendor = self.lead()
        response = self.as_user(self.admin).post(f"/api/v1/vendors/{vendor['id']}/status/", {'status': 'approved'})
        self.assertEqual(response.status_code, 400)

    def test_declined_lead_can_be_reopened(self):
        # Changed in v1.3: only pre-v1.3 data has vendors in the 'lead' status; build one directly.
        vendor = {'id': str(Vendor.objects.create(brand_name='Legacy Lead', instagram='@legacylead').pk)}
        admin = self.as_user(self.admin)
        url = f"/api/v1/vendors/{vendor['id']}/status/"
        self.assertEqual(admin.post(url, {'status': 'declined'}).json()['status'], 'declined')
        self.assertEqual(admin.post(url, {'status': 'in_review'}).json()['status'], 'in_review')
        events = set(ActivityEvent.objects.filter(vendor_id=vendor['id']).values_list('event_type', flat=True))
        # Changed in v1.3: stage changes are logged as APPLICATION_STAGE_CHANGED.
        self.assertTrue({Event.QUALIFICATION_CHANGED, Event.APPLICATION_SUBMITTED, Event.APPLICATION_STAGE_CHANGED} <= events)


class FilterTests(ApiTestCase):
    def test_status_and_call_filters(self):
        lead = self.lead()
        called = self.lead(brand_name='Called', instagram='@called')
        call = Task.objects.get(vendor_id=called['id'])
        self.as_user(self.member).post(f'/api/v1/tasks/{call.pk}/log-call/', {'outcome': 'No Answer'}, format='json')

        client = self.as_user(self.member)
        ids = lambda params: {v['id'] for v in client.get('/api/v1/vendors/', params).json()['results']}  # noqa: E731
        # Changed in v1.3: registered leads are applicants, and a logged call moves them into review.
        self.assertEqual(ids({'status': 'lead'}), set())
        self.assertEqual(ids({'status': 'applied'}), {lead['id']})
        self.assertEqual(ids({'status': 'in_review'}), {called['id']})
        self.assertEqual(ids({'status': 'approved'}), set())
        self.assertEqual(ids({'call': 'due'}), {lead['id'], called['id']})
        self.assertEqual(ids({'call': 'none'}), {lead['id']})
        self.assertEqual(ids({'lead_source': 'nonexistent'}), set())


class ActivityImmutabilityTests(ApiTestCase):
    def test_activity_is_read_only_via_api_and_model(self):
        vendor = self.lead()
        client = self.as_user(self.admin)
        url = f"/api/v1/vendors/{vendor['id']}/activity/"
        events = client.get(url).json()['results']
        self.assertEqual(len(events), 3)  # v1.3: registered, application submitted, task created
        for method in (client.post, client.put, client.patch, client.delete):
            self.assertEqual(method(url, {}, format='json').status_code, 405)

        event = ActivityEvent.objects.first()
        event.description = 'tampered'
        with self.assertRaises(TypeError):
            event.save()
        with self.assertRaises(TypeError):
            event.delete()
        with self.assertRaises(TypeError):
            ActivityEvent.objects.update(description='tampered')
        with self.assertRaises(TypeError):
            ActivityEvent.objects.all().delete()

    def test_notes_create_activity(self):
        vendor = self.lead()
        response = self.as_user(self.member).post(
            f"/api/v1/vendors/{vendor['id']}/notes/", {'body': 'Prefers email', 'note_type': 'DISCOVERY_CALL'}, format='json'
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ActivityEvent.objects.filter(vendor_id=vendor['id'], event_type=Event.NOTE_ADDED).exists())
