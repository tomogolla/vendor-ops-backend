from rest_framework.test import APIClient, APITestCase

from accounts.models import User

PASSWORD = 'test-Password-123'


class ApiTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user('admin', password=PASSWORD, role=User.Role.ADMINISTRATOR)
        cls.member = User.objects.create_user('member', password=PASSWORD, role=User.Role.TEAM_MEMBER)
        cls.other = User.objects.create_user('other', password=PASSWORD, role=User.Role.TEAM_MEMBER)

    def as_user(self, user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    def lead(self, client=None, **fields):
        payload = {'brand_name': 'Moss & Thread', 'instagram': '@mossandthread', **fields}
        response = (client or self.as_user(self.member)).post('/api/v1/vendors/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()
