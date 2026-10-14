"""Data migration (v1.3): sets each application's stage from its old status.

Expand-only: `status` itself is left untouched and copied into `legacy_status`. Only rows that
have not been mapped yet (legacy_status IS NULL) are touched, so it is safe to re-run.
Reversing is a no-op: the schema migration's reverse drops the new columns.
"""

from django.db import migrations
from django.db.models import F

STATUS_TO_STAGE = {
    'SUBMITTED': 'NEW',
    'IN_REVIEW': 'IN_REVIEW',
    'WAITLISTED': 'IN_REVIEW',
    'APPROVED': 'AWAITING_PAYMENT',
    'DECLINED': 'DECLINED',
    'WITHDRAWN': 'WITHDRAWN',
}


def map_status_to_stage(apps, schema_editor):
    Application = apps.get_model('vendors', 'Application')
    unmapped = Application.objects.filter(legacy_status__isnull=True)
    for status, stage in STATUS_TO_STAGE.items():
        # QuerySet.update() does not touch auto_now, so updated_at keeps its value.
        unmapped.filter(status=status).update(
            stage=stage, legacy_status=F('status'), stage_entered_at=F('updated_at'),
        )


class Migration(migrations.Migration):

    dependencies = [
        ('vendors', '0002_application_stage_and_order_fields'),
    ]

    operations = [
        migrations.RunPython(map_status_to_stage, migrations.RunPython.noop),
    ]
