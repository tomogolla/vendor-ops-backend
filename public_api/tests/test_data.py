"""Step 1: the Website form system user, requested weekends, campaign and attribution."""

from datetime import date

from django.contrib import admin
from django.test import RequestFactory

from accounts.admin import UserAdmin
from accounts.models import User
from accounts.system_users import website_form_user
from catalog.models import MarketWeekend
from config.testing import PASSWORD, ApiTestCase
from vendors.models import Application, LeadIntakeEvent
from vendors.services import Intake, submit_application

from ..attribution import intake_metadata


class SystemUserTests(ApiTestCase):
    def test_created_once_inactive_without_usable_password(self):
        user = website_form_user()
        self.assertEqual(website_form_user(), user)
        self.assertEqual((user.display_name, user.is_active, user.is_system), ('Website form', False, True))
        self.assertFalse(user.has_usable_password())

    def test_cannot_sign_in(self):
        user = website_form_user()
        response = self.client.post('/api/v1/auth/login/', {'username': user.username, 'password': ''}, format='json')
        self.assertEqual(response.status_code, 400)
        user.set_password(PASSWORD)  # even with a password, an inactive account cannot sign in
        user.save()
        response = self.client.post('/api/v1/auth/login/', {'username': user.username, 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_hidden_from_team_assignees_and_user_admin(self):
        user = website_form_user()
        team = self.as_user(self.member).get('/api/v1/team/').json()
        self.assertNotIn(str(user.pk), [person['id'] for person in team])
        users = self.as_user(self.admin).get('/api/v1/users/').json()
        self.assertNotIn(user.username, [row['username'] for row in users['results']])
        task = self.as_user(self.admin).post('/api/v1/tasks/', {'title': 'x', 'vendor': str(self.vendor().pk), 'owner': str(user.pk)}, format='json')
        self.assertEqual(task.status_code, 400)
        model_admin = UserAdmin(User, admin.site)
        request = RequestFactory().get('/admin/accounts/user/')
        request.user = self.admin
        changelist = model_admin.get_changelist_instance(request)
        self.assertNotIn(user, changelist.get_queryset(request))
        request = RequestFactory().get('/admin/accounts/user/', {'account': 'system'})
        request.user = self.admin
        self.assertIn(user, model_admin.get_changelist_instance(request).get_queryset(request))

    def vendor(self):
        from vendors.models import Vendor
        return Vendor.objects.create(brand_name='Fern', email='fern@example.com')


class RequestedWeekendsAndCampaignTests(ApiTestCase):
    def test_application_exposes_requested_weekends_and_campaign(self):
        weekend = MarketWeekend.objects.create(label='Oct 10 & 11', start_date=date(2026, 10, 10), end_date=date(2026, 10, 11), status='OPEN')
        application = submit_application(
            actor=website_form_user(), vendor_data={'brand_name': 'Fern', 'email': 'fern@example.com'}, source='Website form',
            intake=Intake(source_detail='Website form', metadata={'utm_campaign': 'fall-ads'}),
        )
        application.requested_weekends.add(weekend)
        other = submit_application(actor=self.member, vendor_data={'brand_name': 'Oak', 'email': 'oak@example.com'})
        client = self.as_user(self.member)
        detail = client.get(f'/api/v1/applications/{application.pk}/').json()
        self.assertEqual(detail['requested_weekends'], [{'id': str(weekend.pk), 'label': 'Oct 10 & 11'}])
        self.assertEqual((detail['campaign'], detail['source']), ('fall-ads', 'Website form'))
        listed = {row['id']: row for row in client.get('/api/v1/applications/').json()['results']}
        self.assertEqual(listed[str(application.pk)]['campaign'], 'fall-ads')
        self.assertEqual((listed[str(other.pk)]['campaign'], listed[str(other.pk)]['requested_weekends']), ('', []))
        filtered = client.get('/api/v1/applications/', {'campaign': 'FALL-ADS'}).json()['results']
        self.assertEqual([row['id'] for row in filtered], [str(application.pk)])
        summary = client.get('/api/v1/applications/summary/', {'campaign': 'fall-ads'}).json()
        self.assertEqual(sum(summary['stages'].values()), 1)

    def test_campaign_follows_the_latest_intake_event(self):
        application = submit_application(
            actor=self.member, vendor_data={'brand_name': 'Fern', 'email': 'fern@example.com'},
            intake=Intake(metadata={'utm_campaign': 'spring'}),
        )
        LeadIntakeEvent.objects.create(vendor=application.vendor, source_type='APPLICATION_SUBMISSION', metadata={'utm_campaign': 'fall'})
        self.assertEqual(self.as_user(self.member).get(f'/api/v1/applications/{application.pk}/').json()['campaign'], 'fall')
        self.assertEqual(Application.objects.count(), 1)


class AttributionTests(ApiTestCase):
    def test_known_fields_cleaned_and_truncated_without_ip(self):
        metadata = intake_metadata(
            attribution={
                'utm_campaign': 'fall\x00-ads\n' + 'x' * 400, 'utm_source': 'meta', 'gclid': 'g' * 400,
                'referrer': 'https://example.com/' + 'r' * 900, 'ip': '203.0.113.9', 'remote_addr': '203.0.113.9',
                'utm_medium': {'nested': True}, 'utm_term': True,
            },
            user_agent='Mozilla/5.0 ' + 'u' * 900, consent_version='v1 ' * 40,
        )
        self.assertTrue(metadata['utm_campaign'].startswith('fall-ads x'))
        self.assertEqual(len(metadata['utm_campaign']), 150)
        self.assertEqual((len(metadata['gclid']), len(metadata['referrer']), len(metadata['user_agent'])), (255, 500, 300))
        self.assertEqual(metadata['utm_source'], 'meta')
        self.assertNotIn('utm_medium', metadata)
        self.assertNotIn('utm_term', metadata)
        self.assertNotIn('203.0.113.9', str(metadata))
        self.assertEqual(len(metadata['consent']['text_version']), 50)
        self.assertIn('accepted_at', metadata['consent'])
