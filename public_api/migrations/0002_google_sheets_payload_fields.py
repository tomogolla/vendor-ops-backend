from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('public_api', '0001_google_sheets_submission')]
    operations = [
        migrations.AddField(
            model_name='googlesheetssubmission', name='raw_fields',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='googlesheetssubmission', name='row_number',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='googlesheetssubmission', name='sheet_name',
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
