from django.core.management.base import BaseCommand
from django.db import transaction

from vendors.services import backfill_lead_applications


class Command(BaseCommand):
    help = (
        'v1.3: create an application for every lead that has none (NEW, or DECLINED for leads marked '
        'not qualified). Idempotent. Run by hand after deploying; no migration runs it.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report the counts and roll everything back.')

    def handle(self, *args, dry_run=False, **options):
        with transaction.atomic():
            counts = backfill_lead_applications()
            if dry_run:
                transaction.set_rollback(True)
        prefix = 'Dry run, nothing saved. Would create' if dry_run else 'Created'
        self.stdout.write(
            f"{prefix} {counts['new']} application(s) at NEW and {counts['declined']} at DECLINED; "
            f"linked {counts['calls_linked']} open discovery call(s)."
        )
