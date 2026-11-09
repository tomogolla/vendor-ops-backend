from django.test import override_settings
from rest_framework.test import APIClient

from communications.models import EmailMessage, EmailTemplate
from config.testing import ApiTestCase
from public_api.models import GoogleSheetsSubmission
from tasks.models import Task
from vendors.models import Application, LeadIntakeEvent, Vendor

URL = '/api/webhooks/vendor-interest/'
SECRET = 'test-google-sheets-secret'


def vendor_payload(**overrides):
    data = {
        'submission_id': 'spreadsheet:Vendor Interest Form:42',
        'sheet_type': 'vendor_interest_form',
        'sheet_name': 'Vendor Interest Form',
        'row_number': 14,
        'first_name': 'Ana',
        'business_name': 'Juniper Goods',
        'phone_number': '(555) 123-4567',
        'instagram': '@juniper.goods',
        'email': 'Ana@Example.com',
        'form_number': 'V-42',
        'date_applied': '2026-11-09',
        'raw_fields': {'First name': 'Ana', 'Unmapped question': 'Extra answer'},
    }
    data.update(overrides)
    return data


@override_settings(VENDOR_OPS_WEBHOOK_SECRET=SECRET)
class GoogleSheetsWebhookTests(ApiTestCase):
    def setUp(self):
        self.client = APIClient(enforce_csrf_checks=True)

    def post(self, data, secret=SECRET):
        return self.client.post(URL, data, format='json', HTTP_X_WEBHOOK_SECRET=secret)

    def test_requires_configured_matching_secret(self):
        self.assertEqual(self.post(vendor_payload(), secret='wrong').status_code, 401)
        with override_settings(VENDOR_OPS_WEBHOOK_SECRET=''):
            self.assertEqual(self.post(vendor_payload()).status_code, 401)
        self.assertFalse(Application.objects.exists())

    def test_vendor_form_enters_normal_pipeline_and_keeps_metadata(self):
        EmailTemplate.objects.filter(key='application_received').update(mode='draft')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(vendor_payload())
        self.assertEqual(response.status_code, 201, response.content)
        application = Application.objects.get()
        vendor = application.vendor
        self.assertEqual((vendor.brand_name, vendor.email, vendor.lead_source), ('Juniper Goods', 'ana@example.com', 'Vendor Interest Form'))
        self.assertEqual((application.source, application.stage), ('Vendor Interest Form', Application.Stage.NEW))
        self.assertTrue(Task.objects.filter(application=application, task_type=Task.TaskType.DISCOVERY_CALL).exists())
        self.assertEqual(EmailMessage.objects.filter(application=application, template_key='application_received').count(), 1)
        event = LeadIntakeEvent.objects.get()
        self.assertEqual((event.metadata['form_number'], event.metadata['date_applied']), ('V-42', '2026-11-09'))
        receipt = GoogleSheetsSubmission.objects.get()
        self.assertEqual((receipt.sheet_name, receipt.row_number), ('Vendor Interest Form', 14))
        self.assertEqual(receipt.raw_fields['Unmapped question'], 'Extra answer')

    def test_popup_form_maps_brand_and_is_idempotent(self):
        data = {
            'submission_id': 'spreadsheet:popup form:8', 'sheet_type': 'popup_form',
            'submission_date': '2026-11-09 10:15', 'first_name': 'Bea', 'email': 'bea@example.com',
            'brand_name': 'Sun House', 'phone_number': '555-444-3333',
        }
        first = self.post(data)
        second = self.post(data)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(second.json(), {'status': 'success', 'submission_id': data['submission_id']})
        self.assertEqual((Vendor.objects.count(), Application.objects.count(), GoogleSheetsSubmission.objects.count()), (1, 1, 1))
        self.assertEqual(LeadIntakeEvent.objects.get().metadata['submission_date'], '2026-11-09 10:15')

    def test_rejects_bad_shape_without_partial_records(self):
        response = self.post(vendor_payload(business_name='', email='', phone_number='', instagram=''))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Vendor.objects.exists())
        self.assertFalse(GoogleSheetsSubmission.objects.exists())
