from config.testing import ApiTestCase
from vendors.models import ActivityEvent

from .models import Task

Event = ActivityEvent.EventType


class TaskVisibilityTests(ApiTestCase):
    def setUp(self):
        self.mine = Task.objects.get(vendor_id=self.lead(owner=str(self.member.pk))['id'])
        self.queue = Task.objects.get(vendor_id=self.lead(brand_name='Queue', instagram='@queue')['id'])
        other_vendor = self.lead(self.as_user(self.other), brand_name='Theirs', instagram='@theirs', owner=str(self.other.pk))
        self.theirs = Task.objects.get(vendor_id=other_vendor['id'])

    def ids(self, user, params=None):
        return {t['id'] for t in self.as_user(user).get('/api/v1/tasks/', params or {}).json()['results']}

    def test_member_sees_own_tasks_and_team_queue_only(self):
        self.assertEqual(self.ids(self.member), {str(self.mine.pk), str(self.queue.pk)})
        self.assertEqual(self.ids(self.admin), {str(self.mine.pk), str(self.queue.pk), str(self.theirs.pk)})

    def test_owner_and_type_filters(self):
        self.assertEqual(self.ids(self.member, {'owner': 'me'}), {str(self.mine.pk)})
        self.assertEqual(self.ids(self.admin, {'owner': 'none'}), {str(self.queue.pk)})
        self.assertEqual(self.ids(self.admin, {'task_type': 'APPLICATION_REVIEW'}), set())
        self.assertEqual(len(self.ids(self.admin, {'task_type': 'DISCOVERY_CALL', 'is_open': 'true'})), 3)
        self.assertEqual(self.ids(self.admin, {'status': 'COMPLETED'}), set())

    def test_member_cannot_touch_someone_elses_task(self):
        client = self.as_user(self.member)
        self.assertEqual(client.patch(f'/api/v1/tasks/{self.theirs.pk}/', {'notes': 'x'}, format='json').status_code, 404)
        self.assertEqual(client.post(f'/api/v1/tasks/{self.theirs.pk}/log-call/', {'outcome': 'Interested'}).status_code, 404)

    def test_member_can_claim_but_not_reassign(self):
        client = self.as_user(self.member)
        url = f'/api/v1/tasks/{self.queue.pk}/'
        self.assertEqual(client.patch(url, {'owner': str(self.other.pk)}, format='json').status_code, 400)
        self.assertEqual(client.patch(url, {'owner': str(self.member.pk)}, format='json').status_code, 200)
        self.assertEqual(client.patch(f'/api/v1/tasks/{self.mine.pk}/', {'owner': None}, format='json').status_code, 400)

    def test_completing_sets_completed_at(self):
        response = self.as_user(self.member).patch(f'/api/v1/tasks/{self.mine.pk}/', {'status': 'COMPLETED'}, format='json')
        self.assertIsNotNone(response.json()['completed_at'])

    def test_cannot_create_second_open_discovery_call(self):
        response = self.as_user(self.admin).post(
            '/api/v1/tasks/', {'vendor': str(self.mine.vendor_id), 'title': 'Again', 'task_type': 'DISCOVERY_CALL'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_member_can_create_follow_up_for_self(self):
        client = self.as_user(self.member)
        payload = {'vendor': str(self.mine.vendor_id), 'title': 'Send pricing', 'task_type': 'FOLLOW_UP'}
        self.assertEqual(client.post('/api/v1/tasks/', {**payload, 'owner': str(self.other.pk)}, format='json').status_code, 400)
        response = client.post('/api/v1/tasks/', {**payload, 'owner': str(self.member.pk)}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ActivityEvent.objects.filter(task_id=response.json()['id'], event_type=Event.TASK_CREATED).exists())


class LogCallTests(ApiTestCase):
    def setUp(self):
        self.task = Task.objects.get(vendor_id=self.lead()['id'])
        self.client = self.as_user(self.member)

    def log(self, **payload):
        return self.client.post(f'/api/v1/tasks/{self.task.pk}/log-call/', payload, format='json')

    def test_no_answer_keeps_task_open_and_claims_it(self):
        body = self.log(outcome='No Answer').json()
        self.assertEqual((body['status'], body['attempt_count'], body['owner']), ('NO_ANSWER', 1, str(self.member.pk)))
        self.assertTrue(ActivityEvent.objects.filter(task=self.task, event_type=Event.CALL_ATTEMPTED).exists())

    def test_interested_completes_and_stores_call_details(self):
        details = {'interest_level': 4, 'challenges': ' Parking ', 'vendor_reaction': 'Keen'}
        body = self.log(outcome='Interested', notes='Great call', call_details=details).json()
        self.assertEqual(body['status'], 'COMPLETED')
        self.assertEqual(body['call_details']['challenges'], 'Parking')
        event = ActivityEvent.objects.get(task=self.task, event_type=Event.CALL_COMPLETED)
        self.assertEqual(event.metadata['call_details']['interest_level'], 4)
        self.assertEqual(self.log(outcome='No Answer').status_code, 400)

    def test_callback_requires_time_and_reschedules(self):
        self.assertEqual(self.log(outcome='Callback Requested').status_code, 400)
        body = self.log(outcome='Callback Requested', callback_at='2030-01-02T15:00:00Z').json()
        self.assertEqual(body['status'], 'RESCHEDULED')
        self.assertEqual(body['due_at'], body['callback_at'])

    def test_five_no_answers_close_the_task(self):
        for _ in range(5):
            body = self.log(outcome='No Answer').json()
        self.assertEqual((body['status'], body['attempt_count']), ('COMPLETED', 5))

    def test_rejects_bad_call_details(self):
        self.assertEqual(self.log(outcome='Interested', call_details={'interest_level': 9}).status_code, 400)
        self.assertEqual(self.log(outcome='Interested', call_details={'unknown': 'x'}).status_code, 400)
