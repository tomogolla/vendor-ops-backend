from rest_framework.test import APIClient

from config.testing import PASSWORD, ApiTestCase


class AuthenticationTests(ApiTestCase):
    def test_health_is_public(self):
        self.assertEqual(APIClient().get('/api/v1/health/').json(), {'status': 'ok'})

    def test_unauthenticated_requests_get_401(self):
        client = APIClient()
        for url in ['/api/v1/auth/me/', '/api/v1/vendors/', '/api/v1/tasks/', '/api/v1/applications/']:
            self.assertEqual(client.get(url).status_code, 401, url)

    def test_login_me_logout(self):
        client = APIClient()
        response = client.post('/api/v1/auth/login/', {'username': 'member', 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['role'], 'TEAM_MEMBER')
        self.assertFalse(response.json()['is_administrator'])
        self.assertEqual(client.get('/api/v1/auth/me/').json()['username'], 'member')
        self.assertEqual(client.post('/api/v1/auth/logout/').status_code, 204)
        self.assertEqual(client.get('/api/v1/auth/me/').status_code, 401)

    def test_bad_credentials(self):
        response = APIClient().post('/api/v1/auth/login/', {'username': 'member', 'password': 'nope'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_inactive_user_cannot_log_in(self):
        self.other.is_active = False
        self.other.save()
        response = APIClient().post('/api/v1/auth/login/', {'username': 'other', 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_writes_require_csrf_token_for_session_users(self):
        client = APIClient(enforce_csrf_checks=True)
        token = client.get('/api/v1/auth/csrf/').json()['csrfToken']
        client.post('/api/v1/auth/login/', {'username': 'member', 'password': PASSWORD}, format='json', HTTP_X_CSRFTOKEN=token)
        payload = {'brand_name': 'X', 'instagram': '@x'}
        self.assertEqual(client.post('/api/v1/vendors/', payload, format='json').status_code, 403)
        token = client.cookies['csrftoken'].value
        self.assertEqual(client.post('/api/v1/vendors/', payload, format='json', HTTP_X_CSRFTOKEN=token).status_code, 201)


class RoleTests(ApiTestCase):
    def test_only_admins_manage_users(self):
        self.assertEqual(self.as_user(self.member).get('/api/v1/users/').status_code, 403)
        self.assertEqual(self.as_user(self.admin).get('/api/v1/users/').status_code, 200)
        created = self.as_user(self.admin).post(
            '/api/v1/users/', {'username': 'new', 'password': 'a-Long-password-9', 'role': 'TEAM_MEMBER'}, format='json'
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertNotIn('password', created.json())

    def test_team_list_is_available_to_members(self):
        names = {user['username'] for user in self.as_user(self.member).get('/api/v1/team/').json()}
        self.assertEqual(names, {'admin', 'member', 'other'})

    def test_member_cannot_change_status_beyond_application(self):
        vendor = self.lead()
        member = self.as_user(self.member)
        self.assertEqual(member.post(f"/api/v1/vendors/{vendor['id']}/status/", {'status': 'declined'}).status_code, 403)
        self.assertEqual(member.post(f"/api/v1/vendors/{vendor['id']}/status/", {'status': 'applied'}).status_code, 200)
        self.assertEqual(member.post(f"/api/v1/vendors/{vendor['id']}/status/", {'status': 'in_review'}).status_code, 403)
        self.assertEqual(
            self.as_user(self.admin).post(f"/api/v1/vendors/{vendor['id']}/status/", {'status': 'in_review'}).status_code, 200
        )

    def test_member_cannot_import_or_set_qualification(self):
        vendor = self.lead()
        member = self.as_user(self.member)
        self.assertEqual(member.post('/api/v1/imports/vendors/preview/').status_code, 403)
        self.assertEqual(
            member.post(f"/api/v1/vendors/{vendor['id']}/qualification/", {'qualification_status': 'QUALIFIED'}).status_code,
            403,
        )

    def test_member_cannot_assign_owner_or_deactivate(self):
        vendor = self.lead()
        member = self.as_user(self.member)
        url = f"/api/v1/vendors/{vendor['id']}/"
        self.assertEqual(member.patch(url, {'owner': str(self.other.pk)}, format='json').status_code, 400)
        self.assertEqual(member.patch(url, {'is_active': False}, format='json').status_code, 400)
        self.assertEqual(member.patch(url, {'notes': 'Met at market'}, format='json').status_code, 200)
        self.assertEqual(self.as_user(self.admin).patch(url, {'owner': str(self.other.pk)}, format='json').status_code, 200)
