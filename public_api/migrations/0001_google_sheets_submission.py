import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [('vendors', '0011_lines_from_requests_event')]
    operations = [
        migrations.CreateModel(
            name='GoogleSheetsSubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('submission_id', models.CharField(max_length=500, unique=True)),
                ('sheet_type', models.CharField(max_length=50)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('application', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='vendors.application')),
            ],
            options={'ordering': ['-received_at']},
        ),
    ]
