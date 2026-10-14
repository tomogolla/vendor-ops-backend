from django.core.files.uploadedfile import SimpleUploadedFile

from config.testing import ApiTestCase
from tasks.models import Task
from vendors.models import ActivityEvent, LeadIntakeEvent, Vendor

from .models import ImportBatch

CSV = '\n'.join([
    'Business Name,First Name,IG,EMAIL,Phone,Category,Tags,Contacted,Inactive,Do not contact,Favourite colour',
    'Juniper,Ana,@juniper,,,ceramics / glass,Returning;Handmade,,,,blue',     # 2 new
    'Juniper Again,,JUNIPER,,,,,,,,',                                         # 3 skipped (same IG as row 2)
    'Acme Copy,,,CY@ACME.example.com,,,,,,,',                                 # 4 matched (email)
    'acme,,@acme_new,,,,,,,,',                                                # 5 possible duplicate (name only)
    ',,@nobrand,,,,,,,,',                                                     # 6 invalid (no brand)
    'No Contact,,,,,,,,,,',                                                   # 7 invalid (no contact)
    'Bad Email,,,not-an-email,,,,,,,',                                        # 8 invalid
    'Called Before,,@calledbefore,,,Knitwear,,yes,,,',                        # 9 new, already contacted
    'Dormant,,@dormant,,,,,,yes,,',                                           # 10 new, inactive
    'Opted Out,,,,555-010-2000,,,,,x,',                                       # 11 new, excluded
    ',,,,,,,,,,',                                                             # 12 skipped (empty)
])


class CsvImportTests(ApiTestCase):
    def setUp(self):
        self.acme = self.lead(brand_name='Acme', email='cy@acme.example.com')
        self.client = self.as_user(self.admin)

    def upload(self, url, content=CSV, **extra):
        file = SimpleUploadedFile('leads.csv', content.encode(), content_type='text/csv')
        return self.client.post(url, {'file': file, **extra}, format='multipart')

    def test_preview_classifies_rows(self):
        body = self.upload('/api/v1/imports/vendors/preview/').json()
        by_line = {row['line']: row for row in body['rows']}
        self.assertEqual(body['ignored_headers'], ['Favourite colour'])
        self.assertEqual(body['counts'], {'new': 4, 'matched': 1, 'possible_duplicate': 1, 'invalid': 3, 'skipped': 2})
        self.assertEqual(by_line[2]['data']['category'], 'Ceramics / Glass')
        self.assertEqual(by_line[2]['data']['tags'], ['Returning', 'Handmade'])
        self.assertEqual(by_line[2]['data']['lead_source'], 'CSV import')
        self.assertIn('row 2', by_line[3]['reasons'][0])
        self.assertEqual(by_line[4]['matches'][0]['vendor']['id'], self.acme['id'])
        self.assertEqual(by_line[5]['classification'], 'possible_duplicate')
        self.assertEqual(by_line[9]['data']['notes'], 'Imported category: Knitwear')
        self.assertEqual(Vendor.objects.count(), 1, 'preview must not write')

    def test_confirm_imports_and_creates_call_tasks(self):
        response = self.upload('/api/v1/imports/vendors/confirm/')
        self.assertEqual(response.status_code, 201, response.content)
        summary = response.json()
        self.assertEqual(
            {k: summary[k] for k in ('created', 'matched', 'skipped', 'invalid', 'call_tasks_created')},
            {'created': 4, 'matched': 1, 'skipped': 3, 'invalid': 3, 'call_tasks_created': 1},
        )
        batch = ImportBatch.objects.get(pk=summary['batch_id'])
        self.assertEqual(batch.intake_events.count(), 5)

        # Changed in v1.3: every imported row is an applicant with an application at NEW.
        self.assertEqual((summary['applications_created'], summary['applications_bundled']), (4, 1))
        juniper = Vendor.objects.get(brand_name='Juniper')
        self.assertEqual(juniper.display_status, 'applied')
        application = juniper.applications.get()
        self.assertEqual((application.stage, application.source), ('NEW', 'CSV import'))
        self.assertEqual(juniper.tasks.get().task_type, Task.TaskType.DISCOVERY_CALL)
        self.assertEqual(juniper.tasks.get().application, application)
        self.assertTrue(juniper.activity.filter(event_type=ActivityEvent.EventType.LEAD_IMPORTED).exists())
        for name in ('Called Before', 'Dormant', 'Opted Out'):
            self.assertFalse(Vendor.objects.get(brand_name=name).tasks.exists(), name)
            self.assertEqual(Vendor.objects.get(brand_name=name).applications.get().stage, 'NEW', name)
        self.assertFalse(Vendor.objects.get(brand_name='Dormant').is_active)
        self.assertTrue(Vendor.objects.get(brand_name='Opted Out').outreach_opt_out)

        acme = Vendor.objects.get(pk=self.acme['id'])
        self.assertEqual(acme.intake_events.filter(source_type=LeadIntakeEvent.SourceType.CSV_IMPORT).count(), 1)
        self.assertEqual(acme.tasks.count(), 1, 'matched rows do not add call tasks')
        self.assertEqual(acme.applications.count(), 1, 'the matched row was bundled into the open application')
        self.assertTrue(acme.activity.filter(event_type=ActivityEvent.EventType.APPLICATION_BUNDLED).exists())

    def test_possible_duplicates_import_only_when_included(self):
        summary = self.upload('/api/v1/imports/vendors/confirm/', include_lines=['5']).json()
        self.assertEqual(summary['created'], 5)
        self.assertEqual(Vendor.objects.filter(brand_name__iexact='acme').count(), 2)

    def test_rejects_large_or_missing_files(self):
        self.assertEqual(self.client.post('/api/v1/imports/vendors/preview/', {}, format='multipart').status_code, 400)
        big = 'Brand,Email\n' + 'x' * (2 * 1024 * 1024)
        self.assertEqual(self.upload('/api/v1/imports/vendors/preview/', big).status_code, 400)
